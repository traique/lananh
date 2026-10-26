"""Truy vết lượt trả lời (core/trace.py) và ghi lượt gọi provider (ai/call_log.py):
mã lượt trong log + provider_calls, số đo lượt (turn_metrics), token/số ký tự
prompt, Gemini utility/agent/Agnes/Tavily đi qua cùng 1 bộ ghi, /status và
/admin/api/usage/outcomes."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

import test_web_admin as _admin
import web_admin
from ai import agent_service, agnes_client, call_log, official_client, orchestrator
from ai import tavily_client
from core import database as db
from core import trace
from services import channel_dispatch, concurrency, provider_health
from services.channel_result import ChannelResult

client = _admin.client
settings_store = _admin.settings_store
sleeps = _admin.sleeps


@pytest.fixture
def provider_calls(monkeypatch):
    rows: list[dict] = []

    async def record(provider, model, **kwargs):
        rows.append({"provider": provider, "model": model, **kwargs})

    monkeypatch.setattr(db, "record_provider_call", record)
    return rows


@pytest.fixture
def turn_rows(monkeypatch):
    rows: list[dict] = []

    async def record(**row):
        rows.append(row)

    monkeypatch.setattr(db, "record_turn_metrics", record)
    return rows


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


# ─── core/trace.py ───────────────────────────────────────────────────────────


def test_turn_dat_ma_va_long_nhau_giu_ma_ngoai():
    assert trace.current() is None
    with trace.turn("telegram:1", channel="telegram"):
        assert trace.current() == "telegram:1"
        with trace.turn("zoom:a:2"):
            assert trace.current() == "telegram:1"
        assert trace.metrics().channel == "telegram"
    assert trace.current() is None and trace.metrics() is None


def test_job_ghi_de_ma_thua_huong_va_co_ngay():
    with trace.turn("telegram:9"):
        with trace.job("reminders"):
            assert trace.current().startswith("job:reminders:20")
            assert trace.metrics().user_kind == "job"
        assert trace.current() == "telegram:9"


async def test_detach_chi_doi_context_cua_task_con():
    seen = {}

    async def background():
        trace.detach()
        seen["child"] = trace.current()

    with trace.turn("zalo:acc:5"):
        await asyncio.create_task(background())
        assert trace.current() == "zalo:acc:5"
    assert seen["child"] is None


def test_filter_gan_ma_luot_vao_moi_dong_log():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "chào", None, None)
    trace.TurnIdFilter().filter(record)
    assert (record.turn_id, record.turn_tag) == ("-", "")
    with trace.turn("telegram:42"):
        trace.TurnIdFilter().filter(record)
    assert (record.turn_id, record.turn_tag) == ("telegram:42", "[telegram:42] ")
    formatter = logging.Formatter("%(levelname)s - %(turn_tag)s%(message)s")
    assert formatter.format(record) == "INFO - [telegram:42] chào"


def test_configure_logging_gan_filter_vao_handler_goc(monkeypatch):
    import logging_setup

    root = logging.getLogger()
    handler = logging.StreamHandler()
    monkeypatch.setattr(root, "handlers", [handler])
    monkeypatch.setenv("ERROR_DIGEST_ENABLED", "0")
    logging_setup.configure_logging()
    assert any(isinstance(f, trace.TurnIdFilter) for f in handler.filters)


async def test_luot_qua_khoa_ghi_turn_metrics_o_nen(turn_rows):
    with trace.turn("telegram:7", channel="telegram", user_kind="owner"):
        async with concurrency.assistant_turn("trace-test"):
            trace.note_call("groq", "error", 1200, "chat")
            trace.note_call("api1", "ok", 800, "chat")
            trace.note_call("groq", "ok", 50, "utility")
            with trace.stage("llm"):
                pass
    await _settle()
    assert len(turn_rows) == 1
    row = turn_rows[0]
    assert row["turn_id"] == "telegram:7" and row["user_kind"] == "owner"
    assert row["lock_wait_ms"] is not None and row["slot_wait_ms"] is not None
    assert row["llm_calls"] == 3 and row["fallback_used"] is True
    # Lượt utility không phải "câu trả lời chính".
    assert row["final_provider"] == "api1" and row["outcome"] == "ok"
    assert row["llm_ms"] is not None


async def test_luot_khong_khoa_khong_goi_ai_khong_ghi(turn_rows):
    with trace.turn("telegram:8"):
        pass
    with trace.job("reminders"):
        pass
    await _settle()
    assert turn_rows == []


async def test_luot_loi_ghi_outcome_error(turn_rows):
    with pytest.raises(RuntimeError):
        with trace.turn("zalo:a:1", channel="zalo"):
            trace.note_call("groq", "ok", 10, "chat")
            raise RuntimeError("hỏng")
    await _settle()
    assert turn_rows[0]["outcome"] == "error"
    # Lượt không qua run_chat_turn: llm_ms = tổng độ trễ provider.
    assert turn_rows[0]["llm_ms"] == 10


async def test_huy_luot_ghi_outcome_cancelled(turn_rows):
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(10)

    async def run():
        with trace.turn("telegram:11"):
            async with concurrency.assistant_turn("trace-cancel"):
                with pytest.raises(concurrency.TurnCancelled):
                    await concurrency.run_cancellable("trace-cancel", slow)

    task = asyncio.create_task(run())
    await started.wait()
    assert concurrency.cancel_turn("trace-cancel")
    await task
    await _settle()
    assert turn_rows[0]["outcome"] == "cancelled"


async def test_ghi_turn_metrics_loi_db_khong_van_len(monkeypatch):
    async def boom(**row):
        raise ConnectionError("db sập")

    monkeypatch.setattr(db, "record_turn_metrics", boom)
    with trace.turn("telegram:12"):
        trace.note_call("groq", "ok", 5, "chat")
    await _settle()
    await trace.drain()


async def test_so_task_ghi_dang_cho_co_tran(monkeypatch):
    gate = asyncio.Event()

    async def blocked(**row):
        await gate.wait()

    monkeypatch.setattr(db, "record_turn_metrics", blocked)
    for i in range(trace.MAX_PENDING_WRITES + 5):
        with trace.turn(f"telegram:{i}"):
            trace.note_call("groq", "ok", 1, "chat")
    assert len(trace._pending) == trace.MAX_PENDING_WRITES
    gate.set()
    await trace.drain()
    await _settle()
    assert not trace._pending


# ─── ai/call_log.py ──────────────────────────────────────────────────────────


async def test_record_kem_ma_luot_token_va_so_ky_tu(provider_calls):
    with trace.turn("telegram:3"):
        with call_log.collect_usage() as usage:
            call_log.note_prompt_chars(120)
            call_log.note_openai_usage({"usage": {"prompt_tokens": 50, "completion_tokens": 9}})
            # Chunk cuối của Groq stream: choices rỗng, usage nằm trong x_groq.
            call_log.note_openai_usage(
                {"choices": [], "x_groq": {"usage": {"prompt_tokens": 1, "completion_tokens": 2}}}
            )
        with call_log.call_stage("chat"):
            await call_log.record("groq", "gpt-oss", latency_ms=33, usage=usage)
    row = provider_calls[0]
    assert row["turn_id"] == "telegram:3" and row["stage"] == "chat"
    assert (row["prompt_tokens"], row["completion_tokens"], row["prompt_chars"]) == (51, 11, 120)
    assert row["outcome"] == "ok" and row["latency_ms"] == 33


def test_gemini_usage_tinh_ca_token_suy_luan():
    meta = SimpleNamespace(
        prompt_token_count=100, candidates_token_count=20, thoughts_token_count=30
    )
    with call_log.collect_usage() as usage:
        call_log.note_gemini_usage(meta)
        call_log.note_gemini_usage(None)
    assert (usage.prompt_tokens, usage.completion_tokens) == (100, 50)


async def test_record_loi_db_khong_van_len(monkeypatch):
    async def boom(*args, **kwargs):
        raise ConnectionError("db sập")

    monkeypatch.setattr(db, "record_provider_call", boom)
    await call_log.record("groq", "m")  # không raise


async def test_timed_ghi_ca_luot_loi_lan_thanh_cong(provider_calls):
    async def ok():
        call_log.note_tokens(5, 6)
        return "xong"

    async def rate_limited():
        err = RuntimeError("HTTP 429: quá lượt")
        err.status_code = 429
        raise err

    assert await call_log.timed("agnes", "img", ok, stage="image") == "xong"
    with pytest.raises(RuntimeError):
        await call_log.timed("agnes", "img", rate_limited, stage="image")
    assert [(r["outcome"], r["stage"], r["prompt_tokens"]) for r in provider_calls] == [
        ("ok", "image", 5),
        ("rate_limited", "image", None),
    ]
    assert all(r["latency_ms"] is not None for r in provider_calls)


async def test_header_rate_limit_giu_ban_moi_nhat_theo_provider(provider_calls):
    with call_log.collect_usage() as usage:
        call_log.note_ratelimit_headers(
            {"x-ratelimit-remaining-requests": "990", "content-type": "json"}
        )
    await call_log.record("groq", "m", usage=usage)
    _, headers = call_log.latest_ratelimit("groq")
    assert headers == {"x-ratelimit-remaining-requests": "990"}


# ─── Orchestrator: provider-chain ghi mã lượt + token ────────────────────────


async def test_provider_chain_ghi_usage_cua_luot_goi(provider_calls, monkeypatch):
    from ai.provider_state import provider_state

    provider_state._loaded = True
    monkeypatch.setattr(orchestrator.config, "PROVIDER_ORDER", ["groq"])
    monkeypatch.setattr(orchestrator.config, "GROQ_API_KEY", "k")

    async def enabled(name):
        return True

    async def effective(provider):
        return "gpt-oss-test"

    monkeypatch.setattr(orchestrator.provider_overrides, "is_enabled", enabled)
    monkeypatch.setattr(orchestrator, "_effective_model", effective)
    provider_state.api_exhausted_until.pop("groq", None)

    async def groq_call():
        call_log.note_prompt_chars(42)
        call_log.note_tokens(10, 4)
        return orchestrator.openai_compatible.Response("Dạ em chào anh")

    with trace.turn("telegram:77"), orchestrator.call_stage("chat"):
        result = await orchestrator._run_provider_chain(
            router9_call=None, api_call=None, groq_call=groq_call
        )
    assert result.text == "Dạ em chào anh"
    row = provider_calls[-1]
    assert row["provider"] == "groq" and row["model"] == "gpt-oss-test"
    assert (row["turn_id"], row["stage"], row["outcome"]) == ("telegram:77", "chat", "ok")
    assert (row["prompt_chars"], row["prompt_tokens"], row["completion_tokens"]) == (42, 10, 4)


# ─── Gemini utility dự phòng, agent, Agnes, Tavily: cùng 1 bộ ghi ────────────


async def test_gemini_utility_ghi_tung_key_thu(provider_calls, monkeypatch):
    class QuotaError(Exception):
        code = 429

    calls = {"n": 0}

    class Models:
        async def generate_content(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise QuotaError("RESOURCE_EXHAUSTED")
            return SimpleNamespace(
                text='{"ok": true}',
                usage_metadata=SimpleNamespace(
                    prompt_token_count=7, candidates_token_count=3, thoughts_token_count=None
                ),
            )

    fake_client = SimpleNamespace(aio=SimpleNamespace(models=Models()))

    async def usable():
        return [1, 2]

    async def get_client(idx):
        return fake_client

    async def model_for(idx):
        return f"gemini-{idx}"

    async def no_failure(idx, exc):
        return None

    monkeypatch.setattr(official_client.config, "HAS_ANY_AI_STUDIO_KEY", True)
    monkeypatch.setattr(official_client, "usable_indices", usable)
    monkeypatch.setattr(official_client, "_get_official_client", get_client)
    monkeypatch.setattr(official_client, "_model_for", model_for)
    monkeypatch.setattr(official_client, "note_failure", no_failure)
    with orchestrator.call_stage("utility"):
        assert await official_client.generate_utility_json("trả JSON") == {"ok": True}
    assert [(r["provider"], r["outcome"], r["stage"]) for r in provider_calls] == [
        ("api1", "rate_limited", "utility"),
        ("api2", "ok", "utility"),
    ]
    assert provider_calls[1]["prompt_tokens"] == 7 and provider_calls[1]["prompt_chars"] == 8


async def test_agent_ghi_tung_buoc_giai_doan_agent(provider_calls, monkeypatch):
    async def generate_with_tools(messages, tools):
        return SimpleNamespace(text="Kết quả đây ạ", tool_calls=[])

    async def model_name():
        return "gpt-oss-agent"

    from ai import groq_client

    monkeypatch.setattr(groq_client, "generate_with_tools", generate_with_tools)
    monkeypatch.setattr(groq_client, "model_name", model_name)
    token = agent_service._turn.set(agent_service._Turn(system="persona"))
    try:
        text = await agent_service._run_tool_provider("groq", "giá vàng?")
    finally:
        agent_service._turn.reset(token)
    assert text == "Kết quả đây ạ"
    assert provider_calls == [
        {
            "provider": "groq",
            "model": "gpt-oss-agent",
            "outcome": "ok",
            "latency_ms": provider_calls[0]["latency_ms"],
            "stage": "agent",
            "prompt_chars": None,
            "turn_id": None,
            "prompt_tokens": None,
            "completion_tokens": None,
        }
    ]


async def test_agnes_ghi_ca_luot_loi(provider_calls, monkeypatch):
    async def enabled():
        return True

    async def api_key():
        return "k"

    async def model():
        return "agnes-img"

    async def failing_request(method, url, limit, **kwargs):
        raise agnes_client.AgnesError("Agnes AI trả lỗi HTTP 500")

    monkeypatch.setattr(agnes_client, "get_enabled", enabled)
    monkeypatch.setattr(agnes_client, "_api_key", api_key)
    monkeypatch.setattr(agnes_client, "_model", model)
    monkeypatch.setattr(agnes_client, "_request_bytes", failing_request)
    with pytest.raises(agnes_client.AgnesError):
        await agnes_client.generate_image("con mèo")
    assert [(r["provider"], r["model"], r["outcome"], r["stage"]) for r in provider_calls] == [
        ("agnes", "agnes-img", "error", "image")
    ]


async def test_tavily_429_ghi_rate_limited_theo_search_depth(provider_calls, monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(429, text="quota")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def api_key():
        return "tvly"

    monkeypatch.setattr(tavily_client, "_api_key", api_key)
    monkeypatch.setattr(tavily_client, "_get_client", lambda: http)
    with pytest.raises(tavily_client.TavilyError):
        await tavily_client.search_results("giá vàng", search_depth="advanced")
    await http.aclose()
    assert [(r["model"], r["outcome"], r["stage"]) for r in provider_calls] == [
        ("advanced", "rate_limited", "search_tavily")
    ]


# ─── Điểm vào kênh: mã lượt = event_key ──────────────────────────────────────


async def test_dispatch_dat_ma_luot_theo_su_kien(turn_rows):
    seen = {}

    async def chat(user_id, text, is_admin):
        seen["turn"] = trace.current()
        trace.note_call("groq", "ok", 5, "chat")
        return ChannelResult(["dạ"])

    async def get_cached(*args):
        return None

    async def save_cached(*args):
        return None

    payload = await channel_dispatch.dispatch(
        channel_dispatch.DispatchRequest(
            account_id="acc",
            event_id="m1",
            cache_kind="text",
            user_id=-5,
            is_admin=True,
            channel="zalo",
            text="chào em",
            turn_key="zalo:-5",
        ),
        channel_dispatch.DispatchHandlers(
            chat=chat, get_cached=get_cached, save_cached=save_cached
        ),
    )
    assert payload["messages"] == ["dạ"]
    assert seen["turn"] == "zalo:acc:m1"
    await _settle()
    assert turn_rows[0]["turn_id"] == "zalo:acc:m1"
    assert (turn_rows[0]["channel"], turn_rows[0]["user_kind"]) == ("zalo", "admin")


async def test_memory_flush_mang_ma_luot_khong_mang_so_do(monkeypatch):
    from services import memory_service

    seen = {}

    async def flush(user_id, explicit=False):
        seen["turn"] = trace.current()
        seen["metrics"] = trace.metrics()

    monkeypatch.setattr(memory_service, "maybe_flush", flush)
    memory_service._pending.pop(-99, None)
    with trace.turn("zalo:acc:m9"):
        memory_service.after_exchange(-99)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert seen == {"turn": "zalo:acc:m9", "metrics": None}
    memory_service._pending.pop(-99, None)


# ─── /status và /admin/api/usage/outcomes ────────────────────────────────────


def test_dong_status_p50_p95_ty_le_loi():
    lines = provider_health.format_rows(
        [
            {"provider": "groq", "calls": 50, "failed": 2, "p50_ms": 2100.0, "p95_ms": 6300.0},
            {"provider": "api1", "calls": 4, "failed": 4, "p50_ms": None, "p95_ms": None},
        ],
        use_html=True,
    )
    assert lines[0].startswith("<b>📈 24 giờ qua")
    assert lines[1] == "• Groq: 50 lượt · 2,1s/6,3s · lỗi 4%"
    assert lines[2] == "• API 1: 4 lượt · -/- · lỗi 100%"
    assert provider_health.format_rows([]) == []


async def test_dong_status_db_loi_thi_bo_qua(monkeypatch):
    async def boom(hours):
        raise ConnectionError("db")

    monkeypatch.setattr(db, "provider_latency_summary", boom)
    assert await provider_health.latency_lines() == []


async def test_status_zalo_zoom_kem_do_tre(monkeypatch):
    from services import channel_command_service as svc

    async def lines(hours=24, use_html=False):
        return ["📈 24 giờ qua:", "• Groq: 3 lượt · 1,0s/2,0s · lỗi 0%"]

    async def search_line():
        return "🔎 Tra web: Tavily"

    monkeypatch.setattr(svc.provider_health, "latency_lines", lines)
    monkeypatch.setattr(svc.search_status, "search_mode_line", search_line)
    outputs, _ = await svc.maybe_handle_command(1, "/status", is_admin=True, channel="zoom")
    assert "• Groq: 3 lượt" in outputs[0] and "Provider:" in outputs[0]


async def test_admin_api_outcomes(client, monkeypatch):
    from datetime import datetime, timezone

    async def outcomes(hours):
        assert hours == 24
        return [
            {
                "provider": "groq",
                "outcome": "rate_limited",
                "calls": 3,
                "p50_ms": 120.4,
                "p95_ms": None,
                "last_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
            }
        ]

    monkeypatch.setattr(web_admin.db, "provider_outcomes", outcomes)
    assert (await client.get("/admin/api/usage/outcomes")).status_code == 403
    await _admin._login(client)
    body = (await client.get("/admin/api/usage/outcomes?hours=24")).json()
    assert body == [
        {
            "provider": "groq",
            "outcome": "rate_limited",
            "calls": 3,
            "p50_ms": 120,
            "p95_ms": None,
            "last_at": "2026-10-01T00:00:00+00:00",
        }
    ]
