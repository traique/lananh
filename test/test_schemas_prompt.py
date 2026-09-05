"""Test stock/schemas.py - prompt JSON cho các bước debate phải là skeleton
giá trị, không phải JSON-schema nguyên bản (model yếu copy nhầm thành giá trị).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock import schemas  # noqa: E402
from stock.schemas import BullCase, NewsAnalysis  # noqa: E402


def test_type_skeleton_la_gia_tri_khong_phai_mo_ta():
    skeleton = json.loads(schemas._type_skeleton(NewsAnalysis))
    assert skeleton == {
        "relevant": True,
        "sentiment_label": "nội dung text",
        "key_points": ["nội dung ý 1"],
        "impact_note": "nội dung text",
    }


@pytest.mark.asyncio
async def test_ask_structured_prompt_khong_chua_json_schema_nguyen_ban(monkeypatch):
    captured = {}

    class FakeResponse:
        text = json.dumps({
            "relevant": True,
            "sentiment_label": "tiêu cực",
            "key_points": ["x"],
            "impact_note": "y",
        })
        used_fallback = False

    async def fake_ask(prompt):
        captured["prompt"] = prompt
        return FakeResponse()

    from ai import orchestrator
    monkeypatch.setattr(orchestrator, "ask", fake_ask)

    result = await schemas.ask_structured(NewsAnalysis, "prompt", step_name="news")
    assert result is not None
    assert result.relevant is True
    # Prompt phải chứa skeleton giá trị, KHÔNG chứa từ khóa mô tả field của
    # JSON-schema (nguyên nhân model trả {'description': ..., 'type': ...}).
    assert '"relevant": true' in captured["prompt"]
    assert "description" not in captured["prompt"]
    assert "model_json_schema" not in captured["prompt"]


@pytest.mark.asyncio
async def test_ask_structured_retry_giai_thich_ro_kieu_loi_copy_schema(monkeypatch):
    prompts = []

    class FakeBad:
        text = '{"relevant": {"description": "Có", "type": true}}'
        used_fallback = False

    class FakeGood:
        text = json.dumps({
            "relevant": False,
            "sentiment_label": "trung lập",
            "key_points": [],
            "impact_note": "",
        })
        used_fallback = False

    responses = [FakeBad(), FakeGood()]

    async def fake_ask(prompt):
        prompts.append(prompt)
        return responses.pop(0)

    from ai import orchestrator
    monkeypatch.setattr(orchestrator, "ask", fake_ask)

    result = await schemas.ask_structured(NewsAnalysis, "prompt", step_name="news")
    assert result is not None
    assert len(prompts) == 2
    # Lần retry phải cảnh báo rõ về lỗi copy schema làm giá trị.
    assert "định nghĩa field" in prompts[1]
    assert "description" in prompts[1]  # nhắc đúng dạng lỗi đã mắc
