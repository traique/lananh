"""Bảo vệ quota free-tier: cooldown theo gợi ý của provider (Retry-After...),
router9 chỉ chết vì lỗi sức khoẻ thật, state provider ghi DB kiểu best-effort,
cache settings ghi-xuyên, cache câu trả lời theo sự kiện webhook."""

import time

import httpx
import pytest

from ai import cooldown, openai_compatible, orchestrator
from ai.provider_state import provider_state
from core import config, database as db, settings_cache
from services import reply_cache
from services.reminder_delivery import NotificationTarget, notification_target


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    store: dict[str, str] = {}
    writes: list[tuple[str, str]] = []

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        writes.append((key, value))
        store[key] = value

    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    provider_state.active_provider = "router9"
    provider_state.router9_dead_since = None
    provider_state.router9_enabled = True
    provider_state.api_exhausted_until = {
        "groq": 0.0,
        "openrouter": 0.0,
        "api1": 0.0,
        "api2": 0.0,
        "router9": 0.0,
    }
    provider_state.cooldown_reasons = {}
    provider_state._router9_strikes = []
    provider_state._loaded = True
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_1", "k1")
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_2", "k2")
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9", "api1", "api2"])
    monkeypatch.setattr(config, "API_QUOTA_COOLDOWN_SEC", 3600)

    async def no_record(*args, **kwargs):
        return None

    monkeypatch.setattr(orchestrator, "_record_provider_call", no_record)
    reply_cache.clear()
    yield writes
    reply_cache.clear()


def _http_error(status, body="", headers=None, label="Groq"):
    request = httpx.Request("POST", "https://api.example/chat/completions")
    response = httpx.Response(status, text=body, headers=headers or {}, request=request)
    exc = httpx.HTTPStatusError("err", request=request, response=response)
    return openai_compatible._http_error(label, exc)


# ─── Gợi ý thời gian chờ ─────────────────────────────────────────────────────


def test_loi_429_mang_status_va_retry_after():
    err = _http_error(429, "rate limited", {"retry-after": "12"})
    assert err.status_code == 429 and err.retry_after_sec == 12
    assert openai_compatible.is_rate_limited(err)


def test_x_ratelimit_reset_epoch_ms_kieu_openrouter():
    reset_ms = str(int((time.time() + 40) * 1000))
    err = _http_error(429, "", {"x-ratelimit-reset": reset_ms}, label="OpenRouter")
    assert 35 <= err.retry_after_sec <= 41


def test_cau_try_again_in_trong_body_cua_groq():
    err = _http_error(429, "Please try again in 1m2.5s. Visit console")
    assert err.retry_after_sec == pytest.approx(62.5)


def test_groq_reset_requests_khong_duoc_dung_lam_goi_y():
    err = _http_error(429, "limit", {"x-ratelimit-reset-requests": "2h"})
    assert err.retry_after_sec is None


def test_router9_error_boc_lai_van_doc_duoc_status():
    from ai.router9_client import Router9Error

    original = _http_error(401, "bad key", label="9Router")
    try:
        try:
            raise original
        except openai_compatible.OpenAICompatibleError as exc:
            raise Router9Error(str(exc)) from exc
    except Router9Error as wrapped:
        assert openai_compatible.http_status(wrapped) == 401


# ─── Tính cooldown ───────────────────────────────────────────────────────────


def test_cooldown_429_theo_phut_ngan_theo_goi_y_va_co_tran():
    seconds, reason = cooldown.cooldown_for("groq", _http_error(429, "", {"retry-after": "12"}))
    assert seconds == cooldown.MIN_COOLDOWN_SEC and "phút" in reason
    seconds, _ = cooldown.cooldown_for("groq", _http_error(429, "", {"retry-after": "5000"}))
    assert seconds == cooldown.MAX_SHORT_COOLDOWN_SEC
    seconds, _ = cooldown.cooldown_for("openrouter", _http_error(429, "slow down"))
    assert seconds == cooldown.DEFAULT_SHORT_COOLDOWN_SEC


def test_cooldown_429_theo_ngay_dai_1_gio():
    err = _http_error(429, "Rate limit reached on tokens per day (TPD): Limit 200000")
    seconds, reason = cooldown.cooldown_for("groq", err)
    assert seconds == 3600 and "NGÀY" in reason


class _GeminiQuotaError(Exception):
    def __init__(self, details):
        super().__init__("429 RESOURCE_EXHAUSTED. quota exceeded")
        self.code = 429
        self.details = {"error": {"code": 429, "details": details}}


def test_cooldown_gemini_doc_quota_failure_va_retry_info():
    per_day = _GeminiQuotaError(
        [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"},
        ]
    )
    seconds, reason = cooldown.cooldown_for("api1", per_day)
    assert cooldown.MIN_COOLDOWN_SEC <= seconds <= 3600 and "NGÀY" in reason

    per_minute = _GeminiQuotaError(
        [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "47s"},
        ]
    )
    assert cooldown.cooldown_for("api2", per_minute)[0] == 47
    # "quota exceeded" trơn KHÔNG phải dấu hiệu theo ngày.
    assert cooldown.cooldown_for("api1", _GeminiQuotaError([]))[0] == 90


def test_seconds_until_pacific_midnight():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 10, 25, 23, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert cooldown.seconds_until_pacific_midnight(now) == 3600


# ─── Provider-chain ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_groq_429_theo_phut_chi_cooldown_ngan_kem_ly_do(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["groq", "api1"])
    monkeypatch.setattr(config, "GROQ_API_KEY", "g")

    async def groq_call():
        raise _http_error(429, "Please try again in 45s")

    async def api_call(idx):
        return "api1-ok"

    result = await orchestrator._run_provider_chain(
        router9_call=None, api_call=api_call, groq_call=groq_call
    )
    assert result == "api1-ok"
    remaining = provider_state.api_exhausted_until["groq"] - time.time()
    assert 40 <= remaining <= 46
    assert "45" in provider_state.cooldown_reasons["groq"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        _http_error(400, "context too long", label="9Router"),
        _http_error(422, "content filter", label="9Router"),
        orchestrator.OutputContaminatedError("cjk"),
    ],
)
async def test_router9_loi_theo_request_khong_retry_khong_khai_tu(error):
    attempts = []

    async def router9_call():
        attempts.append(1)
        raise error

    async def api_call(idx):
        return "api1-ok"

    assert await orchestrator._run_provider_chain(router9_call=router9_call, api_call=api_call) == (
        "api1-ok"
    )
    assert attempts == [1]
    assert provider_state.router9_dead_since is None


@pytest.mark.asyncio
async def test_router9_401_khai_tu_ngay_khong_retry():
    attempts = []

    async def router9_call():
        attempts.append(1)
        raise _http_error(401, "invalid key", label="9Router")

    async def api_call(idx):
        return "api1-ok"

    await orchestrator._run_provider_chain(router9_call=router9_call, api_call=api_call)
    assert attempts == [1]
    assert provider_state.router9_dead_since is not None
    assert "401" in provider_state.cooldown_reasons["router9_dead"]


@pytest.mark.asyncio
async def test_router9_429_chi_cooldown_khong_khai_tu_va_luot_sau_bo_qua():
    calls = []

    async def router9_call():
        calls.append(1)
        raise _http_error(429, "slow", {"retry-after": "60"}, label="9Router")

    async def api_call(idx):
        return "api1-ok"

    await orchestrator._run_provider_chain(router9_call=router9_call, api_call=api_call)
    assert provider_state.router9_dead_since is None
    assert provider_state.api_in_cooldown("router9")
    await orchestrator._run_provider_chain(router9_call=router9_call, api_call=api_call)
    assert calls == [1], "router9 đang cooldown 429 thì không gọi lại, kể cả lượt cứu cánh"


@pytest.mark.asyncio
async def test_router9_5xx_le_te_khong_khai_tu_nhung_2_lan_trong_5_phut_thi_chet():
    sequence = iter(["fail", "ok", "fail", "fail"])

    async def router9_call():
        if next(sequence) == "fail":
            raise _http_error(502, "bad gateway", label="9Router")
        return "router9-ok"

    async def api_call(idx):
        return "api1-ok"

    # Lượt 1: lỗi rồi retry thành công -> vẫn sống, strike được xoá.
    assert await orchestrator._run_provider_chain(router9_call=router9_call, api_call=api_call) == (
        "router9-ok"
    )
    assert provider_state.router9_dead_since is None
    # Lượt 2: lỗi 2 lần liên tiếp -> khai tử.
    assert await orchestrator._run_provider_chain(router9_call=router9_call, api_call=api_call) == (
        "api1-ok"
    )
    assert provider_state.router9_dead_since is not None


# ─── State ghi DB best-effort ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_active_provider_khong_doi_thi_khong_ghi_db(reset_state):
    async def router9_call():
        return "router9-ok"

    async def api_call(idx):
        raise AssertionError

    for _ in range(3):
        await orchestrator._run_provider_chain(router9_call=router9_call, api_call=api_call)
    assert reset_state == [], "router9 vẫn sống + active không đổi -> không ghi settings mỗi lượt"


@pytest.mark.asyncio
async def test_db_loi_khi_ghi_state_khong_bien_cau_tra_loi_thanh_loi(monkeypatch):
    """Supabase chập chờn ngay sau lượt gọi thành công: trước đây set_setting raise
    -> chain coi là provider lỗi, gọi tiếp provider khác rồi vẫn báo lỗi."""

    async def broken_set_setting(key, value):
        raise ConnectionError("db down")

    monkeypatch.setattr(db, "set_setting", broken_set_setting)
    provider_state.router9_dead_since = time.time() - 10  # sống lại -> phải ghi DB
    provider_state.router9_enabled = True
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["api1", "api2"])
    calls = []

    async def api_call(idx):
        calls.append(idx)
        return f"api{idx}-ok"

    result = await orchestrator._run_provider_chain(router9_call=None, api_call=api_call)
    assert result == "api1-ok"
    assert calls == [1], "không được gọi provider thứ 2 chỉ vì ghi state lỗi"
    assert provider_state.active_provider == "api1"
    await provider_state.mark_router9_alive()  # ghi DB lỗi nhưng không raise
    await provider_state.mark_api_exhausted("api2", 60, "rate limit")
    assert provider_state.router9_dead_since is None and provider_state.api_in_cooldown("api2")


# ─── Cache settings ghi-xuyên ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_cache_doc_db_1_lan_va_cap_nhat_khi_ghi(monkeypatch):
    from ai import router9_client, tavily_client

    reads = []

    async def get_setting(key):
        reads.append(key)
        return None

    monkeypatch.setattr(db, "get_setting", get_setting)
    for _ in range(3):
        assert await tavily_client.get_enabled() is False
        assert await router9_client.get_preferred_model_name() is None
    assert len(reads) == 2
    await tavily_client.set_enabled(True)
    assert await tavily_client.get_enabled() is True
    assert len(reads) == 2
    settings_cache.clear()


# ─── Cache câu trả lời theo sự kiện webhook ──────────────────────────────────


@pytest.mark.asyncio
async def test_thu_lai_cung_su_kien_chi_gui_lai_khong_goi_llm_lan_nua():
    calls = []

    async def generate():
        calls.append(1)
        return "câu trả lời /rag"

    target = NotificationTarget("telegram", "1", event_key="telegram:77")
    with notification_target(target):
        first = await reply_cache.generate_once("rag", generate)
    with notification_target(target):
        second = await reply_cache.generate_once("rag", generate)
    assert first == second == "câu trả lời /rag"
    assert calls == [1]
    # Ngoài inbox (không có khoá sự kiện) thì luôn chạy.
    await reply_cache.generate_once("rag", generate)
    assert calls == [1, 1]


def test_reply_cache_co_tran_so_muc():
    for i in range(reply_cache.MAX_ENTRIES + 10):
        reply_cache.put(f"k{i}", "v")
    assert len(reply_cache._cache) == reply_cache.MAX_ENTRIES
    reply_cache.put("big", "x" * (reply_cache.MAX_VALUE_CHARS + 1))
    assert reply_cache.get("big") is None
