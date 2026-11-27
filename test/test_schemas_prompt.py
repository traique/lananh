"""Test stock/schemas.py - prompt JSON cho các bước debate phải là skeleton
giá trị, không phải JSON-schema nguyên bản (model yếu copy nhầm thành giá trị);
lượt JSON đi utility_json (Groq/Gemini JSON mode) trước, provider-chain dự phòng.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import orchestrator, timeouts  # noqa: E402
from stock import schemas  # noqa: E402
from stock.schemas import DebateCase, NewsAnalysis  # noqa: E402

_GOOD_NEWS = {
    "relevant": True,
    "sentiment_label": "tiêu cực",
    "key_points": ["x"],
    "impact_note": "y",
}


class _Text:
    def __init__(self, text):
        self.text = text
        self.used_fallback = False


def test_type_skeleton_la_gia_tri_khong_phai_mo_ta():
    skeleton = json.loads(schemas._type_skeleton(NewsAnalysis))
    assert skeleton == {
        "relevant": True,
        "sentiment_label": "nội dung text",
        "key_points": ["nội dung ý 1"],
        "impact_note": "nội dung text",
    }


def test_type_skeleton_model_long_nhau_cho_bull_bear():
    skeleton = json.loads(schemas._type_skeleton(DebateCase))
    side = {"thesis": "nội dung text", "points": ["nội dung ý 1"]}
    assert skeleton == {"bull": side, "bear": side}


def test_schema_debate_khong_co_truong_action_hay_gia():
    """Không output có cấu trúc nào mang action/giá - LLM không thể thành nguồn số."""
    for model in (NewsAnalysis, DebateCase, schemas.BullCase, schemas.BearCase):
        text = json.dumps(model.model_json_schema()).lower()
        for banned in ("action", "stop", "target", "entry", "price", "confidence"):
            assert banned not in text, (model.__name__, banned)
    assert not hasattr(schemas, "FinalDecision")


@pytest.mark.asyncio
async def test_ask_structured_utility_json_truoc_khong_dung_provider_chain(monkeypatch):
    captured = {}

    async def fake_utility(prompt):
        captured["prompt"] = prompt
        return dict(_GOOD_NEWS)

    async def fail_ask(prompt):
        raise AssertionError("không được gọi provider-chain khi utility_json đã đúng")

    monkeypatch.setattr(orchestrator, "utility_json", fake_utility)
    monkeypatch.setattr(orchestrator, "ask", fail_ask)

    result = await schemas.ask_structured(NewsAnalysis, "prompt", step_name="news")
    assert result is not None and result.relevant is True
    # Prompt phải chứa skeleton giá trị, KHÔNG chứa từ khóa mô tả field của
    # JSON-schema (nguyên nhân model trả {'description': ..., 'type': ...}).
    assert '"relevant": true' in captured["prompt"]
    assert "description" not in captured["prompt"]
    assert "model_json_schema" not in captured["prompt"]
    assert "JSON" in captured["prompt"]  # Groq json_object bắt buộc nhắc JSON


@pytest.mark.asyncio
async def test_ask_structured_sai_schema_thi_provider_chain_kem_loi(monkeypatch):
    prompts = []

    async def bad_utility(prompt):
        return {"relevant": {"description": "Có", "type": True}}

    async def fake_ask(prompt):
        prompts.append(prompt)
        return _Text(
            "```json\n"
            + json.dumps(
                {
                    "relevant": False,
                    "sentiment_label": "trung lập",
                    "key_points": [],
                    "impact_note": "",
                }
            )
            + "\n```"
        )

    monkeypatch.setattr(orchestrator, "utility_json", bad_utility)
    monkeypatch.setattr(orchestrator, "ask", fake_ask)

    result = await schemas.ask_structured(NewsAnalysis, "prompt", step_name="news")
    assert result is not None and result.relevant is False
    assert len(prompts) == 1
    # Lần retry phải cảnh báo rõ về lỗi copy schema làm giá trị.
    assert "định nghĩa field" in prompts[0]
    assert "description" in prompts[0]  # nhắc đúng dạng lỗi đã mắc


@pytest.mark.asyncio
async def test_ask_structured_utility_json_none_thi_du_phong_va_toi_da_2_luot(monkeypatch):
    calls = []

    async def none_utility(prompt):
        calls.append("utility")
        return None

    async def junk_ask(prompt):
        calls.append("ask")
        return _Text("không có JSON nào")

    monkeypatch.setattr(orchestrator, "utility_json", none_utility)
    monkeypatch.setattr(orchestrator, "ask", junk_ask)
    assert await schemas.ask_structured(NewsAnalysis, "p", step_name="news") is None
    assert calls == ["utility", "ask"]  # đúng 2 lượt, không retry thêm


@pytest.mark.asyncio
async def test_ask_structured_het_han_chot_luot_khong_goi_them(monkeypatch):
    calls = []

    async def utility(prompt):
        calls.append("utility")
        return dict(_GOOD_NEWS)

    async def ask(prompt):
        calls.append("ask")
        return _Text(json.dumps(_GOOD_NEWS))

    monkeypatch.setattr(orchestrator, "utility_json", utility)
    monkeypatch.setattr(orchestrator, "ask", ask)
    with timeouts.turn_deadline(0):
        assert await schemas.ask_structured(NewsAnalysis, "p", step_name="news") is None
    assert calls == []


@pytest.mark.asyncio
async def test_chain_pacer_chi_nghi_truoc_luot_chain_thu_hai(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(schemas, "_sleep", fake_sleep)
    pacer = schemas.ChainPacer(3)
    await pacer.before_chain_call()
    assert slept == []
    await pacer.before_chain_call()
    await pacer.before_chain_call()
    assert slept == [3.0, 3.0]
    # Không ngủ quá hạn chót còn lại của lượt.
    pacer = schemas.ChainPacer(3)
    with timeouts.turn_deadline(1):
        await pacer.before_chain_call()
        await pacer.before_chain_call()
    assert slept[-1] <= 1.0
    # Tắt nhịp (ROUTER9_STEP_DELAY_SEC=0).
    slept.clear()
    pacer = schemas.ChainPacer(0)
    await pacer.before_chain_call()
    await pacer.before_chain_call()
    assert slept == []


@pytest.mark.asyncio
async def test_lan_du_phong_provider_chain_duoc_gian_nhip(monkeypatch):
    seen = []

    class Pacer:
        async def before_chain_call(self):
            seen.append("pace")

    async def none_utility(prompt):
        return None

    async def ask(prompt):
        seen.append("ask")
        return _Text(json.dumps(_GOOD_NEWS))

    monkeypatch.setattr(orchestrator, "utility_json", none_utility)
    monkeypatch.setattr(orchestrator, "ask", ask)
    result = await schemas.ask_structured(NewsAnalysis, "p", step_name="news", pacer=Pacer())
    assert result is not None and seen == ["pace", "ask"]
