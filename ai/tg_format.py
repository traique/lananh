"""Chuyển markdown-lite (Gemini hay trả về **bold**, *italic*/_italic_, `code`,
gạch đầu dòng) sang HTML mà Telegram hỗ trợ, để hiển thị đẹp thay vì hiện
nguyên ký tự markdown thô."""
import re
from typing import Optional

_CODE_RE = re.compile(r"`([^`\n]+?)`")
# [text](url) - link markdown chuẩn, chỉ nhận http/https để tránh javascript:
# hay các scheme khác lọt vào href.
_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
# *italic* một dấu sao - chạy SAU _BOLD_RE nên không còn cặp ** nào sót lại
# để bị nuốt nhầm.
_ITALIC_STAR_RE = re.compile(r"\*(\S(?:.*?\S)?|\S)\*")
# _italic_ - chỉ khớp khi 2 đầu dấu "_" là ranh giới từ (không liền chữ/số/
# gạch dưới khác), để không phá các chuỗi snake_case như "foreign_net_vol"
# hay "auto_close".
_ITALIC_RE = re.compile(r"(?<![\w])_(?!_)(?!\s)(.+?)(?<!\s)(?<!_)_(?![\w])")
_BULLET_RE = re.compile(r"^[ \t]*[-*][ \t]+", re.MULTILINE)

_ESCAPE_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_ESCAPE_RE = re.compile(r"[&<>]")

_PROTECTED_TAG_RE = re.compile(r"(<code>.*?</code>|<a href=\"[^\"]*\">.*?</a>)", re.DOTALL)


def _escape(text: str) -> str:
    return _ESCAPE_RE.sub(lambda m: _ESCAPE_MAP[m.group(0)], text)


def _apply_inline_emphasis(segment: str) -> str:
    """Áp dụng bold/italic. KHÔNG gọi trên đoạn đã là <code>...</code>."""
    segment = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", segment)
    segment = _ITALIC_STAR_RE.sub(lambda m: f"<i>{m.group(1)}</i>", segment)
    segment = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1)}</i>", segment)
    return segment


def markdown_to_html(text: str) -> str:
    escaped = _escape(text)
    escaped = _BULLET_RE.sub("• ", escaped)
    escaped = _CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped)
    # [text](url) -> <a href="url">text</a>. Chạy sau escape nên url/text
    # trong href đã an toàn (& < > đã escape thành entity hợp lệ trong HTML).
    escaped = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', escaped)

    # Chỉ áp dụng bold/italic NGOÀI các đoạn <code>...</code> và <a>...</a>,
    # để không phá snake_case/ký tự * _ vốn là nội dung code thật sự, và
    # không làm lệch cặp thẻ <a href="..."> (vd URL chứa "_" dễ bị
    # _ITALIC_RE bắt nhầm thành italic, phá mất href).
    parts = _PROTECTED_TAG_RE.split(escaped)
    for i, part in enumerate(parts):
        if not (part.startswith("<code>") or part.startswith("<a ")):
            parts[i] = _apply_inline_emphasis(part)
    return "".join(parts)


def _split_raw_text(text: str, max_len: int) -> list[str]:
    """Chia text THÔ (chưa convert markdown) thành nhiều đoạn tại ranh giới
    đoạn văn/dòng/khoảng trắng, mỗi đoạn sau đó tự cân bằng thẻ khi convert
    markdown->HTML riêng - tránh cắt ngang giữa cặp **bold**/`code` làm lệch
    thẻ HTML và khiến Telegram từ chối parse cả đoạn.

    Dùng ngưỡng nhỏ hơn max_len thật để chừa chỗ cho các thẻ HTML sẽ được
    thêm vào lúc convert (<b>, <code>,...).
    """
    raw_max = max(500, int(max_len * 0.85))
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= raw_max:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, raw_max)
        if split_at <= 0:
            split_at = remaining.rfind("\n", 0, raw_max)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, raw_max)
        if split_at <= 0:
            split_at = raw_max
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    return chunks


async def _send_chunks(send_fn, text: str, max_len: int) -> None:
    """Gửi text đã convert markdown->HTML qua send_fn(content, parse_mode),
    tự chia đoạn nếu quá dài (chia trên text thô TRƯỚC khi convert, để mỗi
    đoạn tự cân bằng thẻ), và tự rơi về plain text nếu HTML bị lỗi thẻ
    (Gemini đôi khi sinh ký tự lệch cặp)."""
    for raw_chunk in _split_raw_text(text, max_len):
        html_chunk = markdown_to_html(raw_chunk)
        try:
            await send_fn(html_chunk, "HTML")
        except Exception:
            await send_fn(re.sub(r"<[^>]+>", "", html_chunk), None)


async def reply_rich(message, text: str, *, max_len: int = 4096) -> None:
    """Trả lời trực tiếp 1 tin nhắn (dùng khi đã có object `message`)."""
    async def _send(content: str, parse_mode: Optional[str]) -> None:
        await message.reply_text(content, parse_mode=parse_mode)

    await _send_chunks(_send, text, max_len)


async def reply_rich_edit_first(status_message, text: str, *, max_len: int = 4096) -> None:
    """Giống reply_rich, nhưng đoạn ĐẦU TIÊN sẽ edit trực tiếp vào
    `status_message` (tin nhắn trạng thái "đang tìm..." đã gửi trước đó)
    thay vì gửi tin mới - tránh nhấp nháy xoá + gửi lại. Các đoạn sau (nếu
    nội dung dài hơn 1 tin nhắn) vẫn gửi nối tiếp như bình thường."""
    is_first = True

    async def _send(content: str, parse_mode) -> None:
        nonlocal is_first
        if is_first:
            is_first = False
            await status_message.edit_text(content, parse_mode=parse_mode)
        else:
            await status_message.reply_text(content, parse_mode=parse_mode)

    await _send_chunks(_send, text, max_len)


async def send_rich(bot, chat_id: int, text: str, *, max_len: int = 4096) -> None:
    """Gửi chủ động (không phải trả lời 1 tin nhắn có sẵn) tới `chat_id` -
    dùng cho các callback chỉ có bot + chat_id (vd reminder/daily digest của
    scheduler.py), nơi không có object `message` để gọi reply_rich()."""
    async def _send(content: str, parse_mode: Optional[str]) -> None:
        await bot.send_message(chat_id=chat_id, text=content, parse_mode=parse_mode)

    await _send_chunks(_send, text, max_len)
