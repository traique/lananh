"""Ngân sách kích thước prompt cho lượt chat.

Trước đây prompt = persona (~11k ký tự) + CHAT_HISTORY_TURNS tin lịch sử không
cắt (mỗi tin có thể tới 4k token output) + trí nhớ (tới ~17k ký tự) + grounding
Tavily/tool/cổ phiếu không trần. Model free context 8k token lỗi hoặc cắt im
lặng. Module này ước lượng token (~4 ký tự/token cho tiếng Việt có dấu, bảo
thủ) và cắt theo thứ tự ít giá trị nhất trước: lịch sử cũ -> grounding ->
trí nhớ. Prompt của người dùng và persona không bao giờ bị cắt ở đây.
"""

from __future__ import annotations

import os

from core import untrusted

CHARS_PER_TOKEN = 4


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(lo, min(hi, value))


# Tổng ký tự tối đa của prompt gửi provider (persona + lịch sử + trí nhớ +
# grounding + câu hỏi). 40.000 ký tự ≈ 10k token: vừa model 16k context còn
# chỗ cho output, dư cho model 128k.
PROMPT_BUDGET_CHARS = _env_int("PROMPT_BUDGET_CHARS", 40_000, 12_000, 400_000)
# Trần riêng từng khối (áp trước khi cân tổng).
HISTORY_MESSAGE_MAX_CHARS = _env_int("HISTORY_MESSAGE_MAX_CHARS", 1_500, 200, 20_000)
GROUNDING_MAX_CHARS = _env_int("GROUNDING_MAX_CHARS", 9_000, 1_000, 100_000)
MEMORY_MAX_CHARS = _env_int("MEMORY_MAX_CHARS", 3_000, 500, 50_000)

_CUT_MARK = "\n[... đã lược bớt cho vừa ngân sách ...]\n"


def estimate_tokens(text: str) -> int:
    return (len(text or "") + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def truncate_middle(text: str, max_chars: int) -> str:
    """Giữ đầu và cuối (đầu thường là chỉ dẫn, cuối là dữ liệu mới nhất).

    Văn bản có rào dữ liệu ngoài (core/untrusted.py): khối bị cắt ngang được
    đóng/mở lại marker ở 2 bên vết cắt, để không đoạn dữ liệu ngoài nào nằm
    "ngoài rào" (có thể vượt max_chars vài chục ký tự marker)."""
    text = text or ""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(_CUT_MARK) + 20:
        if untrusted.contains_untrusted(text):
            return ""
        return text[:max_chars]
    keep = max_chars - len(_CUT_MARK)
    head_len = keep * 2 // 3
    head, tail = text[:head_len], text[len(text) - (keep - head_len) :]
    if untrusted.contains_untrusted(text):
        head, tail = untrusted.balance_cut(head, tail)
    return head + _CUT_MARK + tail


def truncate_tail(text: str, max_chars: int) -> str:
    """Giữ phần đầu (dùng cho từng tin lịch sử: mở đầu mang ý chính)."""
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def trim_history(
    history: list[tuple[str, str]],
    budget_chars: int,
    *,
    message_max_chars: int = HISTORY_MESSAGE_MAX_CHARS,
) -> list[tuple[str, str]]:
    """Cắt từng tin về message_max_chars rồi bỏ tin CŨ nhất cho tới khi tổng
    vừa budget_chars. Luôn giữ nguyên thứ tự cũ -> mới."""
    trimmed = [(role, truncate_tail(content, message_max_chars)) for role, content in history]
    total = sum(len(c) for _, c in trimmed)
    while trimmed and total > budget_chars:
        _, dropped = trimmed.pop(0)
        total -= len(dropped)
    return trimmed


def fit_chat_prompt(
    *,
    persona: str,
    history: list[tuple[str, str]],
    memory_context: str,
    grounding: str,
    prompt: str,
    budget_chars: int = PROMPT_BUDGET_CHARS,
) -> tuple[list[tuple[str, str]], str, str]:
    """Trả (history, memory_context, grounding) đã cắt cho vừa ngân sách.

    Thứ tự ưu tiên giữ: prompt > persona > grounding (dữ liệu cho câu hỏi hiện
    tại) > trí nhớ > lịch sử. Lịch sử bị cắt trước; nếu vẫn thiếu thì cắt
    grounding rồi trí nhớ theo trần riêng."""
    grounding = truncate_middle(grounding, GROUNDING_MAX_CHARS)
    memory_context = truncate_middle(memory_context, MEMORY_MAX_CHARS)
    fixed = len(persona) + len(prompt) + len(grounding) + len(memory_context)
    history = trim_history(history, max(0, budget_chars - fixed))
    if fixed > budget_chars:
        # Cắt thêm grounding rồi memory để tổng về ngân sách (lịch sử đã trống).
        over = fixed - budget_chars
        cut_g = min(over, max(0, len(grounding) - 1_000))
        grounding = truncate_middle(grounding, len(grounding) - cut_g)
        over -= cut_g
        if over > 0:
            cut_m = min(over, max(0, len(memory_context) - 500))
            memory_context = truncate_middle(memory_context, len(memory_context) - cut_m)
    return history, memory_context, grounding
