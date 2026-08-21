"""Gửi text nguyên văn trong khối <pre> của Telegram.

Vì sao tách khỏi tg_format.py: tg_format.reply_rich() convert markdown-lite
sang HTML inline (<b>, <i>, <code>). Cách đó KHÔNG bao giờ sinh ra <pre> -
mà Telegram chỉ hiển thị nút \"Copy\" cho khối <pre>. Tệ hơn, prompt tạo ảnh
tiếng Anh chứa rất nhiều dấu * và _ (vd \"--ar 4:5\", \"snake_case\") nên bị
_ITALIC_STAR_RE / _ITALIC_RE nuốt mất ký tự, khiến prompt chép ra KHÁC với
bản Gemini trả về.

Ở đây text được escape thẳng rồi bọc <pre>, không đụng gì tới markdown.
"""
import logging
from typing import List

logger = logging.getLogger(__name__)

_PRE_OPEN = "<pre>"
_PRE_CLOSE = "</pre>"

# Chi phí ký tự sau khi escape HTML: & -> &amp; (5), < -> &lt; (4), > -> &gt; (4).
# Dùng để đo độ dài THẬT của chunk sau escape, tránh vượt 4096 ký tự của
# Telegram khi text chứa nhiều ký tự đặc biệt.
_ESCAPE_COST = {"&": 5, "<": 4, ">": 4}
_ESCAPE_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}

# Chừa chỗ cho thẻ <pre></pre> và một ít biên an toàn.
_TAG_OVERHEAD = len(_PRE_OPEN) + len(_PRE_CLOSE) + 16


def escape_pre(text: str) -> str:
    return "".join(_ESCAPE_MAP.get(ch, ch) for ch in text)


def escaped_len(text: str) -> int:
    return sum(_ESCAPE_COST.get(ch, 1) for ch in text)


def split_for_code_block(text: str, max_len: int = 4096) -> List[str]:
    """Chia text thô thành các đoạn mà SAU KHI escape + bọc <pre> vẫn nằm
    trong max_len. Ưu tiên cắt ở ranh giới xuống dòng để prompt không bị
    đứt giữa câu; dòng đơn quá dài thì cắt cứng theo ký tự."""
    budget = max(200, max_len - _TAG_OVERHEAD)
    chunks: List[str] = []
    buf: List[str] = []
    buf_len = 0

    for ch in text:
        cost = _ESCAPE_COST.get(ch, 1)
        if buf_len + cost > budget:
            piece = "".join(buf)
            cut = piece.rfind("\n")
            if cut > len(piece) // 2:
                chunks.append(piece[:cut])
                rest = piece[cut + 1:]
            else:
                chunks.append(piece)
                rest = ""
            buf = list(rest)
            buf_len = escaped_len(rest)
        buf.append(ch)
        buf_len += cost

    if buf:
        chunks.append("".join(buf))

    return [c for c in chunks if c.strip()]


def wrap(chunk: str) -> str:
    return f"{_PRE_OPEN}{escape_pre(chunk)}{_PRE_CLOSE}"


async def reply_code_block(message, text: str, *, max_len: int = 4096) -> None:
    """Trả lời bằng 1 hoặc nhiều khối <pre> - Telegram sẽ hiện nút Copy trên
    từng khối. Nếu Telegram từ chối parse HTML thì rơi về plain text (mất nút
    Copy nhưng vẫn giữ đúng nội dung)."""
    for chunk in split_for_code_block(text, max_len):
        try:
            await message.reply_text(wrap(chunk), parse_mode="HTML")
        except Exception:
            logger.warning("Telegram từ chối khối <pre>, rơi về plain text", exc_info=True)
            await message.reply_text(chunk, parse_mode=None)
