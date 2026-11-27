"""Groq free (~8K token/phút tính cả max_tokens): lượt chat chọn persona đầy đủ hay
rút gọn + max_tokens cho vừa GROQ_TPM_BUDGET, không vừa thì bỏ qua Groq mà KHÔNG
gửi request (provider_calls ghi "skipped_budget"); 413 ghi "too_large"; gpt-oss
nhận reasoning_effort "low"."""

from types import SimpleNamespace

import pytest

from ai import call_log, groq_client, openai_compatible, orchestrator, prompt_budget
from ai.provider_state import provider_state
from core import config, database as db, trace


@pytest.fixture
def chain(monkeypatch):
    """Chuỗi groq -> api1 với provider giả; ghi lại lượt gọi và provider_calls."""
    store: dict[str, str] = {}

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        store[key] = value

    async def history(*args):
        return [("user", "lượt trước"), ("model", "em trả lời lượt trước")]

    rows: list[dict] = []

    async def record(provider, model, **kwargs):
        rows.append({"provider": provider, "model": model, **kwargs})

    async def groq_key():
        return "gk"

    async def groq_model():
        return "openai/gpt-oss-20b"

    calls = SimpleNamespace(groq=[], api=[], rows=rows)

    async def fake_groq(prompt, **kwargs):
        calls.groq.append({"prompt": prompt, **kwargs})
        return openai_compatible.Response("groq-ok")

    async def fake_official(idx, prompt, **kwargs):
        calls.api.append(idx)
        return SimpleNamespace(text=f"api{idx}-ok", used_fallback=True, searched=False)

    async def api_key_for(idx):
        return "k" if idx == 1 else None

    from ai import provider_overrides

    monkeypatch.setattr(provider_overrides, "_api_key_cache", {})
    monkeypatch.setattr(provider_overrides, "_enabled_cache", {})
    monkeypatch.setattr(provider_overrides, "_model_cache", {})
    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    monkeypatch.setattr(db, "get_session_messages", history)
    monkeypatch.setattr(db, "record_provider_call", record)
    monkeypatch.setattr(db, "record_turn_metrics", _no_turn_metrics)
    monkeypatch.setattr(groq_client, "_api_key", groq_key)
    monkeypatch.setattr(groq_client, "_model", groq_model)
    monkeypatch.setattr(groq_client, "generate", fake_groq)
    monkeypatch.setattr(orchestrator.official_client, "generate", fake_official)
    monkeypatch.setattr(orchestrator.official_client, "api_key_for", api_key_for)
    monkeypatch.setattr(config, "GROQ_API_KEY", "gk")
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["groq", "api1"])
    monkeypatch.setattr(prompt_budget, "GROQ_TPM_BUDGET", 7_000)
    provider_state.active_provider = "groq"
    provider_state.router9_dead_since = None
    provider_state.api_exhausted_until = {"groq": 0.0, "api1": 0.0, "api2": 0.0}
    provider_state._loaded = True
    yield calls
    provider_state._loaded = False


async def _no_turn_metrics(**row):
    return None


def _persona(monkeypatch, full: str, compact: str = "PERSONA RÚT GỌN"):
    seen = []

    def load_compact(**kwargs):
        seen.append(kwargs)
        return compact

    monkeypatch.setattr(config, "load_chat_skill", lambda force_reload=False, **kwargs: full)
    monkeypatch.setattr(config, "load_chat_skill_compact", load_compact)
    return seen


# ─── prompt_budget.fit_groq_chat ─────────────────────────────────────────────


def test_persona_day_du_vua_ngan_sach_thi_giu_nguyen():
    compact_calls = []
    fit = prompt_budget.fit_groq_chat(
        system="PERSONA",
        history=[("user", "chào"), ("model", "dạ")],
        prompt="hỏi",
        compact_system=lambda: compact_calls.append(1) or "rút gọn",
        budget=7_000,
    )
    assert fit is not None and not fit.compact
    assert fit.system == "PERSONA" and fit.max_tokens == prompt_budget.GROQ_FULL_MAX_TOKENS
    assert fit.history == [("user", "chào"), ("model", "dạ")]
    assert compact_calls == []  # chỉ dựng bản rút gọn khi cần


def test_persona_dai_chuyen_ban_rut_gon_va_bo_lich_su_cu_cho_vua():
    history = [("user", "h" * 1_000), ("model", "m" * 1_000)] * 2
    fit = prompt_budget.fit_groq_chat(
        system="p" * 12_000,
        history=history,
        prompt="q" * 13_000,
        compact_system=lambda: "rút gọn",
        budget=7_000,
    )
    assert fit is not None and fit.compact
    assert fit.system == "rút gọn" and fit.max_tokens == prompt_budget.GROQ_COMPACT_MAX_TOKENS
    assert fit.tokens + fit.max_tokens <= 7_000
    # Bớt tin cũ nhất trước, lịch sử còn lại vẫn mở đầu bằng tin người dùng.
    assert len(fit.history) < len(history) and fit.history[0][0] == "user"
    assert fit.history[-1] == history[-1]


def test_khong_vua_ca_ban_rut_gon_thi_bo_qua_va_0_la_khong_kiem():
    kwargs = dict(system="p", history=[], prompt="q" * 30_000, compact_system=lambda: "c")
    assert prompt_budget.fit_groq_chat(**kwargs, budget=7_000) is None
    unchecked = prompt_budget.fit_groq_chat(**kwargs, budget=0)
    assert unchecked is not None and not unchecked.compact


def test_uoc_luong_token_tinh_ca_tools_va_tool_calls():
    messages = [{"role": "user", "content": "x" * 280}]
    base = prompt_budget.request_tokens(messages)
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"q": "y" * 280}}}]
    assert prompt_budget.request_tokens(messages, tools) > base + 100
    with_call = messages + [{"role": "assistant", "content": None, "tool_calls": tools}]
    assert prompt_budget.request_tokens(with_call) > base + 100


# ─── Chốt chặn ở groq_client (mọi đường gọi Groq) + reasoning_effort ─────────


@pytest.fixture
def groq_http(monkeypatch):
    sent = []

    async def fake_post(client, **kwargs):
        sent.append(kwargs)
        return '{"ok": true}'

    async def key():
        return "gk"

    async def model():
        return "openai/gpt-oss-20b"

    monkeypatch.setattr(openai_compatible, "post_chat_completion", fake_post)
    monkeypatch.setattr(groq_client, "_api_key", key)
    monkeypatch.setattr(groq_client, "_model", model)
    monkeypatch.setattr(prompt_budget, "GROQ_TPM_BUDGET", 7_000)
    return sent


async def test_groq_vuot_ngan_sach_khong_gui_request(groq_http):
    with call_log.collect_usage() as usage:
        with pytest.raises(openai_compatible.BudgetExceededError) as info:
            await groq_client.generate("x" * 20_000)
    assert groq_http == []
    assert info.value.outcome == "skipped_budget"
    assert usage.prompt_chars == 20_000  # provider_calls vẫn thấy độ dài prompt bị bỏ qua
    await groq_client.generate("chào")
    assert len(groq_http) == 1


async def test_groq_gpt_oss_suy_luan_low_model_khac_khong_gui(groq_http, monkeypatch):
    await groq_client.generate("chào")
    assert groq_http[-1]["extra_payload"] == {"reasoning_effort": "low"}
    await groq_client.generate_json("trả JSON")
    assert groq_http[-1]["extra_payload"] == {
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
    }

    async def llama():
        return "llama-3.3-70b-versatile"

    monkeypatch.setattr(groq_client, "_model", llama)
    await groq_client.generate("chào")
    assert groq_http[-1]["extra_payload"] is None
    monkeypatch.setattr(config, "GROQ_REASONING_EFFORT", "")
    await groq_client.generate("chào", model="openai/gpt-oss-20b")
    assert groq_http[-1]["extra_payload"] is None


def test_doc_muc_suy_luan_tu_env():
    assert config._groq_reasoning_effort("") == ""
    assert config._groq_reasoning_effort("off") == ""
    assert config._groq_reasoning_effort("HIGH") == "high"
    assert config._groq_reasoning_effort("max") == "low"


# ─── orchestrator.chat ───────────────────────────────────────────────────────


async def test_chat_groq_dung_persona_rut_gon_kem_cach_tra_loi(chain, monkeypatch):
    seen = _persona(monkeypatch, "P" * 12_000)
    response = await orchestrator.chat(
        -5, "chào em", channel="zalo", address_form="chị", style_rules="CÁCH TRẢ LỜI: ngắn"
    )
    assert response.text == "groq-ok" and chain.api == []
    call = chain.groq[0]
    assert call["max_tokens"] == prompt_budget.GROQ_COMPACT_MAX_TOKENS
    assert call["system_instruction"] == "PERSONA RÚT GỌN\n\nCÁCH TRẢ LỜI: ngắn"
    assert seen == [{"variant": config.PERSONA_MEMBER, "channel": "zalo", "address_form": "chị"}]


async def test_chat_persona_ngan_vua_ngan_sach_giu_ban_day_du(chain, monkeypatch):
    seen = _persona(monkeypatch, "PERSONA")
    await orchestrator.chat(1, "chào em")
    call = chain.groq[0]
    assert call["system_instruction"] == "PERSONA"
    assert call["max_tokens"] == prompt_budget.GROQ_FULL_MAX_TOKENS
    assert seen == []


async def test_chat_qua_ngan_sach_bo_qua_groq_khong_gui_va_ghi_skipped(chain, monkeypatch):
    _persona(monkeypatch, "P" * 12_000)
    with trace.turn("telegram:77"):
        response = await orchestrator.chat(1, "x" * 25_000)
        # Chỉ api1: lượt bỏ qua không phải 1 lượt gọi.
        assert trace.metrics().llm_calls == 1
    assert response.text == "api1-ok" and chain.groq == [] and chain.api == [1]
    outcomes = [(row["provider"], row["outcome"], row["stage"]) for row in chain.rows]
    assert outcomes == [("groq", "skipped_budget", "chat"), ("api1", "ok", "chat")]
    assert chain.rows[0]["prompt_chars"] >= 25_000
    assert not provider_state.api_in_cooldown("groq")  # không phải 429


def test_phan_loai_413_va_bo_qua():
    too_large = openai_compatible.OpenAICompatibleError("Groq HTTP 413: TPM", status_code=413)
    assert orchestrator._classify_outcome(too_large) == "too_large"
    assert call_log.classify(too_large) == "too_large"
    skipped = openai_compatible.BudgetExceededError("quá ngân sách")
    assert orchestrator._classify_outcome(skipped) == "skipped_budget"
    assert call_log.classify(skipped) == "skipped_budget"


async def test_luot_bo_qua_khong_tinh_vao_turn_metrics(monkeypatch):
    rows = []

    async def record(provider, model, **kwargs):
        rows.append(kwargs["outcome"])

    monkeypatch.setattr(db, "record_provider_call", record)
    monkeypatch.setattr(db, "record_turn_metrics", _no_turn_metrics)
    with trace.turn("telegram:78"):
        await call_log.record("groq", "m", outcome="skipped_budget", latency_ms=0)
        await call_log.record("api1", "m", outcome="ok", latency_ms=5)
        metrics = trace.metrics()
        assert metrics.llm_calls == 1 and metrics.failed_calls == 0
    assert rows == ["skipped_budget", "ok"]


# ─── Persona rút gọn (templates/chat_skill_compact.j2) ───────────────────────


def _compact_all() -> dict[tuple[str, str | None], str]:
    config._chat_skill_cache.clear()
    try:
        return {
            (variant, form): config.load_chat_skill_compact(variant=variant, address_form=form)
            for variant in config.PERSONA_VARIANTS
            for form in (None, "chị")
        }
    finally:
        config._chat_skill_cache.clear()


def test_persona_rut_gon_ngan_va_giu_luat_an_toan():
    for (variant, form), text in _compact_all().items():
        flat = " ".join(text.split())
        assert 1_000 < len(text) <= 2_200, (variant, form, len(text))
        assert flat.startswith("Bạn là Lan Anh.")
        assert "Không bịa số liệu" in flat and "[Kết quả tool ...]" in flat
        assert "Asia/Ho_Chi_Minh" in flat and 'gợi ý nhắn "phân tích <mã>"' in flat
        if form and variant != config.PERSONA_OWNER:
            assert 'gọi người đang trò chuyện là "chị"' in flat
    compact = _compact_all()
    member = " ".join(compact[(config.PERSONA_MEMBER, None)].split())
    admin = " ".join(compact[(config.PERSONA_ZALO_ADMIN, None)].split())
    owner = " ".join(compact[(config.PERSONA_OWNER, None)].split())
    assert "không đưa khuyến nghị mua/bán/giữ" in member and "KHÔNG phải chủ bot" in member
    assert "không tự đặt ra nhãn hành động" in admin and "KHÔNG phải chủ bot" not in admin
    assert "không đưa khuyến nghị" not in owner and "không tự đặt ra nhãn" not in owner


def test_persona_rut_gon_ho_so_chi_cho_chu_bot_va_goi_y_kenh(tmp_path, monkeypatch):
    import yaml

    data = yaml.safe_load(config.CHAT_SKILL_PATH.read_text(encoding="utf-8"))
    data["persona"]["user_profile"]["nickname"] = "Tuấn-bí-mật"
    custom = tmp_path / "chat_skill.yaml"
    custom.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(config, "CHAT_SKILL_PATH", custom)
    config._chat_skill_cache.clear()
    try:
        owner = config.load_chat_skill_compact()
        admin = config.load_chat_skill_compact(variant=config.PERSONA_ZALO_ADMIN)
        odd = config.load_chat_skill_compact(variant="khong-ro")
        zalo = config.load_chat_skill_compact(variant=config.PERSONA_MEMBER, channel="zalo")
    finally:
        config._chat_skill_cache.clear()
    assert "Tuấn-bí-mật" in owner
    assert "Tuấn-bí-mật" not in admin and "Tuấn-bí-mật" not in odd
    assert zalo.endswith(config.channel_style_hint("zalo"))


def test_persona_txt_cu_khong_co_ban_rut_gon(tmp_path, monkeypatch):
    legacy = tmp_path / "persona.txt"
    legacy.write_text("Bạn là Lan Anh.\n", encoding="utf-8")
    monkeypatch.setattr(config, "CHAT_SKILL_PATH", legacy)
    config._chat_skill_cache.clear()
    try:
        assert config.load_chat_skill_compact() == ""
    finally:
        config._chat_skill_cache.clear()
