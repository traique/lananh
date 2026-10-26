"""Ngữ cảnh phụ cho lượt chat AI (services/channel_chat_service.run_chat_turn).

- ``quote_grounding``: tin người dùng đang trả lời/trích dẫn (Telegram
  reply_to_message/quote, Zalo quote_text) - rào như dữ liệu ngoài, KHÔNG gộp
  vào lời người dùng.
"""

from __future__ import annotations

from core import untrusted

QUOTE_MAX_CHARS = 600
_QUOTE_HEADER = (
    "[Người dùng đang trả lời/trích dẫn tin nhắn dưới đây - hiểu câu hỏi theo đúng tin đó]"
)


def clip_quote(text: str | None) -> str | None:
    """Chuẩn hoá tin trích dẫn: rỗng -> None, cắt QUOTE_MAX_CHARS ký tự."""
    text = (text or "").strip()
    return text[:QUOTE_MAX_CHARS] if text else None


def quote_grounding(quote_text: str | None) -> str:
    quote = clip_quote(quote_text)
    if not quote:
        return ""
    fenced = untrusted.wrap_untrusted(quote, "tin được trích dẫn", max_chars=QUOTE_MAX_CHARS)
    return f"{_QUOTE_HEADER}\n{fenced}" if fenced else ""
