from ai import prompt_budget as pb


def test_trim_history_bo_tin_cu_truoc_va_cat_tung_tin():
    history = [("user", "a" * 3000), ("model", "b" * 100), ("user", "c" * 100)]
    trimmed = pb.trim_history(history, budget_chars=1800, message_max_chars=1500)
    # Tin đầu bị cắt còn 1500 rồi bị bỏ vì tổng 1700 > 1800? Không: 1500+100+100=1700 <= 1800 -> giữ.
    assert [r for r, _ in trimmed] == ["user", "model", "user"]
    assert len(trimmed[0][1]) == 1500
    trimmed = pb.trim_history(history, budget_chars=250, message_max_chars=1500)
    assert [r for r, _ in trimmed] == ["model", "user"]  # tin cũ nhất bị bỏ, thứ tự giữ nguyên


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
    assert pb.estimate_tokens("abcd" * 10) == 10
