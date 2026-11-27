from ai import prompt_budget as pb


def test_trim_history_bo_tin_cu_truoc_va_cat_tung_tin():
    history = [("user", "a" * 3000), ("model", "b" * 100), ("user", "c" * 100)]
    trimmed = pb.trim_history(history, budget_chars=1800, message_max_chars=1500)
    # Tin đầu bị cắt còn 1500 rồi bị bỏ vì tổng 1700 > 1800? Không: 1500+100+100=1700 <= 1800 -> giữ.
    assert [r for r, _ in trimmed] == ["user", "model", "user"]
    assert len(trimmed[0][1]) == 1500
    trimmed = pb.trim_history(history, budget_chars=250, message_max_chars=1500)
    # Tin cũ nhất bị bỏ, thứ tự giữ nguyên; câu trả lời mất câu hỏi ở đầu cũng bị bỏ.
    assert [r for r, _ in trimmed] == ["user"]


def test_lich_su_sau_khi_cat_luon_mo_dau_bang_tin_nguoi_dung():
    history = [("model", "dạ"), ("user", "hỏi 1"), ("model", "đáp 1"), ("user", "hỏi 2")]
    assert pb.trim_history(history, budget_chars=10_000)[0] == ("user", "hỏi 1")
    assert pb.trim_history([("model", "x")], budget_chars=10_000) == []


def test_fit_chat_prompt_cat_theo_thu_tu_lich_su_grounding_memory():
    persona = "p" * 11_000
    history = [("user", "h" * 1000)] * 10
    grounding = "g" * 20_000
    memory = "m" * 6_000
    hist, mem, ground = pb.fit_chat_prompt(
        persona=persona,
        history=history,
        memory_context=memory,
        grounding=grounding,
        prompt="hỏi",
        budget_chars=30_000,
    )
    assert len(ground) <= pb.GROUNDING_MAX_CHARS
    assert len(mem) <= pb.MEMORY_MAX_CHARS
    total = len(persona) + sum(len(c) for _, c in hist) + len(ground) + len(mem) + 3
    assert total <= 30_000
    # Grounding giữ đầu + cuối (đánh dấu đã lược).
    assert ground.startswith("g") and ground.endswith("g") and "lược bớt" in ground


def test_fit_chat_prompt_khong_dung_den_khi_vua_ngan_sach():
    history = [("user", "xin chào"), ("model", "chào anh")]
    hist, mem, ground = pb.fit_chat_prompt(
        persona="p", history=history, memory_context="m", grounding="g", prompt="q"
    )
    assert hist == history and mem == "m" and ground == "g"


def test_estimate_tokens():
    assert pb.estimate_tokens("") == 0
    assert pb.estimate_tokens("x" * 28) == 10
    assert pb.estimate_tokens("abcd" * 10) == 15  # làm tròn lên


def test_ti_le_ky_tu_moi_token_tieng_viet_va_ngan_sach_theo_token(monkeypatch):
    """Tiếng Việt có dấu chỉ ~2,5-3 ký tự/token với tokenizer của gpt-oss/Gemini
    (provider_calls: prompt_chars/prompt_tokens) - 4 ký tự/token cũ đếm thiếu ~30%,
    ngân sách "10k token" thật ra là 14k. Ngân sách đặt theo token nên đổi tỉ lệ thì
    trần ký tự tự đổi theo, mục tiêu token giữ nguyên."""
    assert pb.CHARS_PER_TOKEN == 2.8
    assert 2.5 <= pb.CHARS_PER_TOKEN <= 3.0
    monkeypatch.delenv("PROMPT_BUDGET_CHARS", raising=False)
    monkeypatch.delenv("GROQ_HISTORY_MAX_CHARS", raising=False)
    default = pb._env_int("PROMPT_BUDGET_CHARS", pb.chars_for_tokens(10_000), 12_000, 400_000)
    assert pb.PROMPT_BUDGET_TOKENS == 10_000 and default == 28_000
    assert pb.estimate_tokens("x" * default) == pb.PROMPT_BUDGET_TOKENS
    groq = pb._env_int("GROQ_HISTORY_MAX_CHARS", pb.chars_for_tokens(1_250), 0, 100_000)
    assert pb.GROQ_HISTORY_MAX_TOKENS == 1_250 and groq == 3_500
    # Persona đầy đủ (~9-15k ký tự) đã ~3,5-5,6k token: cùng max_tokens 4096 là vượt
    # trần ~8K token/phút của Groq free - lý do cần persona rút gọn (fit_groq_chat).
    assert pb.estimate_tokens("p" * 9_400) + pb.GROQ_FULL_MAX_TOKENS > pb.GROQ_TPM_BUDGET


def test_lich_su_rieng_cho_groq_co_tran_nho_hon():
    from ai import prompt_budget

    history = [("user", "a" * 1000), ("model", "b" * 1000)] * 5
    groq = prompt_budget.history_for_provider(history, "groq")
    assert sum(len(c) for _, c in groq) <= prompt_budget.GROQ_HISTORY_MAX_CHARS
    assert groq[-1] == history[-1]  # giữ tin mới nhất
    assert prompt_budget.history_for_provider(history, "router9") == history


async def test_chat_doc_lich_su_theo_luot_va_noi_cach_tra_loi_vao_persona(monkeypatch):
    from types import SimpleNamespace

    from ai import orchestrator
    from core import config, database as db

    seen = {}

    async def fake_history(user_id, k, session_timeout_sec):
        seen["k"] = k
        return []

    async def fake_chain(**kwargs):
        seen["groq"] = kwargs["groq_call"]
        return SimpleNamespace(text="dạ")

    async def fake_groq_generate(prompt, *, system_instruction, history, temperature, max_tokens):
        seen["persona"] = system_instruction
        return SimpleNamespace(text="dạ")

    monkeypatch.setattr(config, "CHAT_HISTORY_TURNS", 10)
    monkeypatch.setattr(db, "get_session_messages", fake_history)
    monkeypatch.setattr(orchestrator, "_run_provider_chain", fake_chain)
    monkeypatch.setattr(orchestrator.groq_client, "generate", fake_groq_generate)
    await orchestrator.chat(1, "chào", search_mode="off", style_rules="CÁCH TRẢ LỜI: ngắn gọn")
    assert seen["k"] == 20  # 10 lượt = 20 dòng
    await seen["groq"]()
    assert seen["persona"].endswith("CÁCH TRẢ LỜI: ngắn gọn")
