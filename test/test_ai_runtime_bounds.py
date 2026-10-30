"""Hạn chót lượt trả lời, timeout tổng của provider stream, bộ lọc CJK và cache
client Google AI Studio (ai/orchestrator.py, ai/openai_compatible.py,
ai/official_client.py, ai/timeouts.py)."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from ai import official_client, openai_compatible, orchestrator
from ai.provider_state import provider_state
from ai.timeouts import TurnTimeoutError, bounded, remaining_turn_seconds, turn_deadline
from core import config, database as db


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    store: dict[str, str] = {}

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        store[key] = value

    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    provider_state.router9_dead_since = None
    provider_state.router9_enabled = True
    provider_state.api_exhausted_until = {"groq": 0.0, "openrouter": 0.0, "api1": 0.0, "api2": 0.0}
    provider_state._loaded = True
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_1", "k1")
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_2", "k2")
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9", "api1", "api2"])
    yield


# ─── Hạn chót lượt ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_deadline_nests_to_earliest_and_resets():
    assert remaining_turn_seconds() is None
    with turn_deadline(100):
        outer = remaining_turn_seconds()
        with turn_deadline(1):
            assert remaining_turn_seconds() <= 1
        with turn_deadline(1000):  # hạn muộn hơn không nới hạn ngoài
            assert remaining_turn_seconds() <= outer
    assert remaining_turn_seconds() is None


@pytest.mark.asyncio
async def test_bounded_distinguishes_provider_timeout_from_turn_timeout():
    async def slow():
        await asyncio.sleep(5)

    with turn_deadline(10):
        with pytest.raises(TimeoutError) as own:
            await bounded(slow, 0.01)
        assert not isinstance(own.value, TurnTimeoutError)
    with turn_deadline(0.02):
        with pytest.raises(TurnTimeoutError):
            await bounded(slow, 30)
        with pytest.raises(TurnTimeoutError):  # đã hết hạn: không gọi nữa
            await bounded(slow, 30)


@pytest.mark.asyncio
async def test_chain_stops_at_turn_deadline_and_skips_remaining_providers(monkeypatch):
    monkeypatch.setattr(config, "CHAT_TURN_TIMEOUT_SEC", 0.05)
    api_calls = []

    async def slow_router9():
        await asyncio.sleep(5)

    async def api_call(idx):
        api_calls.append(idx)
        return "api"

    with pytest.raises(TurnTimeoutError):
        await asyncio.wait_for(
            orchestrator._run_provider_chain(router9_call=slow_router9, api_call=api_call), 2
        )
    assert api_calls == []
    # Hết ngân sách lượt KHÔNG phải lỗi của 9Router -> không khai tử router9.
    assert provider_state.router9_dead_since is None


@pytest.mark.asyncio
async def test_slow_api_provider_is_cut_by_turn_deadline(monkeypatch):
    monkeypatch.setattr(config, "CHAT_TURN_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["api1", "api2"])

    async def api_call(idx):
        await asyncio.sleep(5)

    with pytest.raises(TurnTimeoutError):
        await asyncio.wait_for(
            orchestrator._run_provider_chain(router9_call=None, api_call=api_call), 2
        )


@pytest.mark.asyncio
async def test_streamed_provider_has_total_timeout_even_when_bytes_keep_flowing():
    """Groq/OpenRouter stream nhỏ giọt: timeout từng pha của httpx không bao giờ
    hết hạn; ClientPool.total_timeout_sec phải cắt tổng thời gian."""

    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'

    pool = openai_compatible.ClientPool(timeout_sec=1, max_concurrency=1, total_timeout_sec=0.1)
    pool._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Trickle()
            )
        )
    )

    async def call():
        async with pool.get_semaphore():
            return await openai_compatible.post_chat_completion(
                pool.get_client(),
                base_url="https://groq.example/v1",
                api_key="k",
                messages=[],
                model="m",
                temperature=0,
                max_tokens=8,
                provider_label="Groq",
            )

    try:
        with pytest.raises(TimeoutError, match="Groq"):
            await asyncio.wait_for(pool.bounded("Groq", call), 2)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_generic_provider_timeout_falls_through_to_next_provider(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["groq", "api1"])
    monkeypatch.setattr(config, "GROQ_API_KEY", "g")

    async def groq_call():
        raise TimeoutError("Groq: quá 90s")

    async def api_call(idx):
        return SimpleNamespace(text=f"api{idx}")

    result = await orchestrator._run_provider_chain(
        router9_call=None, api_call=api_call, groq_call=groq_call
    )
    assert result.text == "api1"


# ─── Bộ lọc CJK ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cjk_filter_rejects_contaminated_response_objects(monkeypatch):
    async def router9_call():
        return openai_compatible.Response("Thị trường hôm nay 市场今天表现很好 tăng nhẹ")

    async def api_call(idx):
        return SimpleNamespace(text="Thị trường hôm nay tăng nhẹ.")

    result = await orchestrator._run_provider_chain(router9_call=router9_call, api_call=api_call)
    assert result.text == "Thị trường hôm nay tăng nhẹ."


def test_cjk_filter_allows_a_few_han_characters_in_vietnamese():
    response = SimpleNamespace(text="Chữ Nhẫn (忍) nghĩa là kiên nhẫn.")
    assert orchestrator._assert_output_clean(response, "x") is response


@pytest.mark.asyncio
async def test_cjk_filter_exempts_translation_to_japanese(monkeypatch):
    seen = []

    async def fake_router9(prompt, **kwargs):
        seen.append(prompt)
        return openai_compatible.Response("お世話になっております。確認をお願いします。")

    async def preferred():
        return None

    monkeypatch.setattr(orchestrator.router9_client, "generate", fake_router9)
    monkeypatch.setattr(orchestrator.router9_client, "get_preferred_model_name", preferred)
    prompt = "Chiều dịch cho lượt này: Tiếng Việt → Tiếng Nhật.\nDịch: cảm ơn, nhờ anh kiểm tra."
    response = await orchestrator.ask(prompt)
    assert response.text.startswith("お世話")
    assert orchestrator.cjk_output_expected("ja>vi: 確認お願いします")
    assert not orchestrator.cjk_output_expected("cập nhật giá vàng hôm nay")
    with orchestrator.allow_cjk_output():
        contaminated = SimpleNamespace(text="市场今天表现很好")
        assert orchestrator._assert_output_clean(contaminated, "x") is contaminated


# ─── Client Google AI Studio ─────────────────────────────────────────────────


class _FakeAio:
    def __init__(self, owner):
        self.owner = owner

    async def aclose(self):
        self.owner.closed = True


class _FakeGenaiClient:
    created = 0

    def __init__(self, api_key):
        _FakeGenaiClient.created += 1
        self.api_key = api_key
        self.closed = False
        self.aio = _FakeAio(self)

    def close(self):
        pass


@pytest.fixture
def fake_genai(monkeypatch):
    from google import genai

    _FakeGenaiClient.created = 0
    monkeypatch.setattr(genai, "Client", _FakeGenaiClient)
    monkeypatch.setattr(official_client, "_official_clients", {})

    async def no_override(provider):
        return None

    monkeypatch.setattr(official_client.provider_overrides, "get_api_key_override", no_override)
    return _FakeGenaiClient


@pytest.mark.asyncio
async def test_alternating_api_slots_reuse_clients(fake_genai):
    clients = [await official_client._get_official_client(idx) for idx in (1, 2, 1, 2, 1)]
    assert fake_genai.created == 2
    assert clients[0] is clients[2] is clients[4] and clients[1] is clients[3]


@pytest.mark.asyncio
async def test_key_change_closes_only_that_slots_old_client(fake_genai, monkeypatch):
    old1 = await official_client._get_official_client(1)
    old2 = await official_client._get_official_client(2)
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_1", "rotated")
    new1 = await official_client._get_official_client(1)
    assert new1 is not old1 and old1.closed and not old2.closed
    assert len(official_client._official_clients) == 2


@pytest.mark.asyncio
async def test_usable_indices_respects_admin_switch_and_quota_cooldown(monkeypatch):
    async def no_override(provider):
        return None

    enabled = {"api1": False, "api2": True}

    async def is_enabled(provider):
        return enabled.get(provider, True)

    monkeypatch.setattr(official_client.provider_overrides, "get_api_key_override", no_override)
    monkeypatch.setattr(official_client.provider_overrides, "is_enabled", is_enabled)
    assert await official_client.usable_indices() == [2]

    class Quota(Exception):
        code = 429

    await official_client.note_failure(2, Quota("429 RESOURCE_EXHAUSTED"))
    assert provider_state.api_in_cooldown("api2")
    assert await official_client.usable_indices() == []
    assert await official_client.generate_utility_json("{}") is None


# ─── Persona: hồ sơ chủ bot chỉ cho chủ bot ─────────────────────────────────


@pytest.mark.asyncio
async def test_chat_sends_owner_profile_only_to_owner(monkeypatch):
    captured = []

    async def fake_router9(prompt, **kwargs):
        captured.append(kwargs.get("system_instruction"))
        return openai_compatible.Response("Dạ.")

    async def no_history(*args):
        return []

    async def preferred():
        return None

    def fake_skill(force_reload=False, *, variant=None, **kwargs):
        return "PERSONA+PROFILE" if variant == config.PERSONA_OWNER else "PERSONA"

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 777)
    monkeypatch.setattr(config, "load_chat_skill", fake_skill)
    monkeypatch.setattr(orchestrator.router9_client, "generate", fake_router9)
    monkeypatch.setattr(orchestrator.router9_client, "get_preferred_model_name", preferred)
    monkeypatch.setattr(db, "get_session_messages", no_history)
    await orchestrator.chat(777, "chào em")
    await orchestrator.chat(-3, "chào em")
    assert captured == ["PERSONA+PROFILE", "PERSONA"]


@pytest.mark.asyncio
async def test_chat_chon_bien_the_persona_va_goi_y_kenh(monkeypatch):
    """Admin Zalo (id âm, is_admin) nhận biến thể "zalo_admin" - KHÔNG BAO GIỜ
    "owner" chỉ vì có quyền admin; kênh được truyền xuống để gợi ý trình bày."""
    seen = []

    async def fake_router9(prompt, **kwargs):
        return openai_compatible.Response("Dạ.")

    async def no_history(*args):
        return []

    async def preferred():
        return None

    def fake_skill(force_reload=False, *, variant=None, channel=None, **kwargs):
        seen.append((variant, channel))
        return "PERSONA"

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 777)
    monkeypatch.setattr(config, "load_chat_skill", fake_skill)
    monkeypatch.setattr(orchestrator.router9_client, "generate", fake_router9)
    monkeypatch.setattr(orchestrator.router9_client, "get_preferred_model_name", preferred)
    monkeypatch.setattr(db, "get_session_messages", no_history)
    await orchestrator.chat(-3, "chào em", channel="zalo", is_admin=True)
    await orchestrator.chat(-4, "chào em", channel="zalo")
    await orchestrator.chat(777, "chào em", channel="zoom", is_admin=True)
    await orchestrator.chat(777, "chào em")
    assert seen == [
        (config.PERSONA_ZALO_ADMIN, "zalo"),
        (config.PERSONA_MEMBER, "zalo"),
        (config.PERSONA_OWNER, "zoom"),
        (config.PERSONA_OWNER, "telegram"),
    ]


@pytest.mark.asyncio
async def test_chat_truyen_co_phan_co_phieu_xuong_persona(monkeypatch):
    """Mặc định persona đầy đủ (rag_service và caller cũ không đổi); run_chat_turn
    tắt phần cổ phiếu ở lượt đời thường qua stock_rules=False."""
    seen = []

    async def fake_router9(prompt, **kwargs):
        return openai_compatible.Response("Dạ.")

    async def no_history(*args):
        return []

    async def preferred():
        return None

    def fake_skill(force_reload=False, *, stock=True, **kwargs):
        seen.append(stock)
        return "PERSONA"

    monkeypatch.setattr(config, "load_chat_skill", fake_skill)
    monkeypatch.setattr(orchestrator.router9_client, "generate", fake_router9)
    monkeypatch.setattr(orchestrator.router9_client, "get_preferred_model_name", preferred)
    monkeypatch.setattr(db, "get_session_messages", no_history)
    await orchestrator.chat(777, "chào em")
    await orchestrator.chat(777, "chào em", stock_rules=False)
    assert seen == [True, False]
