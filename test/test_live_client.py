"""Gemini Live (ai/live_client.py) bằng phiên WebSocket GIẢ (không mạng): ghép bản
chép lời nhiều mảnh, trần byte/thời gian, huỷ giữa chừng vẫn đóng socket, cooldown
riêng live_api*, bộ đọc /noi rơi về TTS khi đọc lệch/lỗi, provider "live" chỉ chạy
khi mọi provider trước lỗi."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from google.genai import types

from ai import cooldown, live_client, official_client, orchestrator, provider_overrides
from ai.provider_state import provider_state
from ai.timeouts import TurnTimeoutError, turn_deadline
from core import config
from core import database as db
from services import monitor_service, provider_health, voice_reply

PCM = b"\x01\x00" * 24000  # 1 giây PCM 16-bit mono 24 kHz


def _audio(data: bytes) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            model_turn=types.Content(
                role="model",
                parts=[types.Part(inline_data=types.Blob(data=data, mime_type="audio/pcm"))],
            )
        )
    )


def _said(text: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(output_transcription=types.Transcription(text=text))
    )


def _done(**flags) -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(**flags))


class FakeSession:
    def __init__(self, messages, *, hang: bool = False) -> None:
        self.messages = list(messages)
        self.hang = hang
        self.sent: list[tuple] = []
        self.closed = False
        self.receiving = asyncio.Event()

    async def send_client_content(self, *, turns=None, turn_complete=True):
        self.sent.append((turns, turn_complete))

    async def receive(self):
        # Như SDK: dừng sau tin có turn_complete.
        self.receiving.set()
        for message in self.messages:
            yield message
            content = message.server_content
            if content is not None and content.turn_complete:
                return
        if self.hang:
            await asyncio.Event().wait()

    async def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, idx: int, factory) -> None:
        self.idx = idx
        self.factory = factory
        self.sessions: list[FakeSession] = []
        self.configs: list = []
        self.exited = 0
        self.aio = SimpleNamespace(live=SimpleNamespace(connect=self._connect))

    @asynccontextmanager
    async def _connect(self, *, model, config):
        self.configs.append((model, config))
        session = self.factory(self.idx)  # có thể raise (lỗi bắt tay/quota)
        self.sessions.append(session)
        try:
            yield session
        finally:
            self.exited += 1


@pytest.fixture
def live(monkeypatch):
    """Môi trường Live giả: 2 key, admin bật, provider_calls ghi vào list."""
    store: dict[str, str] = {}

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        store[key] = value

    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    monkeypatch.setattr(provider_overrides, "_api_key_cache", {})
    monkeypatch.setattr(provider_overrides, "_enabled_cache", {})
    monkeypatch.setattr(provider_overrides, "_model_cache", {})
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_1", "k1")
    monkeypatch.setattr(config, "GOOGLE_AI_STUDIO_API_KEY_2", "k2")
    provider_state.api_exhausted_until = {}
    provider_state.cooldown_reasons = {}
    provider_state._loaded = True
    live_client.reset_sessions_for_tests()

    env = SimpleNamespace(records=[], clients={}, factory=None)

    async def record(provider, model, *, outcome="ok", latency_ms=None, stage=None, usage=None):
        env.records.append((provider, outcome, stage))

    async def get_client(idx):
        if idx not in env.clients:
            env.clients[idx] = FakeClient(idx, lambda i: env.factory(i))
        return env.clients[idx]

    monkeypatch.setattr(live_client.call_log, "record", record)
    monkeypatch.setattr(official_client, "_get_official_client", get_client)
    yield env
    live_client.reset_sessions_for_tests()


def _reply(*pieces: str, audio: bytes = PCM) -> list:
    return [
        _audio(audio[: len(audio) // 2]),
        *[_said(p) for p in pieces],
        _audio(audio[len(audio) // 2 :]),
        _done(turn_complete=True),
    ]


# ─── Một lượt hỏi -> đáp ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_converse_ghep_ban_chep_loi_nhieu_manh_va_dong_phien(live):
    session = FakeSession(_reply("Dạ, em ", "chào", " anh  nha."))
    live.factory = lambda idx: session
    history = [("user", "câu trước"), ("model", "em đáp trước")]
    response = await live_client.converse(
        "Hôm nay thế nào?", system_instruction="PERSONA", history=history
    )

    assert response.text == "Dạ, em chào anh nha."
    assert session.closed and live.clients[1].exited == 1
    turns, complete = session.sent[0]
    assert complete is True
    assert [t.role for t in turns] == ["user", "model", "user"]
    assert turns[-1].parts[0].text == "Hôm nay thế nào?"
    model, cfg = live.clients[1].configs[0]
    assert model == "gemini-3.8-live"
    assert cfg.response_modalities == [types.Modality.AUDIO]
    assert cfg.output_audio_transcription is not None
    assert (
        cfg.system_instruction.startswith("PERSONA")
        and live_client.CHAT_NOTE in cfg.system_instruction
    )
    assert live.records == [("live_api1", "ok", "chat")]
    assert live_client.active_sessions() == 0


@pytest.mark.asyncio
async def test_lich_su_rut_gon_theo_env(live, monkeypatch):
    monkeypatch.setenv("LIVE_HISTORY_MESSAGES", "2")
    session = FakeSession(_reply("Dạ."))
    live.factory = lambda idx: session
    history = [("user", f"tin {i}") for i in range(10)]
    await live_client.converse("hỏi", system_instruction="P", history=history)
    turns = session.sent[0][0]
    assert [t.parts[0].text for t in turns] == ["tin 8", "tin 9", "hỏi"]


@pytest.mark.asyncio
async def test_chi_doi_chut_sau_generation_complete(live, monkeypatch):
    """Model chờ "phát" xong mới gửi turn_complete: hết TAIL_GRACE_SEC thì thôi đợi."""
    monkeypatch.setattr(live_client, "TAIL_GRACE_SEC", 0.05)
    session = FakeSession(
        [_audio(PCM), _said("Dạ "), _done(generation_complete=True), _said("vâng.")], hang=True
    )
    live.factory = lambda idx: session
    response = await live_client.converse("hỏi", system_instruction="P")
    assert response.text == "Dạ vâng." and session.closed


# ─── Trần byte / thời gian / huỷ ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tran_byte_am_thanh_dung_phien_khong_thu_key_khac(live, monkeypatch):
    monkeypatch.setattr(live_client, "max_audio_bytes", lambda: 1000)
    sessions = []

    def factory(idx):
        sessions.append(
            FakeSession([_audio(b"\x00" * 800), _audio(b"\x00" * 800), _done(turn_complete=True)])
        )
        return sessions[-1]

    live.factory = factory
    with pytest.raises(live_client.LiveAudioTooLargeError):
        await live_client.speak("Dạ chào anh.", voice="Kore", timeout_sec=5)
    assert len(sessions) == 1 and sessions[0].closed
    assert live.records == [("live_api1", "too_large", "tts")]


@pytest.mark.asyncio
async def test_het_tran_thoi_gian_dong_socket(live, monkeypatch):
    monkeypatch.setattr(live_client, "call_timeout_sec", lambda: 0.05)
    session = FakeSession([_audio(PCM)], hang=True)
    live.factory = lambda idx: session
    with pytest.raises(TimeoutError):
        await live_client.converse("hỏi", system_instruction="P")
    assert session.closed and live.clients[1].exited == 1
    assert live.records == [("live_api1", "timeout", "chat")]
    assert 2 not in live.clients  # timeout không phải lỗi của key -> không thử api2


@pytest.mark.asyncio
async def test_het_han_chot_luot_raise_turn_timeout(live):
    session = FakeSession([], hang=True)
    live.factory = lambda idx: session
    with turn_deadline(0.1), pytest.raises(TurnTimeoutError):
        await live_client.converse("hỏi", system_instruction="P")
    assert session.closed
    assert live.records == [("live_api1", "turn_timeout", "chat")]


@pytest.mark.asyncio
async def test_huy_giua_chung_van_dong_socket_va_tra_cho(live):
    session = FakeSession([_audio(PCM), _said("Dạ")], hang=True)
    live.factory = lambda idx: session
    task = asyncio.create_task(live_client.converse("hỏi", system_instruction="P"))
    await asyncio.wait_for(session.receiving.wait(), 1)
    await asyncio.sleep(0)
    assert live_client.active_sessions() == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.closed and live.clients[1].exited == 1
    assert live_client.active_sessions() == 0


@pytest.mark.asyncio
async def test_toi_da_live_max_sessions_phien(live, monkeypatch):
    monkeypatch.setattr(live_client, "SESSION_WAIT_SEC", 0.05)
    first = FakeSession([], hang=True)
    live.factory = lambda idx: first
    task = asyncio.create_task(live_client.converse("a", system_instruction="P"))
    await asyncio.wait_for(first.receiving.wait(), 1)
    with pytest.raises(live_client.LiveBusyError):
        await live_client.converse("b", system_instruction="P")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(live.clients[1].sessions) == 1  # phiên thứ 2 không được mở


# ─── Key + cooldown riêng ────────────────────────────────────────────────────


class _ClosedByQuota(Exception):
    """WebSocket bị đóng 1011 kèm lý do quota (kiểu errors.APIError của SDK)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"1011 None. {reason}")
        self.code = 1011


@pytest.mark.asyncio
async def test_quota_api1_cooldown_rieng_roi_dung_api2(live):
    def factory(idx):
        if idx == 1:
            raise _ClosedByQuota("You exceeded your current quota, please check your plan.")
        return FakeSession(_reply("Dạ vâng."))

    live.factory = factory
    response = await live_client.converse("hỏi", system_instruction="P")
    assert response.text == "Dạ vâng."
    assert live.records == [("live_api1", "rate_limited", "chat"), ("live_api2", "ok", "chat")]
    assert provider_state.api_in_cooldown("live_api1")
    assert not provider_state.api_in_cooldown("api1")  # chat Gemini thường không bị đụng
    assert "Gemini Live" in provider_state.cooldown_reasons["live_api1"]
    # Lượt sau bỏ qua api1 đang cooldown.
    live.records.clear()
    await live_client.converse("hỏi", system_instruction="P")
    assert live.records == [("live_api2", "ok", "chat")]


@pytest.mark.asyncio
async def test_cooldown_chat_api1_khong_chan_live(live):
    provider_state.api_exhausted_until["api1"] = 9e12
    live.factory = lambda idx: FakeSession(_reply("Dạ."))
    assert await live_client.usable_indices() == [1, 2]


@pytest.mark.asyncio
async def test_admin_tat_live(live):
    await provider_overrides.set_enabled("live", False)
    assert await live_client.usable_indices() == []
    with pytest.raises(live_client.LiveError):
        await live_client.converse("hỏi", system_instruction="P")


def test_live_cooldown_doc_loi_ngay_tu_cau_chu(monkeypatch):
    monkeypatch.setattr(config, "API_QUOTA_COOLDOWN_SEC", 3600)
    seconds, reason = cooldown.cooldown_for(
        "live_api1", _ClosedByQuota("Quota exceeded: requests per day")
    )
    assert cooldown.MIN_COOLDOWN_SEC <= seconds <= 3600
    assert "NGÀY" in reason and "Live" in reason
    seconds, reason = cooldown.cooldown_for("live_api2", _ClosedByQuota("Quota exceeded"))
    assert seconds == cooldown.DEFAULT_SHORT_COOLDOWN_SEC and "Live" in reason


def test_loi_quota_cua_websocket():
    assert live_client.is_quota_error(_ClosedByQuota("Resource has been exhausted"))
    assert live_client.is_quota_error(RuntimeError("429 RESOURCE_EXHAUSTED"))
    assert not live_client.is_quota_error(RuntimeError("1008 model not found"))


# ─── Đọc nguyên văn (/noi) ───────────────────────────────────────────────────


def test_do_giong_theo_token_bo_dau():
    assert live_client.similarity("Dạ, giá 25.000 đồng!", "da gia 25000 dong") == 1.0
    assert live_client.similarity("Em chào anh.", "") == 0.0
    assert live_client.similarity("Hôm nay trời đẹp quá anh ạ.", "Hôm nay trời đẹp quá anh") > 0.9
    assert (
        live_client.similarity("Hôm nay trời đẹp.", "Dạ em rất vui được trò chuyện với anh hôm nay")
        < 0.5
    )


@pytest.mark.asyncio
async def test_speak_dung_van_ban_tra_am_thanh(live):
    session = FakeSession(_reply("Dạ, ", "em chào anh."))
    live.factory = lambda idx: session
    turn = await live_client.speak("Dạ, em chào anh.", voice="Kore", timeout_sec=5)
    assert turn.audio == PCM and turn.provider == "live_api1" and turn.similarity == 1.0
    assert turn.mime == "audio/L16;codec=pcm;rate=24000"
    cfg = live.clients[1].configs[0][1]
    assert cfg.system_instruction == live_client.SPEAK_INSTRUCTION
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Kore"
    assert session.sent[0][0][-1].parts[0].text == "Dạ, em chào anh."
    assert live.records == [("live_api1", "ok", "tts")]


@pytest.mark.asyncio
async def test_speak_doc_lech_raise_mismatch_khong_thu_key_khac(live):
    live.factory = lambda idx: FakeSession(_reply("Dạ anh hỏi gì em cũng trả lời được ạ."))
    with pytest.raises(live_client.LiveMismatchError) as info:
        await live_client.speak("Hôm nay trời nắng đẹp.", voice="Kore", timeout_sec=5)
    assert info.value.similarity < 0.8
    assert live.records == [("live_api1", "mismatch", "tts")]
    assert 2 not in live.clients


@pytest.fixture
def tts_api(monkeypatch):
    calls = []

    async def speech(idx, text, *, model, voice):
        calls.append((idx, text))
        return b"\x02\x00" * 100, "audio/L16;codec=pcm;rate=24000"

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(official_client, "synthesize_speech", speech)
    monkeypatch.setattr(orchestrator.call_log, "record", nothing)
    return calls


@pytest.mark.asyncio
async def test_auto_doc_lech_thi_dung_tts(live, tts_api):
    live.factory = lambda idx: FakeSession(_reply("Một câu hoàn toàn khác hẳn ạ."))
    speech = await orchestrator.synthesize_speech(
        "Hôm nay trời nắng đẹp.", model="tts-m", voice="Kore", timeout_sec=5, engine="auto"
    )
    assert speech.provider == "api1" and tts_api == [(1, "Hôm nay trời nắng đẹp.")]


@pytest.mark.asyncio
async def test_auto_live_loi_thi_dung_tts_live_ok_thi_khong(live, tts_api):
    def broken(idx):
        raise RuntimeError("1006 Abnormal closure.")

    live.factory = broken
    speech = await orchestrator.synthesize_speech(
        "Dạ.", model="m", voice="Kore", timeout_sec=5, engine="auto"
    )
    assert speech.provider == "api1" and len(tts_api) == 1
    live.factory = lambda idx: FakeSession(_reply("Dạ."))
    live.clients.clear()
    speech = await orchestrator.synthesize_speech(
        "Dạ.", model="m", voice="Kore", timeout_sec=5, engine="auto"
    )
    assert speech.provider == "live_api1" and speech.audio == PCM and len(tts_api) == 1


@pytest.mark.asyncio
async def test_engine_live_khong_roi_ve_tts_engine_tts_khong_mo_live(live, tts_api):
    live.factory = lambda idx: FakeSession(_reply("Khác hẳn."))
    with pytest.raises(live_client.LiveMismatchError):
        await orchestrator.synthesize_speech(
            "Dạ chào anh.", model="m", voice="K", timeout_sec=5, engine="live"
        )
    assert tts_api == []
    live.clients.clear()
    speech = await orchestrator.synthesize_speech(
        "Dạ.", model="m", voice="K", timeout_sec=5, engine="tts"
    )
    assert speech.provider == "api1" and live.clients == {}


@pytest.mark.asyncio
async def test_make_clip_dung_am_thanh_live(live, monkeypatch):
    store = {"voice_reply:42": "on"}

    async def get_setting(key):
        return store.get(key)

    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(voice_reply, "ffmpeg_path", lambda: None)
    monkeypatch.delenv("VOICE_ENGINE", raising=False)
    live.factory = lambda idx: FakeSession(_reply("Dạ chào anh."))
    clip = await voice_reply.make_clip(42, "Dạ chào anh.", channel="telegram")
    assert clip is not None and clip.ext == "wav" and clip.duration_ms == 1000
    assert live.records == [("live_api1", "ok", "tts")]


def test_voice_engine_env(monkeypatch):
    monkeypatch.setenv("VOICE_ENGINE", "TTS")
    assert voice_reply.voice_engine() == "tts"
    monkeypatch.setenv("VOICE_ENGINE", "lạ")
    assert voice_reply.voice_engine() == "auto"


# ─── Provider "live" trong chuỗi chat ────────────────────────────────────────


@pytest.fixture
def chain(monkeypatch, live):
    async def history(*args):
        return [("user", "lượt trước"), ("model", "em đáp lượt trước")]

    async def no_record(*args, **kwargs):
        return None

    monkeypatch.setattr(db, "get_session_messages", history)
    monkeypatch.setattr(orchestrator, "_record_provider_call", no_record)
    monkeypatch.setattr(config, "load_chat_skill", lambda force_reload=False, **kwargs: "PERSONA")
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["api1", "api2", "live"])
    monkeypatch.setattr(config, "LIVE_CHAT_FALLBACK", True)
    calls = SimpleNamespace(log=[], fail=set())

    async def official(idx, prompt, **kwargs):
        calls.log.append(f"api{idx}")
        if f"api{idx}" in calls.fail:
            raise RuntimeError(f"api{idx} lỗi")
        return official_client.FallbackResponse(f"api{idx}-ok")

    async def converse(prompt, *, system_instruction, history=None, voice=None):
        calls.log.append("live")
        calls.live_kwargs = {"prompt": prompt, "system": system_instruction, "history": history}
        return official_client.FallbackResponse("live-ok")

    monkeypatch.setattr(official_client, "generate", official)
    monkeypatch.setattr(live_client, "converse", converse)
    return calls


@pytest.mark.asyncio
async def test_live_chi_chay_khi_moi_provider_truoc_loi(chain):
    response = await orchestrator.chat(1, "chào em")
    assert response.text == "api1-ok" and chain.log == ["api1"]

    chain.log.clear()
    chain.fail = {"api1", "api2"}
    response = await orchestrator.chat(1, "chào em")
    assert response.text == "live-ok" and response.provider == "live"
    assert chain.log == ["api1", "api2", "live"]
    assert chain.live_kwargs["system"] == "PERSONA"
    assert chain.live_kwargs["history"] == [("user", "lượt trước"), ("model", "em đáp lượt trước")]


@pytest.mark.asyncio
async def test_live_khi_cac_provider_truoc_dang_cooldown(chain):
    provider_state.api_exhausted_until.update({"api1": 9e12, "api2": 9e12})
    response = await orchestrator.chat(1, "chào em")
    assert response.text == "live-ok" and chain.log == ["live"]


@pytest.mark.asyncio
async def test_live_bi_bo_qua_khi_tat_co_hoac_cam(chain, monkeypatch):
    chain.fail = {"api1", "api2"}
    with pytest.raises(RuntimeError):
        await orchestrator.chat(1, "chào em", allow_live=False)
    assert "live" not in chain.log
    monkeypatch.setattr(config, "LIVE_CHAT_FALLBACK", False)
    with pytest.raises(RuntimeError):
        await orchestrator.chat(1, "chào em")
    assert "live" not in chain.log
    # Hai key Live cùng cooldown: bỏ qua, báo trong lỗi cuối.
    monkeypatch.setattr(config, "LIVE_CHAT_FALLBACK", True)
    provider_state.api_exhausted_until.update(
        {"api1": 9e12, "api2": 9e12, "live_api1": 9e12, "live_api2": 9e12}
    )
    with pytest.raises(RuntimeError) as info:
        await orchestrator.chat(1, "chào em")
    assert "live" in str(info.value) and "live" not in chain.log


@pytest.mark.asyncio
async def test_live_khong_dung_cho_ask_va_tra_web(chain):
    chain.fail = {"api1", "api2"}
    with pytest.raises(RuntimeError):
        await orchestrator.ask("trả JSON")
    assert "live" not in chain.log
    chain.log.clear()
    # Lượt cần tra web: Live không tự tra được -> không đứng trong chuỗi tra web;
    # chỉ có thể là dự phòng ở chuỗi thường kèm ghi chú "chưa tra được".
    assert not await orchestrator.provider_can_search("live")
    response = await orchestrator.chat(1, "tin mới nhất?", search_mode="native")
    assert response.text == "live-ok"
    assert "KHÔNG tra được" in chain.live_kwargs["prompt"]


# ─── Cấu hình / giám sát / status ────────────────────────────────────────────


def test_provider_order_them_live_vao_cuoi():
    assert config._parse_provider_order("router9,groq,openrouter,api1,api2")[-1] == "live"
    assert config._parse_provider_order("api1,live,api2")[:3] == ["api1", "live", "api2"]
    assert "live" not in config._parse_provider_order("api1,live", live=False)


def test_monitor_live_chi_down_khi_ca_hai_key_cooldown():
    now = 1000.0
    provider_state.api_exhausted_until = {"live_api1": 2000.0}
    assert not monitor_service._is_provider_down("live", now)
    provider_state.api_exhausted_until = {"live_api1": 2000.0, "live_api2": 2000.0}
    assert monitor_service._is_provider_down("live", now)


@pytest.mark.asyncio
async def test_status_hien_so_luot_live_trong_ngay_quota(live, monkeypatch):
    seen = {}

    async def burn(providers, since):
        seen["providers"], seen["since"] = providers, since
        return [
            {"provider": "live_api1", "requests": 7, "rate_limited": 1},
            {"provider": "live_api2", "requests": 3, "rate_limited": 0},
        ]

    monkeypatch.setattr(db, "provider_burn_since", burn)
    provider_state.api_exhausted_until["live_api1"] = 9e9
    provider_state.cooldown_reasons["live_api1"] = "Gemini Live rate limit <phút>"
    lines = await provider_health.live_lines(use_html=True)
    text = "\n".join(lines)
    assert "Gemini Live (gemini-3.8-live): bật" in text
    assert "10 lượt" in text and "1 lượt bị từ chối" in text
    assert "live_api1 cooldown" in text and "&lt;phút&gt;" in text
    assert "aistudio.google.com/rate-limit" in text
    assert seen["providers"] == ["live_api1", "live_api2"]
    requests, limited = await provider_health.live_usage_today(
        datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
    )
    assert (requests, limited) == (10, 1)
    assert seen["since"].astimezone(timezone.utc) == datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
