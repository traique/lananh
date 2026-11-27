"""Ngân sách kích thước prompt cho lượt chat.

Trước đây prompt = persona (~11k ký tự) + CHAT_HISTORY_TURNS tin lịch sử không
cắt (mỗi tin có thể tới 4k token output) + trí nhớ (tới ~17k ký tự) + grounding
Tavily/tool/cổ phiếu không trần. Model free context 8k token lỗi hoặc cắt im
lặng. Module này ước lượng token (CHARS_PER_TOKEN ký tự/token) và cắt theo thứ
tự ít giá trị nhất trước: lịch sử cũ -> grounding -> trí nhớ. Prompt của người
dùng và persona không bao giờ bị cắt ở đây.

Tỉ lệ ký tự/token: tiếng Việt có dấu với tokenizer của gpt-oss/Gemini chỉ ~2,5-3
ký tự/token (đo bằng prompt_chars/prompt_tokens trong provider_calls); 4 ký
tự/token cũ đếm thiếu ~30% số token. Ngân sách đặt theo TOKEN rồi đổi ra ký tự
qua CHARS_PER_TOKEN, nên đổi tỉ lệ thì mọi trần tự đổi theo.

Groq free (gpt-oss: ~8K token/phút TÍNH CẢ max_tokens của request - vượt là 413
ngay, không tốn quota nhưng tốn 1 lượt và độ trễ): ``fit_groq_chat`` chọn persona
đầy đủ hay rút gọn + max_tokens + phần lịch sử vừa GROQ_TPM_BUDGET; không vừa thì
lượt đó bỏ qua Groq mà không gửi request nào.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from core import untrusted

CHARS_PER_TOKEN = 2.8
# Phần token không nằm trong nội dung: khung mỗi message và phần hệ thống provider tự
# chèn (gpt-oss: định danh model, ngày, mức suy luận...). Ước dư cho an toàn.
_MESSAGE_OVERHEAD_TOKENS = 4
_REQUEST_OVERHEAD_TOKENS = 64


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def chars_for_tokens(tokens: int) -> int:
    """Số ký tự tiếng Việt ứng với ``tokens`` token (theo CHARS_PER_TOKEN)."""
    return int(tokens * CHARS_PER_TOKEN)


# Tổng prompt gửi provider (persona + lịch sử + trí nhớ + grounding + câu hỏi):
# 10k token (28.000 ký tự) - vừa model 16k context còn chỗ cho output 4k, dư cho
# model 128k.
PROMPT_BUDGET_TOKENS = 10_000
PROMPT_BUDGET_CHARS = _env_int(
    "PROMPT_BUDGET_CHARS", chars_for_tokens(PROMPT_BUDGET_TOKENS), 12_000, 400_000
)
# Trần riêng từng khối (áp trước khi cân tổng).
HISTORY_MESSAGE_MAX_CHARS = _env_int("HISTORY_MESSAGE_MAX_CHARS", 1_500, 200, 20_000)
GROUNDING_MAX_CHARS = _env_int("GROUNDING_MAX_CHARS", 9_000, 1_000, 100_000)
MEMORY_MAX_CHARS = _env_int("MEMORY_MAX_CHARS", 3_000, 500, 50_000)

# ─── Groq free: ngân sách theo token/phút ────────────────────────────────────
# Trần token (prompt ước lượng + max_tokens) cho 1 request Groq: free tier gpt-oss
# ~8K token/phút; chừa ~1K cho sai số ước lượng. Tài khoản trả phí/model TPM cao
# hơn thì tăng; 0 = không kiểm (gửi mọi request như cũ).
GROQ_TPM_BUDGET = _env_int("GROQ_TPM_BUDGET", 7_000, 0, 1_000_000)
# max_tokens của lượt chat Groq: persona đầy đủ (vừa ngân sách) / persona rút gọn.
GROQ_FULL_MAX_TOKENS = 4096
GROQ_COMPACT_MAX_TOKENS = 1024
# Trần lịch sử riêng của Groq ~1.250 token (3.500 ký tự): persona rút gọn (~600
# token) + trí nhớ (tới ~1.100) + lịch sử + câu hỏi + max_tokens 1.024 còn ~3K token
# của phút đó cho router tool/viết lại truy vấn cùng lượt. fit_groq_chat còn cắt
# thêm cho vừa GROQ_TPM_BUDGET khi grounding dài.
GROQ_HISTORY_MAX_TOKENS = 1_250
GROQ_HISTORY_MAX_CHARS = _env_int(
    "GROQ_HISTORY_MAX_CHARS", chars_for_tokens(GROQ_HISTORY_MAX_TOKENS), 0, 100_000
)

_CUT_MARK = "\n[... đã lược bớt cho vừa ngân sách ...]\n"


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text or "") / CHARS_PER_TOKEN)


def _content_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(str(part.get("text") or "")) for part in content if isinstance(part, dict))
    return 0


def request_tokens(messages: list[dict[str, Any]], tools: Optional[list[Any]] = None) -> int:
    """Ước lượng token prompt của 1 request chuẩn OpenAI: nội dung chữ mọi message,
    tool_calls đã gửi lại, khai báo ``tools`` và phần khung (ảnh không tính)."""
    chars = 0
    for message in messages:
        chars += _content_chars(message.get("content"))
        for call in message.get("tool_calls") or ():
            chars += len(json.dumps(call, ensure_ascii=False))
    if tools:
        chars += len(json.dumps(tools, ensure_ascii=False))
    return (
        math.ceil(chars / CHARS_PER_TOKEN)
        + len(messages) * _MESSAGE_OVERHEAD_TOKENS
        + _REQUEST_OVERHEAD_TOKENS
    )


def chat_tokens(system: str, history: list[tuple[str, str]], prompt: str) -> int:
    """request_tokens của lượt chat (system + lịch sử + câu hỏi) - cùng cách đếm với
    chốt chặn ở openai_compatible.OpenAICompatibleProvider."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    messages += [{"role": role, "content": content} for role, content in history]
    messages.append({"role": "user", "content": prompt})
    return request_tokens(messages)


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


def _drop_leading_replies(history: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Lịch sử phải mở đầu bằng 1 tin người dùng: cắt mất câu hỏi mà còn câu trả lời
    thì model đọc như câu trả lời không đầu đuôi (Gemini còn đòi hỏi-đáp xen kẽ)."""
    start = 0
    while start < len(history) and history[start][0] != "user":
        start += 1
    return history[start:]


def trim_history(
    history: list[tuple[str, str]],
    budget_chars: int,
    *,
    message_max_chars: int = HISTORY_MESSAGE_MAX_CHARS,
) -> list[tuple[str, str]]:
    """Cắt từng tin về message_max_chars rồi bỏ tin CŨ nhất cho tới khi tổng
    vừa budget_chars; sau cùng bỏ các câu trả lời còn sót ở đầu (lịch sử luôn mở
    đầu bằng tin người dùng). Luôn giữ nguyên thứ tự cũ -> mới."""
    trimmed = [(role, truncate_tail(content, message_max_chars)) for role, content in history]
    total = sum(len(c) for _, c in trimmed)
    while trimmed and total > budget_chars:
        _, dropped = trimmed.pop(0)
        total -= len(dropped)
    return _drop_leading_replies(trimmed)


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


def history_for_provider(history: list[tuple[str, str]], provider: str) -> list[tuple[str, str]]:
    """Lịch sử đã cắt thêm theo trần riêng của provider (bỏ tin cũ nhất trước)."""
    if provider == "groq":
        return trim_history(history, GROQ_HISTORY_MAX_CHARS)
    return history


@dataclass(frozen=True)
class GroqFit:
    """Cách gửi 1 lượt chat qua Groq cho vừa GROQ_TPM_BUDGET."""

    system: str
    history: list[tuple[str, str]]
    max_tokens: int
    compact: bool
    tokens: int


def fit_groq_chat(
    *,
    system: str,
    history: list[tuple[str, str]],
    prompt: str,
    compact_system: Callable[[], str],
    budget: Optional[int] = None,
) -> Optional[GroqFit]:
    """Persona đầy đủ + max_tokens 4096 nếu vừa ngân sách (hiếm với persona 9-15k ký
    tự); không thì persona rút gọn (``compact_system()``, gọi lười) + max_tokens 1024
    và bỏ bớt lịch sử cũ cho vừa. Câu hỏi + trí nhớ + grounding (``prompt``) không
    cắt ở đây: tự chúng đã vượt ngân sách thì trả None - caller bỏ qua Groq lượt
    này, KHÔNG gửi request (gửi chỉ nhận 413). ``budget`` 0 = không kiểm."""
    budget = GROQ_TPM_BUDGET if budget is None else budget
    history = trim_history(history, GROQ_HISTORY_MAX_CHARS)
    full = chat_tokens(system, history, prompt)
    if budget <= 0 or full + GROQ_FULL_MAX_TOKENS <= budget:
        return GroqFit(system, history, GROQ_FULL_MAX_TOKENS, False, full)
    compact = compact_system() or system
    history = list(history)
    while True:
        tokens = chat_tokens(compact, history, prompt)
        if tokens + GROQ_COMPACT_MAX_TOKENS <= budget:
            return GroqFit(compact, history, GROQ_COMPACT_MAX_TOKENS, True, tokens)
        if not history:
            return None
        history = _drop_leading_replies(history[1:])
