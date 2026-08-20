"""Unit test cho provider-chain (ai.orchestrator._run_provider_chain).

Chạy: pytest tests/test_provider_chain.py -v

State provider-chain sống trong ai.provider_state.provider_state (1 singleton
ProviderChainState) - các test dưới đây tự reset qua fixture `reset_state`
trước mỗi test để không bị rò rỉ giữa các case.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import orchestrator  # noqa: E402
from ai.provider_state import _STATE_ROUTER9_DEAD_SINCE, provider_state  # noqa: E402
from core import config, database as db  # noqa: E402


# Regression: /model chọn tên model theo catalog 9Router (vd "notion/GPT-5.6
# Luna"), KHÔNG phải tên model chính thức của Google. Bug đã fix: khi 9Router
# lỗi và orchestrator.ask()/chat() fallback sang api1/api2, code cũ vẫn
# truyền nguyên preferred_model_name đó sang official_client.generate(model=...)
# -> Google AI Studio SDK không hiểu tên model dạng đó -> lỗi. Test này PHẢI
# fail nếu ai đó vô tình đưa preferred_model_name (router9) vào lệnh gọi
# official_client trở lại.
@pytest.mark.asyncio
async def test_ask_does_not_leak_router9_model_name_to_official_api_fallback(monkeypatch):
    async def fake_router9_call(prompt, **kwargs):
        raise RuntimeError("9Router lỗi")

    async def fake_get_preferred_model_name():
        return "notion/GPT-5.6 Luna"  # tên model catalog 9Router, KHÔNG hợp lệ với Google AI Studio

    captured_model = {}

    class FakeResponse:
        text = "trả lời từ API dự phòng"

    async def fake_official_generate(idx, prompt, **kwargs):
        captured_model["model"] = kwargs.get("model")
        return FakeResponse()

    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9", "api1", "api2"])
    monkeypatch.setattr(orchestrator.router9_client, "generate", fake_router9_call)
    monkeypatch.setattr(orchestrator.router9_client, "get_preferred_model_name", fake_get_preferred_model_name)
    monkeypatch.setattr(orchestrator.official_client, "api_key_for", lambda idx: "k" if idx == 1 else None)
    monkeypatch.setattr(orchestrator.official_client, "generate", fake_official_generate)

    response = await orchestrator.ask("câu hỏi bất kỳ")

    assert response.text == "trả lời từ API dự phòng"
    assert captured_model["model"] != "notion/GPT-5.6 Luna"
    assert captured_model["model"] is None  # official_client tự dùng GOOGLE_AI_STUDIO_MODEL mặc định


class FakeSettingsStore:
    """Thay thế db.get_setting/set_setting bằng dict trong RAM, để test
    không cần Postgres thật."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value


@pytest.fixture
def fake_store(monkeypatch):
    store = FakeSettingsStore()
    monkeypatch.setattr(db, "get_setting", store.get)
    monkeypatch.setattr(db, "set_setting", store.set)
    return store


@pytest.fixture(autouse=True)
def reset_state(monkeypatch, fake_store):
    """Reset toàn bộ state của provider_state trước mỗi test, và mock 2 API
    key mặc định đã cấu hình (test tự override nếu cần khác)."""
    provider_state.active_provider = "router9"
    provider_state.router9_dead_since = None
    provider_state.api_exhausted_until = {"groq": 0.0, "openrouter": 0.0, "api1": 0.0, "api2": 0.0}
    provider_state._loaded = False

    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_1", "fake-key-1")
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_2", "fake-key-2")
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9", "api1", "api2"])

    # reset_client() thật sẽ gọi client.close() (I/O) - không cần thiết cho
    # test logic thuần provider-chain, mock thành no-op.
    async def _fake_close_client():
        return None

    from ai import router9_client

    monkeypatch.setattr(router9_client, "close", _fake_close_client)
    yield


def _ok_router9_call():
    async def _call():
        return "router9-response"

    return _call


def _failing_router9_call():
    async def _call():
        raise RuntimeError("router9 treo/lỗi mô phỏng")

    return _call


class QuotaExhaustedError(Exception):
    """Mô phỏng lỗi 429/RESOURCE_EXHAUSTED thật của google-genai."""

    def __init__(self):
        super().__init__("429 RESOURCE_EXHAUSTED: quota exceeded")
        self.code = 429


@pytest.mark.asyncio
async def test_router9_song_thi_dung_router9(fake_store):
    """9Router sống bình thường -> luôn dùng router9, không chạm tới api_call."""

    async def api_call(idx):
        raise AssertionError("Không được gọi api_call khi 9Router đang sống")

    result = await orchestrator._run_provider_chain(
        router9_call=_ok_router9_call(), api_call=api_call
    )
    assert result == "router9-response"
    assert provider_state.active_provider == "router9"


@pytest.mark.asyncio
async def test_router9_chet_chuyen_sang_api1(fake_store):
    """9Router lỗi -> đánh dấu chết + active_provider chuyển sang api1."""
    api_calls = []

    async def api_call(idx):
        api_calls.append(idx)
        if idx == 1:
            return "api1-response"
        raise AssertionError("Không nên rơi xuống api2 khi api1 đã thành công")

    result = await orchestrator._run_provider_chain(
        router9_call=_failing_router9_call(), api_call=api_call
    )

    assert result == "api1-response"
    assert api_calls == [1]
    assert provider_state.active_provider == "api1"
    assert provider_state.router9_dead_since is not None
    # State phải được persist vào DB (qua db.set_setting đã mock), không chỉ
    # ở RAM - để sống qua restart như thiết kế gốc.
    assert fake_store.data.get(_STATE_ROUTER9_DEAD_SINCE)


@pytest.mark.asyncio
async def test_router9_chet_roi_khong_thu_lai_router9_o_request_sau(fake_store):
    """Sau khi 9Router đã bị đánh dấu chết ở 1 request trước, request MỚI
    không được thử 9Router nữa (chỉ probe nền/,/use9Router mới thử lại)."""
    router9_call_count = 0

    async def router9_call():
        nonlocal router9_call_count
        router9_call_count += 1
        return "router9-response"  # nếu bị gọi, coi như lỗi thiết kế

    async def api_call(idx):
        return f"api{idx}-response"

    provider_state.router9_dead_since = time.time() - 100  # đã chết từ trước
    provider_state._loaded = True  # tránh ensure_loaded() nạp đè từ DB rỗng

    result = await orchestrator._run_provider_chain(router9_call=router9_call, api_call=api_call)

    assert result == "api1-response"
    assert router9_call_count == 0, "9Router đã biết chết -> KHÔNG được thử lại ở request thường"


@pytest.mark.asyncio
async def test_api1_het_quota_chuyen_api2_va_cooldown(fake_store):
    """api1 lỗi 429 -> đánh dấu cooldown + chuyển sang api2, không phải lỗi
    thường (điểm quan trọng: official_client.is_quota_exhausted_error phải
    phân biệt được)."""
    provider_state.router9_dead_since = time.time() - 100  # bỏ qua nhánh 9Router cho gọn
    provider_state._loaded = True

    async def api_call(idx):
        if idx == 1:
            raise QuotaExhaustedError()
        return "api2-response"

    result = await orchestrator._run_provider_chain(
        router9_call=_failing_router9_call(), api_call=api_call
    )

    assert result == "api2-response"
    assert provider_state.active_provider == "api2"
    assert provider_state.api_in_cooldown("api1"), "api1 phải được đánh dấu cooldown sau lỗi 429"
    assert not provider_state.api_in_cooldown("api2")


@pytest.mark.asyncio
async def test_api1_dang_cooldown_bi_bo_qua_ngay_khong_goi_lai(fake_store):
    """api1 đang trong thời gian cooldown -> KHÔNG được gọi lại, nhảy thẳng
    sang api2 (khác với lỗi 429 mới - ở đây api_call(1) không được gọi)."""
    provider_state.router9_dead_since = time.time() - 100
    provider_state.api_exhausted_until["api1"] = time.time() + 999  # đang cooldown
    provider_state._loaded = True  # tránh ensure_loaded() nạp đè từ DB rỗng

    called_idx = []

    async def api_call(idx):
        called_idx.append(idx)
        return f"api{idx}-response"

    result = await orchestrator._run_provider_chain(
        router9_call=_failing_router9_call(), api_call=api_call
    )

    assert result == "api2-response"
    assert called_idx == [2], "api1 đang cooldown -> không được gọi lại"


@pytest.mark.asyncio
async def test_dao_provider_order_api_truoc_router9(fake_store, monkeypatch):
    """PROVIDER_ORDER=api1,api2,9Router -> phải thử api1 TRƯỚC router9, kể cả
    khi 9Router đang sống bình thường (đảo ưu tiên)."""
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["api1", "api2", "router9"])

    call_order = []

    async def router9_call():
        call_order.append("router9")
        return "router9-response"

    async def api_call(idx):
        call_order.append(f"api{idx}")
        return f"api{idx}-response"

    result = await orchestrator._run_provider_chain(router9_call=router9_call, api_call=api_call)

    assert result == "api1-response"
    assert call_order == ["api1"], "9Router không được thử khi api1 đứng đầu order và đã thành công"


@pytest.mark.asyncio
async def test_moi_provider_deu_that_bai_thi_raise_loi_cuoi(fake_store):
    """9Router chết + cả 2 API đều cooldown -> cứu cánh cuối thử lại các
    provider known-bad; nếu vẫn thất bại hết, phải raise lỗi (không được
    nuốt lỗi và trả None/im lặng)."""
    provider_state.router9_dead_since = time.time() - 100
    provider_state.api_exhausted_until["api1"] = time.time() + 999
    provider_state.api_exhausted_until["api2"] = time.time() + 999
    provider_state._loaded = True

    async def api_call(idx):
        raise RuntimeError(f"api{idx} vẫn lỗi ở lượt cứu cánh")

    with pytest.raises(Exception):
        await orchestrator._run_provider_chain(
            router9_call=_failing_router9_call(), api_call=api_call
        )


@pytest.mark.asyncio
async def test_chua_cau_hinh_api_thi_retry_router9_1_lan(fake_store, monkeypatch):
    """Không có API key nào (hành vi gốc trước khi có provider-chain) ->
    9Router lỗi thì reset + thử lại 9Router đúng 1 lần, không raise ngay."""
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_1", None)
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_2", None)

    attempts = []

    async def router9_call():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("lỗi lần đầu")
        return "router9-response-lan-2"

    async def api_call(idx):
        raise AssertionError("Không có key nào cấu hình -> không được gọi api_call")

    result = await orchestrator._run_provider_chain(router9_call=router9_call, api_call=api_call)

    assert result == "router9-response-lan-2"
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_co_api_van_retry_router9_1_lan_truoc_khi_khai_tu(fake_store):
    """Có cấu hình API key nhưng 9Router chỉ lỗi THOÁNG QUA (thành công ngay ở
    lần retry) -> không được khai tử oan (router9_dead_since phải None) và
    không được rơi xuống api_call, vì 9Router đã hồi trong chính request này."""
    attempts = []

    async def router9_call():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("lỗi thoáng qua lần đầu")
        return "router9-response-lan-2"

    async def api_call(idx):
        raise AssertionError("9Router đã hồi ở lần retry -> không được rơi xuống api_call")

    result = await orchestrator._run_provider_chain(router9_call=router9_call, api_call=api_call)

    assert result == "router9-response-lan-2"
    assert len(attempts) == 2
    assert provider_state.router9_dead_since is None


@pytest.mark.asyncio
async def test_router9_va_api_deu_khong_kha_dung_thi_dung_groq(fake_store, monkeypatch):
    """router9 đã chết + chưa cấu hình api1/api2 -> order router9,groq,api1,api2
    phải rơi xuống groq_call."""
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9", "groq", "api1", "api2"])
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake-groq-key")
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_1", None)
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_2", None)
    provider_state.router9_dead_since = time.time() - 100
    provider_state._loaded = True

    async def groq_call():
        return "groq-response"

    async def api_call(idx):
        raise AssertionError("Chưa cấu hình api1/api2 -> không được gọi")

    result = await orchestrator._run_provider_chain(
        router9_call=_failing_router9_call(), api_call=api_call, groq_call=groq_call
    )

    assert result == "groq-response"
    assert provider_state.active_provider == "groq"


@pytest.mark.asyncio
async def test_groq_het_quota_chuyen_openrouter_va_cooldown(fake_store, monkeypatch):
    """groq lỗi 429 -> cooldown + chuyển sang openrouter, giữ nguyên hành vi
    cooldown-on-quota như api1/api2 nhưng key là string "groq"."""
    from ai import openai_compatible

    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9", "groq", "openrouter", "api1", "api2"])
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake-groq-key")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "fake-openrouter-key")
    provider_state.router9_dead_since = time.time() - 100
    provider_state._loaded = True

    async def groq_call():
        raise openai_compatible.OpenAICompatibleError("Groq HTTP 429: rate limited")

    async def openrouter_call():
        return "openrouter-response"

    async def api_call(idx):
        raise AssertionError("openrouter đã thành công -> không được rơi xuống api")

    result = await orchestrator._run_provider_chain(
        router9_call=_failing_router9_call(),
        api_call=api_call,
        groq_call=groq_call,
        openrouter_call=openrouter_call,
    )

    assert result == "openrouter-response"
    assert provider_state.active_provider == "openrouter"
    assert provider_state.api_in_cooldown("groq")
    assert not provider_state.api_in_cooldown("openrouter")


def test_search_only_providers_router9_truoc_roi_groq_realtime_roi_gemini(monkeypatch):
    """require_real_search: router9 đứng đầu (đã tự bật search phía server,
    xem app/realtime.py bên repo 9Router), rồi groq (compound-mini), rồi
    api1/api2. openrouter không bao giờ xuất hiện (nhánh OpenRouter của
    lananh gọi thẳng API, không đi qua 9Router, không đảm bảo tool search)."""
    from ai import official_client

    monkeypatch.setattr(config, "ROUTER9_API_KEY", "fake-router9-key")
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake-groq-key")
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9", "groq", "openrouter", "api1", "api2"])
    monkeypatch.setattr(official_client, "api_key_for", lambda idx: "k" if idx in (1, 2) else None)

    order = orchestrator._search_only_providers()

    assert order == ["router9", "groq", "api1", "api2"]


def test_search_only_providers_khong_co_router9_key_thi_bo_qua(monkeypatch):
    """Chưa cấu hình ROUTER9_API_KEY -> rơi thẳng xuống groq/api1/api2 như cũ,
    không raise chỉ vì thiếu mỗi router9."""
    from ai import official_client

    monkeypatch.setattr(config, "ROUTER9_API_KEY", "")
    monkeypatch.setattr(config, "GROQ_API_KEY", "fake-groq-key")
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9", "groq", "openrouter", "api1", "api2"])
    monkeypatch.setattr(official_client, "api_key_for", lambda idx: "k" if idx in (1, 2) else None)

    order = orchestrator._search_only_providers()

    assert order == ["groq", "api1", "api2"]
