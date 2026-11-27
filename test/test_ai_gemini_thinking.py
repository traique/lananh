"""Gemini: lượt JSON/tiện ích tắt suy luận (thinking_budget=0), lượt chat theo
GEMINI_CHAT_THINKING_BUDGET; model từ chối tham số thì gọi lại không kèm và nhớ."""

from types import SimpleNamespace

import pytest

from ai import official_client, orchestrator
from core import config


class _Models:
    def __init__(self, reject: bool = False):
        self.configs = []
        self.reject = reject

    async def generate_content(self, model, contents, config):
        self.configs.append(config)
        if self.reject and config.thinking_config is not None:
            raise _ClientError(
                400,
                "INVALID_ARGUMENT: Budget 0 is invalid. This model only works in thinking mode.",
            )
        return SimpleNamespace(text='{"ok": true}', usage_metadata=None, candidates=[])


class _ClientError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code} {message}")
        self.code = code


@pytest.fixture
def gemini(monkeypatch):
    models = _Models()

    async def get_client(idx):
        return SimpleNamespace(aio=SimpleNamespace(models=models))

    async def model_for(idx):
        return "gemini-test-lite"

    async def usable():
        return [1]

    async def no_record(*args, **kwargs):
        return None

    monkeypatch.setattr(official_client, "_get_official_client", get_client)
    monkeypatch.setattr(official_client, "_model_for", model_for)
    monkeypatch.setattr(official_client, "usable_indices", usable)
    monkeypatch.setattr(official_client.call_log, "record", no_record)
    monkeypatch.setattr(config, "HAS_ANY_AI_STUDIO_KEY", True)
    monkeypatch.setattr(official_client, "_THINKING_REJECTED", set())
    return models


def _budget(cfg):
    return cfg.thinking_config.thinking_budget if cfg.thinking_config else None


async def test_utility_json_tat_suy_luan(gemini):
    assert await official_client.generate_utility_json("trả JSON") == {"ok": True}
    assert _budget(gemini.configs[0]) == 0
    assert gemini.configs[0].response_mime_type == "application/json"


async def test_generate_theo_ngan_sach_truyen_vao(gemini):
    await official_client.generate(1, "chào", thinking_budget=512)
    await official_client.generate(1, "chào")
    assert [_budget(c) for c in gemini.configs] == [512, None]


async def test_model_tu_choi_thi_goi_lai_khong_kem_va_nho(gemini):
    gemini.reject = True
    assert await official_client.generate_utility_json("trả JSON") == {"ok": True}
    assert [_budget(c) for c in gemini.configs] == [0, None]
    assert "gemini-test-lite" in official_client._THINKING_REJECTED
    await official_client.generate_utility_json("trả JSON")
    assert [_budget(c) for c in gemini.configs] == [0, None, None]  # không tốn lượt hỏng nữa


async def test_loi_khac_khong_goi_lai(gemini, monkeypatch):
    async def boom(model, contents, config):
        gemini.configs.append(config)
        raise _ClientError(400, "INVALID_ARGUMENT: contents is empty")

    monkeypatch.setattr(gemini, "generate_content", boom)
    with pytest.raises(_ClientError):
        await official_client.generate(1, "chào", thinking_budget=0)
    assert len(gemini.configs) == 1


async def test_viec_phu_qua_chuoi_va_chat_truyen_ngan_sach(monkeypatch):
    seen = []

    async def official(idx, prompt, **kwargs):
        seen.append(kwargs.get("thinking_budget", "không truyền"))
        return SimpleNamespace(text="ok")

    async def api_key_for(idx):
        return "k"

    async def no_record(*args, **kwargs):
        return None

    async def history(*args):
        return []

    from core import database as db

    monkeypatch.setattr(orchestrator.official_client, "generate", official)
    monkeypatch.setattr(orchestrator.official_client, "api_key_for", api_key_for)
    monkeypatch.setattr(orchestrator, "_record_provider_call", no_record)

    async def get_setting(key):
        return None

    async def set_setting(key, value):
        return None

    monkeypatch.setattr(db, "get_session_messages", history)
    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["api1"])
    monkeypatch.setattr(config, "load_chat_skill", lambda force_reload=False, **kw: "PERSONA")
    from ai.provider_state import provider_state

    monkeypatch.setattr(provider_state, "_loaded", True)
    monkeypatch.setattr(provider_state, "api_exhausted_until", {"api1": 0.0})
    await orchestrator.ask_utility("viết lại", providers=["api1"])
    await orchestrator.ask("trả JSON", json_mode=True)
    await orchestrator.ask("câu thường")
    monkeypatch.setattr(config, "GEMINI_CHAT_THINKING_BUDGET", None)
    await orchestrator.chat(1, "chào em")
    monkeypatch.setattr(config, "GEMINI_CHAT_THINKING_BUDGET", 256)
    await orchestrator.chat(1, "chào em")
    assert seen == [0, 0, "không truyền", "không truyền", 256]


def test_doc_ngan_sach_suy_luan_tu_env():
    assert config._thinking_budget("") is None
    assert config._thinking_budget("0") == 0
    assert config._thinking_budget("-5") == -1
    assert config._thinking_budget("nhiều") is None
