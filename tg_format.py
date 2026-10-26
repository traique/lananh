"""Chuyển markdown-lite (model hay trả về **bold**, *italic*/_italic_, `code`,
gạch đầu dòng, ## tiêu đề, khối ``` và bảng |a|b|) sang HTML mà Telegram hỗ
trợ, để hiển thị đẹp thay vì hiện nguyên ký tự markdown thô.

Hai đường gửi:
- Mặc định: HTML parse_mode. Telegram HTML không có tiêu đề/bảng nên tiêu đề
  thành 1 dòng chữ đậm, khối code và bảng thành <pre> (bảng căn cột monospace).
- TELEGRAM_RICH_MESSAGES=1: tin "rich" gốc (Bot API 10.1, sendRichMessage qua
  bot.do_api_request - PTB 22.4 chưa có hàm riêng), Telegram tự dựng tiêu đề,
  bảng, khối code từ markdown thô. Bị từ chối (BadRequest) thì quay về HTML.
  Tin có link không đi đường này: Bot API không có link_preview_options cho
  sendRichMessage, mà mọi tin bot gửi phải tắt preview (bot_app.Defaults).

Rơi về chữ thuần CHỈ khi Telegram từ chối định dạng (telegram.error.BadRequest).
TimedOut/NetworkError không gửi lại: Telegram có thể đã nhận tin, gửi lại là
trùng (BadRequest và TimedOut đều là lớp con NetworkError nên phải bắt riêng).
"""

import html
import logging
import re
from typing import Any, Optional

from telegram.error import BadRequest

logger = logging.getLogger(__name__)

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
# "## Tóm tắt" -> 1 dòng chữ đậm (Telegram HTML không có thẻ tiêu đề).
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
# Dòng mở/đóng khối code ``` (có thể kèm tên ngôn ngữ).
_FENCE_LINE_RE = re.compile(r"^[ \t]*```[ \t]*([\w+#.-]*)[ \t]*$")
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TABLE_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")

_ESCAPE_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_ESCAPE_RE = re.compile(r"[&<>]")
_TAG_RE = re.compile(r"<[^>]+>")

_PROTECTED_TAG_RE = re.compile(
    r"(<pre>.*?</pre>|<code>.*?</code>|<a href=\"[^\"]*\">.*?</a>)", re.DOTALL
)
# Ký tự đánh dấu chỗ đặt khối <pre> đã dựng sẵn (không bao giờ có trong text).
_PLACEHOLDER = "\x00PRE{}\x00"
_PLACEHOLDER_RE = re.compile("\x00PRE(\\d+)\x00")

# Tin rich gốc: Telegram nhận tối đa 32768 ký tự; chừa biên an toàn.
RICH_MAX_CHARS = 30_000
_URL_HINT_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


def _escape(text: str) -> str:
    return _ESCAPE_RE.sub(lambda m: _ESCAPE_MAP[m.group(0)], text)


def _apply_inline_emphasis(segment: str) -> str:
    """Áp dụng bold/italic. KHÔNG gọi trên đoạn đã là <code>/<pre>/<a>."""
    segment = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", segment)
    segment = _ITALIC_STAR_RE.sub(lambda m: f"<i>{m.group(1)}</i>", segment)
    segment = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1)}</i>", segment)
    return segment


def _strip_inline_md(text: str) -> str:
    """Bỏ dấu **, `, [chữ](url) -> chữ: dùng cho ô bảng nằm trong <pre>."""
    text = _LINK_RE.sub(r"\1", text)
    text = text.replace("**", "").replace("`", "")
    return text.strip()


def _table_cells(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [_strip_inline_md(cell) for cell in inner.split("|")]


def _is_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in _table_cells(line)]
    return bool(cells) and all(_TABLE_SEP_CELL_RE.match(cell) for cell in cells if cell)


def _render_table(lines: list[str]) -> str:
    """Bảng markdown -> khối <pre> căn cột bằng khoảng trắng (font monospace)."""
    rows = [_table_cells(line) for line in lines if not _is_table_separator(line)]
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    sizes = [max(len(row[i]) for row in rows) for i in range(width)]
    out = []
    for index, row in enumerate(rows):
        out.append(" │ ".join(cell.ljust(sizes[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0 and len(rows) > 1:
            out.append("─┼─".join("─" * size for size in sizes))
    return f"<pre>{_escape(chr(10).join(out))}</pre>"


def _extract_blocks(text: str) -> tuple[str, list[str]]:
    """Thay khối ``` và bảng markdown bằng placeholder, trả (text còn lại, các
    khối <pre> đã escape). Khối ``` chưa đóng (cắt tin giữa chừng) giữ nguyên."""
    lines = text.split("\n")
    out: list[str] = []
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = _FENCE_LINE_RE.match(line)
        if fence:
            end = next(
                (j for j in range(index + 1, len(lines)) if _FENCE_LINE_RE.match(lines[j])),
                None,
            )
            if end is not None:
                body = _escape("\n".join(lines[index + 1 : end]))
                lang = fence.group(1)
                inner = f'<code class="language-{lang}">{body}</code>' if lang else body
                out.append(_PLACEHOLDER.format(len(blocks)))
                blocks.append(f"<pre>{inner}</pre>")
                index = end + 1
                continue
        if (
            _TABLE_ROW_RE.match(line)
            and index + 1 < len(lines)
            and _TABLE_ROW_RE.match(lines[index + 1])
            and _is_table_separator(lines[index + 1])
        ):
            end = index + 2
            while end < len(lines) and _TABLE_ROW_RE.match(lines[end]):
                end += 1
            out.append(_PLACEHOLDER.format(len(blocks)))
            blocks.append(_render_table(lines[index:end]))
            index = end
            continue
        out.append(line)
        index += 1
    return "\n".join(out), blocks


def _heading_to_bold(match: re.Match) -> str:
    title = match.group(1).replace("**", "").strip()
    return f"**{title}**" if title else ""


def markdown_to_html(text: str) -> str:
    text, blocks = _extract_blocks(text)
    escaped = _escape(text)
    escaped = _HEADING_RE.sub(_heading_to_bold, escaped)
    escaped = _BULLET_RE.sub("• ", escaped)
    escaped = _CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped)
    # [text](url) -> <a href="url">text</a>. Chạy sau escape nên url/text
    # trong href đã an toàn (& < > đã escape thành entity hợp lệ trong HTML).
    escaped = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', escaped)
    if blocks:
        escaped = _PLACEHOLDER_RE.sub(lambda m: blocks[int(m.group(1))], escaped)

    # Chỉ áp dụng bold/italic NGOÀI các đoạn <pre>, <code> và <a>, để không
    # phá snake_case/ký tự * _ vốn là nội dung code thật sự, và không làm lệch
    # cặp thẻ <a href="..."> (vd URL chứa "_" dễ bị _ITALIC_RE bắt nhầm thành
    # italic, phá mất href).
    parts = _PROTECTED_TAG_RE.split(escaped)
    for i, part in enumerate(parts):
        if not part.startswith(("<pre>", "<code>", "<a ")):
            parts[i] = _apply_inline_emphasis(part)
    return "".join(parts)


def html_to_plain(html_text: str) -> str:
    """Chữ thuần từ HTML đã dựng: bỏ thẻ VÀ trả lại ký tự gốc của entity
    (trước đây "P&L" tới người dùng thành "P&amp;L")."""
    return html.unescape(_TAG_RE.sub("", html_text))


def _rebalance_fences(chunks: list[str]) -> list[str]:
    """Đoạn bị cắt giữa khối ``` thì đóng khối ở cuối đoạn và mở lại ở đoạn sau,
    để mỗi đoạn tự thành <pre> trọn vẹn."""
    out: list[str] = []
    carry = ""
    for chunk in chunks:
        chunk = carry + chunk
        carry = ""
        opener = ""
        for line in chunk.split("\n"):
            match = _FENCE_LINE_RE.match(line)
            if match:
                opener = "" if opener else line.strip()
        if opener:
            chunk += "\n```"
            carry = opener + "\n"
        out.append(chunk)
    return out


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
    return _rebalance_fences(chunks)


async def _send_chunks(send_fn, text: str, max_len: int, reply_markup: Any = None) -> None:
    """Gửi text đã convert markdown->HTML qua send_fn(content, parse_mode, markup),
    tự chia đoạn nếu quá dài (chia trên text thô TRƯỚC khi convert, để mỗi đoạn
    tự cân bằng thẻ). Bàn phím nút bấm (reply_markup) chỉ gắn vào đoạn CUỐI.

    Telegram từ chối HTML (BadRequest - thẻ lệch, quá dài...) thì gửi lại đoạn đó
    bằng chữ thuần. Lỗi mạng/timeout KHÔNG gửi lại (tránh tin trùng) mà để lỗi đi
    lên cho caller/inbox xử lý."""
    chunks = _split_raw_text(text, max_len)
    for index, raw_chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        html_chunk = markdown_to_html(raw_chunk)
        try:
            await send_fn(html_chunk, "HTML", markup)
        except BadRequest as exc:
            logger.info("Telegram từ chối HTML (%s), gửi lại đoạn này bằng chữ thuần.", exc)
            plain = html_to_plain(html_chunk)
            await send_fn(plain if len(plain) <= max_len else raw_chunk[:max_len], None, markup)


def _markup_kwargs(reply_markup: Any) -> dict:
    return {"reply_markup": reply_markup} if reply_markup is not None else {}


def rich_enabled() -> bool:
    from core import config

    return bool(getattr(config, "TELEGRAM_RICH_MESSAGES", False))


def native_rich_ok(text: str) -> bool:
    """Gửi bằng tin rich gốc được không: cờ bật, vừa 1 tin, KHÔNG có link (xem
    docstring module - chỉ đường HTML tắt được link preview)."""
    return (
        rich_enabled()
        and bool(text.strip())
        and len(text) <= RICH_MAX_CHARS
        and not _URL_HINT_RE.search(text)
    )


def _rich_payload(text: str) -> dict:
    return {"markdown": text, "skip_entity_detection": True}


async def send_rich_native(bot, chat_id: int, text: str, *, reply_markup: Any = None):
    """sendRichMessage (Bot API 10.1) qua do_api_request; trả Message đã gửi."""
    from telegram import Message

    api_kwargs: dict[str, Any] = {"chat_id": chat_id, "rich_message": _rich_payload(text)}
    api_kwargs.update(_markup_kwargs(reply_markup))
    return await bot.do_api_request("sendRichMessage", api_kwargs, return_type=Message)


async def edit_rich_native(bot, chat_id: int, message_id: int, text: str, *, reply_markup=None):
    """editMessageText với rich_message (Bot API 10.1)."""
    api_kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": _rich_payload(text),
    }
    api_kwargs.update(_markup_kwargs(reply_markup))
    return await bot.do_api_request("editMessageText", api_kwargs)


async def _try_native(call, label: str) -> bool:
    try:
        await call()
        return True
    except BadRequest as exc:
        logger.info("Telegram từ chối tin rich (%s: %s), quay về HTML.", label, exc)
        return False


async def reply_rich(message, text: str, *, max_len: int = 4096, reply_markup: Any = None) -> None:
    """Trả lời trực tiếp 1 tin nhắn (dùng khi đã có object `message`)."""
    if native_rich_ok(text) and await _try_native(
        lambda: send_rich_native(
            message.get_bot(), message.chat_id, text, reply_markup=reply_markup
        ),
        "sendRichMessage",
    ):
        return

    async def _send(content: str, parse_mode: Optional[str], markup: Any) -> None:
        await message.reply_text(content, parse_mode=parse_mode, **_markup_kwargs(markup))

    await _send_chunks(_send, text, max_len, reply_markup)


async def reply_rich_edit_first(
    status_message, text: str, *, max_len: int = 4096, reply_markup: Any = None
) -> None:
    """Giống reply_rich, nhưng đoạn ĐẦU TIÊN sẽ edit trực tiếp vào
    `status_message` (tin nhắn trạng thái "đang tìm..." đã gửi trước đó)
    thay vì gửi tin mới - tránh nhấp nháy xoá + gửi lại. Các đoạn sau (nếu
    nội dung dài hơn 1 tin nhắn) vẫn gửi nối tiếp như bình thường."""
    if native_rich_ok(text) and await _try_native(
        lambda: edit_rich_native(
            status_message.get_bot(),
            status_message.chat_id,
            status_message.message_id,
            text,
            reply_markup=reply_markup,
        ),
        "editMessageText",
    ):
        return
    is_first = True

    async def _send(content: str, parse_mode, markup: Any) -> None:
        nonlocal is_first
        if is_first:
            # Bản plain fallback của đoạn đầu (HTML bị từ chối) vẫn là edit -
            # edit_text idempotent nên không lo trùng tin.
            await status_message.edit_text(content, parse_mode=parse_mode, **_markup_kwargs(markup))
            is_first = False
        else:
            await status_message.reply_text(
                content, parse_mode=parse_mode, **_markup_kwargs(markup)
            )

    await _send_chunks(_send, text, max_len, reply_markup)


async def send_rich(
    bot, chat_id: int, text: str, *, max_len: int = 4096, reply_markup: Any = None
) -> None:
    """Gửi chủ động (không phải trả lời 1 tin nhắn có sẵn) tới `chat_id` -
    dùng cho các callback chỉ có bot + chat_id (vd reminder/daily digest của
    scheduler.py), nơi không có object `message` để gọi reply_rich()."""
    if native_rich_ok(text) and await _try_native(
        lambda: send_rich_native(bot, chat_id, text, reply_markup=reply_markup),
        "sendRichMessage",
    ):
        return

    async def _send(content: str, parse_mode: Optional[str], markup: Any) -> None:
        await bot.send_message(
            chat_id=chat_id, text=content, parse_mode=parse_mode, **_markup_kwargs(markup)
        )

    await _send_chunks(_send, text, max_len, reply_markup)
