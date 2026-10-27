"""Token qua kênh phụ (OpenAI-compatible SSE/JSON, Groq stream_options, Gemini
usage_metadata), số đo giai đoạn lượt chat (turn_metrics) và mức đốt quota miễn
phí từng provider (services/quota_burn.py)."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

import test_channel_chat_service as _chat
import test_web_admin as _admin
import web_admin
from ai import call_log, drafts, official_client, openai_compatible
from core import database as db
from core import trace
from services import quota_burn

client = _admin.client
settings_store = _admin.settings_store
sleeps = _admin.sleeps
chat_fakes = _chat.chat_fakes


def _sse(*chunks) -> str:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


GROQ_STREAM = _sse(
    {"choices": [{"index": 0, "delta": {"content": "Dạ "}}]},
    {"choices": [{"index": 0, "delta": {"content": "em đây"}, "finish_reason": "stop"}]},
    {"choices": [], "x_groq": {"usage": {"prompt_tokens": 120, "completion_tokens": 7}}},
)


def _transport(seen: dict, body: str, content_type="text/event-stream", headers=None):
    def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200, text=body, headers={"content-type": content_type, **(headers or {})}
        )

    return httpx.MockTransport(handler)


async def _post(http, **kwargs):
    return await openai_compatible.post_chat_completion(
        http,
        base_url="https://api.test/v1",
        api_key="k",
        messages=[{"role": "system", "content": "persona"}, {"role": "user", "content": "chào"}],
        model="m",
        temperature=0.5,
        max_tokens=64,
        provider_label="Groq",
        **kwargs,
    )


async def test_sse_groq_doc_usage_chunk_cuoi_va_xin_include_usage():
    seen = {}
    headers = {"x-ratelimit-remaining-requests": "998", "x-ratelimit-limit-requests": "1000"}
    async with httpx.AsyncClient(transport=_transport(seen, GROQ_STREAM, headers=headers)) as http:
        with call_log.collect_usage() as usage:
            text = await _post(http, include_usage=True)
    assert text == "Dạ em đây"
    assert seen["payload"]["stream_options"] == {"include_usage": True}
    assert (usage.prompt_tokens, usage.completion_tokens) == (120, 7)
    assert usage.prompt_chars == len("persona") + len("chào")
    assert usage.ratelimit["x-ratelimit-remaining-requests"] == "998"


async def test_khong_xin_include_usage_thi_khong_gui_tham_so_la():
    seen = {}
    openrouter = _sse(
        {"choices": [{"delta": {"content": "ok"}}]},
        {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 1}},
    )
    async with httpx.AsyncClient(transport=_transport(seen, openrouter)) as http:
        with call_log.collect_usage() as usage:
            assert await _post(http) == "ok"
    assert "stream_options" not in seen["payload"]
    # OpenRouter luôn gửi usage ở chunk cuối.
    assert (usage.prompt_tokens, usage.completion_tokens) == (9, 1)


async def test_json_thuong_doc_usage():
    seen = {}
    body = json.dumps(
        {
            "choices": [{"message": {"content": "xin chào"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
    )
    async with httpx.AsyncClient(transport=_transport(seen, body, "application/json")) as http:
        with call_log.collect_usage() as usage:
            assert await _post(http) == "xin chào"
    assert (usage.prompt_tokens, usage.completion_tokens) == (3, 2)


async def test_ban_nhap_stream_van_doc_usage():
    seen, pushed = {}, []
    async with httpx.AsyncClient(transport=_transport(seen, GROQ_STREAM)) as http:
        with (
            call_log.collect_usage() as usage,
            drafts.sink_scope(pushed.append),
            drafts.drafting(),
        ):
            assert await _post(http, include_usage=True) == "Dạ em đây"
    assert pushed and (usage.prompt_tokens, usage.completion_tokens) == (120, 7)


async def test_ngoai_o_chua_thi_khong_ghi_gi():
    seen = {}
    async with httpx.AsyncClient(transport=_transport(seen, GROQ_STREAM)) as http:
        assert await _post(http) == "Dạ em đây"  # không có collect_usage: không lỗi


async def test_groq_generate_xin_usage(monkeypatch):
    from ai import groq_client

    seen = {}

    async def fake_post(client, **kwargs):
        seen.update(kwargs)
        return "dạ"

    async def key():
        return "gk"

    async def model():
        return "gpt-oss-test"

    monkeypatch.setattr(groq_client, "_api_key", key)
    monkeypatch.setattr(groq_client, "_model", model)
    monkeypatch.setattr(groq_client.openai_compatible, "post_chat_completion", fake_post)
    await groq_client.generate("chào")
    assert seen["include_usage"] is True


async def test_gemini_generate_ghi_usage_metadata(monkeypatch):
    class Models:
        async def generate_content(self, **kwargs):
            return SimpleNamespace(
                text="Dạ",
                candidates=[],
                usage_metadata=SimpleNamespace(
                    prompt_token_count=40, candidates_token_count=5, thoughts_token_count=15
                ),
            )

    async def get_client(idx):
        return SimpleNamespace(aio=SimpleNamespace(models=Models()))

    async def model_for(idx):
        return "gemini-test"

    monkeypatch.setattr(official_client, "_get_official_client", get_client)
    monkeypatch.setattr(official_client, "_model_for", model_for)
    with call_log.collect_usage() as usage:
        response = await official_client.generate(
            1, "câu hỏi", system_instruction="persona", history=[("user", "trước")]
        )
    assert response.text == "Dạ"
    assert (usage.prompt_tokens, usage.completion_tokens) == (40, 20)
    assert usage.prompt_chars > len("câu hỏi") + len("persona") + len("trước")


# ─── Số đo giai đoạn lượt chat ───────────────────────────────────────────────


async def test_run_chat_turn_do_ngu_canh_va_llm(chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs

    rows = []

    async def record(**row):
        rows.append(row)

    monkeypatch.setattr(db, "record_turn_metrics", record)
    with trace.turn("zalo:a:1", channel="zalo"):
        trace.mark_locked(3, 4)
        await ccs.run_chat_turn(1, "hỏi", channel="zalo")
    await ccs.stop_background_tasks()
    for _ in range(3):
        await asyncio.sleep(0)
    assert rows[0]["context_ms"] is not None and rows[0]["llm_ms"] is not None
    assert (rows[0]["lock_wait_ms"], rows[0]["slot_wait_ms"]) == (3, 4)


async def test_run_chat_turn_het_han_ghi_timeout(chat_fakes, monkeypatch):
    from ai.timeouts import TurnTimeoutError
    from services import channel_chat_service as ccs

    async def slow(*args, **kwargs):
        raise TurnTimeoutError("hết giờ")

    rows = []

    async def record(**row):
        rows.append(row)

    monkeypatch.setattr(ccs.orchestrator, "chat", slow)
    monkeypatch.setattr(db, "record_turn_metrics", record)
    with trace.turn("telegram:5"):
        trace.mark_locked(0, 0)
        turn = await ccs.run_chat_turn(1, "hỏi")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert turn.timed_out and rows[0]["outcome"] == "timeout"


async def test_telegram_gui_tin_do_send_ms():
    from handlers import chat_router

    async def send(target, text, **kwargs):
        await asyncio.sleep(0)
        return "sent"

    with trace.turn("telegram:6"):
        assert await chat_router._send_answer(send, object(), "dạ", None) == "sent"
        assert trace.metrics().send_ms is not None


# ─── Quota miễn phí ──────────────────────────────────────────────────────────


@pytest.fixture
def burn_db(monkeypatch):
    queries = []
    data = {
        "api1": {
            "requests": 160,
            "rate_limited": 2,
            "prompt_tokens": 9000,
            "completion_tokens": 900,
        },
        "api2": {"requests": 40, "rate_limited": 0, "prompt_tokens": 100, "completion_tokens": 10},
        "openrouter": {"requests": 45, "rate_limited": 1},
        "tavily": {"requests": 600, "credits": 850},
        "groq": {"requests": 300},
    }

    async def burn(providers, since):
        queries.append((tuple(providers), since))
        return [{"provider": p, **data[p]} for p in providers if p in data]

    monkeypatch.setattr(db, "provider_burn_since", burn)
    monkeypatch.delenv("GEMINI_KEYS_SAME_PROJECT", raising=False)
    monkeypatch.setattr(call_log, "_latest_ratelimit", {})
    return queries


async def test_burn_dung_cua_so_cua_tung_provider(burn_db, monkeypatch):
    monkeypatch.setenv("FREE_TIER_RPD_GEMINI", "200")
    now = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)  # 17h VN, 3h sáng Pacific
    burns = {b.name: b for b in await quota_burn.snapshot(now)}
    starts = {providers: since for providers, since in burn_db}
    # Gemini reset 0h giờ Thái Bình Dương (26/10 03:00 PDT -> mốc 26/10 07:00 UTC);
    # Gemini Live cùng cửa sổ nên chung 1 câu SQL.
    gemini_start = starts[("api1", "api2", "live_api1", "live_api2")]
    assert gemini_start.astimezone(timezone.utc) == datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
    assert burns["api1"].pct == 80.0 and burns["api2"].pct == 20.0
    assert burns["openrouter"].limit == 50 and burns["openrouter"].pct == 90.0
    # Tavily: theo tháng, tính credit.
    assert burns["tavily"].period == "month" and burns["tavily"].used == 850
    assert burns["tavily"].window_start.startswith("2026-10-01")
    assert burns["groq"].limit == 0 and burns["groq"].pct is None
    assert burns["agnes"].used == 0
    # Live: hạn mức chưa được Google công bố -> chỉ đếm, không %.
    assert burns["live"].limit == 0 and burns["live"].pct is None


async def test_burn_gemini_cung_project_cong_chung(burn_db, monkeypatch):
    monkeypatch.setenv("GEMINI_KEYS_SAME_PROJECT", "1")
    monkeypatch.setenv("FREE_TIER_RPD_GEMINI", "250")
    burns = {b.name: b for b in await quota_burn.snapshot()}
    assert "api1" not in burns and burns["gemini"].used == 200 and burns["gemini"].pct == 80.0


async def test_burn_groq_lay_so_con_lai_tu_header(burn_db, monkeypatch):
    import time

    from ai import groq_client

    async def chat_model():
        return "openai/gpt-oss-20b"

    monkeypatch.setattr(groq_client, "model_name", chat_model)
    call_log._latest_ratelimit[("groq", "openai/gpt-oss-20b")] = (
        time.time() - 5,
        {"x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "150"},
    )
    # Header MỚI HƠN của model tra web (bucket khác) không được đè số của model chat.
    call_log._latest_ratelimit[("groq", "openai/gpt-oss-120b")] = (
        time.time(),
        {"x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "990"},
    )
    groq = {b.name: b for b in await quota_burn.snapshot()}["groq"]
    assert (groq.used, groq.limit, groq.pct, groq.source) == (850, 1000, 85.0, "header")


async def test_canh_bao_80_phan_tram_moi_cua_so_mot_lan(burn_db, monkeypatch):
    store, sent = {}, []

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        store[key] = value

    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    monkeypatch.setattr(
        quota_burn.alerts, "send", lambda text, **kw: sent.append((text, kw)) or True
    )
    monkeypatch.setenv("FREE_TIER_RPD_GEMINI", "200")
    now = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
    assert await quota_burn.check_and_alert(now, force=True) == ["api1", "openrouter", "tavily"]
    assert "Gemini API 1 đã dùng 160/200 lượt" in sent[0][0] and "Thái Bình Dương" in sent[0][0]
    # Cùng cửa sổ: không báo lại (đánh dấu ở settings, sống qua restart).
    assert await quota_burn.check_and_alert(now, force=True) == []
    # Không force: tối đa mỗi CHECK_INTERVAL_SEC.
    assert await quota_burn.check_and_alert(now) == []
    monkeypatch.setenv("QUOTA_BURN_ALERT_PCT", "0")
    assert await quota_burn.check_and_alert(now, force=True) == []


async def test_admin_api_turns_turn_burn(client, monkeypatch):
    created = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)

    async def recent(hours, limit):
        return [{"turn_id": "telegram:1", "total_ms": 900, "created_at": created}]

    async def summary(hours):
        return [{"channel": "telegram", "turns": 1, "total_p50": 900.4, "total_p95": None}]

    async def calls(turn_id):
        return [{"provider": "groq", "latency_ms": 800, "created_at": created}]

    async def snapshot():
        return [quota_burn.Burn("groq", "Groq", "day", "2026-10-26T00:00:00+00:00", used=3)]

    monkeypatch.setattr(web_admin.db, "recent_turn_metrics", recent)
    monkeypatch.setattr(web_admin.db, "turn_stage_summary", summary)
    monkeypatch.setattr(web_admin.db, "provider_calls_for_turn", calls)
    monkeypatch.setattr(web_admin.quota_burn, "snapshot", snapshot)
    assert (await client.get("/admin/api/usage/turns")).status_code == 403
    await _admin._login(client)
    turns = (await client.get("/admin/api/usage/turns")).json()
    assert turns["recent"][0]["created_at"] == "2026-10-26T03:00:00+00:00"
    assert turns["summary"][0]["total_p50"] == 900 and turns["summary"][0]["turns"] == 1
    assert (await client.get("/admin/api/usage/turn")).status_code == 400
    detail = (await client.get("/admin/api/usage/turn?turn_id=telegram:1")).json()
    assert detail["calls"][0]["provider"] == "groq"
    burn = (await client.get("/admin/api/usage/burn")).json()
    assert burn["alert_pct"] == 80 and burn["providers"][0]["used"] == 3


def test_trang_admin_co_bang_quota_va_luot():
    html = (_admin.ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    for endpoint in ("/admin/api/usage/outcomes", "/admin/api/usage/burn", "/admin/api/usage/turn"):
        assert endpoint in html
    for box in ('id="outcomes-table"', 'id="burn-table"', 'id="turns-table"'):
        assert box in html


async def test_groq_tat_xin_usage_bang_env(monkeypatch):
    from ai import groq_client

    seen = {}

    async def fake_post(client, **kwargs):
        seen.update(kwargs)
        return "dạ"

    async def key():
        return "gk"

    async def model():
        return "gpt-oss-test"

    monkeypatch.setattr(groq_client, "_INCLUDE_USAGE", False)
    monkeypatch.setattr(groq_client, "_api_key", key)
    monkeypatch.setattr(groq_client, "_model", model)
    monkeypatch.setattr(groq_client.openai_compatible, "post_chat_completion", fake_post)
    await groq_client.generate("chào")
    assert seen["include_usage"] is False
