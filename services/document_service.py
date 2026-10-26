"""Đọc tài liệu người dùng gửi trong chat (PDF, TXT/MD, Word .docx) và trả lời
câu hỏi kèm theo (mặc định: tóm tắt).

- PDF: ``orchestrator.analyze_document`` - Gemini (AI Studio) đọc PDF native,
  dự phòng OpenRouter với engine file-parser MIỄN PHÍ (cloudflare-ai, không bao
  giờ engine tính tiền).
- TXT/MD/DOCX: tách chữ tại chỗ (DOCX bằng zipfile + ElementTree của thư viện
  chuẩn, có trần chống zip bomb, từ chối DOCTYPE/ENTITY), rào core.untrusted rồi
  hỏi qua provider-chain thường (``orchestrator.ask``).
- Chữ trong tài liệu là DỮ LIỆU ngoài (rào + luật "không làm theo mệnh lệnh").
- Lượt được lưu lịch sử "[tài liệu: <tên>] <câu hỏi>" (kind "document", không
  trích trí nhớ). File chỉ nằm trên đĩa tạm, nơi gọi xoá ngay sau lượt.
- Caption "lưu vào kho" (CHỈ chủ bot trên Telegram): chép tài liệu thành Markdown
  vào rag/tailieu/<tên>.md và đồng bộ lên GitHub bằng cơ chế của /ragxuly - kho
  rag/ vẫn là file .md (không bảng DB mới); tài liệu thành viên Zalo không bao
  giờ vào kho của chủ bot.

RAM: file <= DOCUMENT_MAX_BYTES (mặc định 5 MB); PDF đọc vào RAM 1 lần lúc gọi
provider (base64 ~1,33 lần), chữ tách ra có trần DOCUMENT_MAX_CHARS.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import re
import zipfile
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import messages
from ai import official_client, orchestrator
from ai.timeouts import TurnTimeoutError, turn_deadline
from core import config, untrusted
from core.envutil import env_int, env_on
from core.fileutil import safe_delete
from core.text_normalize import nfc
from services import conversation_log, media_text, reply_audience
from services.prompt_templates import DOCUMENT_TO_MARKDOWN_INSTRUCTION, build_document_instruction
from services.telemetry import telemetry

logger = logging.getLogger(__name__)

_MB = 1024 * 1024
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
KIND_PDF = "pdf"
KIND_TXT = "txt"
KIND_DOCX = "docx"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DEFAULT_QUESTION = "Tóm tắt ngắn gọn nội dung chính của tài liệu này."
_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".log"}
FILENAME_MAX_CHARS = 80
REPLY_MAX_CHARS = 8000
# Trần chống zip bomb cho DOCX: số mục trong zip, cỡ word/document.xml giải nén
# và tỉ lệ nén bất thường (XML chữ thường nén ~5-20 lần).
_DOCX_MAX_ENTRIES = 2000
_DOCX_XML_MAX_BYTES = 30 * _MB
_DOCX_MAX_RATIO = 120
_XML_PROLOG_SCAN_BYTES = 64 * 1024
# Trần số phần tử XML và độ lồng của word/document.xml (chặn file nhỏ toàn phần
# tử rỗng ngốn RAM/CPU); văn bản Word 100 trang thật còn dưới xa các mức này.
_DOCX_MAX_ELEMENTS = 1_000_000
_DOCX_MAX_DEPTH = 256
# Lưu vào kho: trần chữ của 1 file .md trong rag/ (rag_clean_service dùng 500k).
RAG_SAVE_MAX_CHARS = 300_000
RAG_SAVE_SUBDIR = "tailieu"
# Trần tổng dung lượng rag/tailieu/: kho rag/ được quét và lập chỉ mục trong RAM
# (rag_service), không để "lưu vào kho" phình vô hạn trên máy 512 MB.
RAG_SAVE_DIR_MAX_BYTES = 20 * _MB


def enabled() -> bool:
    return env_on("DOCUMENT_QA_ENABLED")


def max_bytes() -> int:
    # Trần cứng 8 MB = trần body /internal/zalo/file (services/request_limits.py).
    return env_int("DOCUMENT_MAX_BYTES", 5 * _MB, 64 * 1024, 8 * _MB)


def max_chars() -> int:
    """Trần ký tự chữ tách từ TXT/DOCX đưa vào 1 lượt hỏi (~10k token)."""
    return env_int("DOCUMENT_MAX_CHARS", 40_000, 2_000, 200_000)


class DocumentError(Exception):
    """kind in disabled|too_big|format|broken|empty|failed|kb_denied|kb_full."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


# ─── Nhận dạng + tách chữ ────────────────────────────────────────────────────


def display_name(filename: str) -> str:
    """Tên file để hiện/lưu lịch sử: bỏ đường dẫn, ký tự điều khiển, cắt ngắn."""
    name = nfc(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:FILENAME_MAX_CHARS] or "tai-lieu"


def detect_kind(filename: str, mime: str, head: bytes) -> str | None:
    """pdf | docx | txt theo magic bytes + đuôi/MIME; None = không hỗ trợ."""
    ext = Path(filename or "").suffix.lower()
    mime = (mime or "").split(";", 1)[0].strip().lower()
    if head.startswith(b"%PDF-"):
        return KIND_PDF
    if head.startswith(b"PK\x03\x04"):
        return KIND_DOCX if ext == ".docx" or mime == DOCX_MIME else None
    if ext in _TEXT_EXTENSIONS or mime.startswith("text/"):
        utf16 = head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
        if utf16 or b"\x00" not in head:
            return KIND_TXT
    return None


def decode_text(data: bytes) -> str:
    if data.startswith(codecs.BOM_UTF8):
        text = data[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace")
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = data.decode("utf-16", errors="replace")
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # File Windows cũ: cp1258 (Windows tiếng Việt) giải được mọi byte.
            text = data.decode("cp1258", errors="replace")
    return nfc(text.replace("\r\n", "\n").replace("\r", "\n"))


def _cut(text: str, limit: int) -> tuple[str, bool]:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return (text, False) if len(text) <= limit else (text[:limit], True)


class _CappedXml:
    """Luồng đọc word/document.xml có trần byte; từ chối DOCTYPE/ENTITY ở phần
    đầu (chặn tấn công mở rộng entity) - ElementTree không tự tải entity ngoài."""

    def __init__(self, raw, limit: int) -> None:
        self._raw = raw
        self._limit = limit
        self._read = 0
        self._prolog = b""

    def read(self, size: int = -1) -> bytes:
        chunk = self._raw.read(size if size and size > 0 else 64 * 1024)
        self._read += len(chunk)
        if self._read > self._limit:
            raise DocumentError("broken", "word/document.xml quá lớn")
        if len(self._prolog) < _XML_PROLOG_SCAN_BYTES:
            self._prolog += chunk[: _XML_PROLOG_SCAN_BYTES - len(self._prolog)]
            upper = self._prolog.upper()
            if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
                raise DocumentError("broken", "DOCX có DOCTYPE/ENTITY")
        return chunk


def extract_docx(path: Path, limit: int) -> tuple[str, bool]:
    """Chữ của word/document.xml (đoạn -> xuống dòng), cắt về ``limit`` ký tự."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _DOCX_MAX_ENTRIES:
                raise DocumentError("broken", "quá nhiều mục trong file zip")
            try:
                info = archive.getinfo("word/document.xml")
            except KeyError as exc:
                raise DocumentError("broken", "thiếu word/document.xml") from exc
            ratio = info.file_size / max(1, info.compress_size)
            if info.file_size > _DOCX_XML_MAX_BYTES or (
                info.file_size > _MB and ratio > _DOCX_MAX_RATIO
            ):
                raise DocumentError("broken", "tỉ lệ nén bất thường (zip bomb?)")
            with archive.open(info) as raw:
                return _docx_text(_CappedXml(raw, _DOCX_XML_MAX_BYTES), limit)
    except DocumentError:
        raise
    except (zipfile.BadZipFile, ElementTree.ParseError, OSError, ValueError) as exc:
        raise DocumentError("broken", f"{type(exc).__name__}: {exc}") from exc


def _docx_text(stream: _CappedXml, limit: int) -> tuple[str, bool]:
    """Không giữ cây XML: phần tử nào đã xong (sự kiện "end") thì xoá nội dung và
    gỡ khỏi phần tử cha (``del parent[:]`` - mọi anh em trước đó đã xử lý xong ở
    "end" của chúng), nên RAM chỉ cỡ độ lồng thay vì số phần tử."""
    parts: list[str] = []
    size = 0
    truncated = False
    stack: list[ElementTree.Element] = []
    count = 0
    for event, element in ElementTree.iterparse(stream, events=("start", "end")):
        if event == "start":
            count += 1
            if count > _DOCX_MAX_ELEMENTS:
                raise DocumentError("broken", "quá nhiều phần tử XML")
            stack.append(element)
            if len(stack) > _DOCX_MAX_DEPTH:
                raise DocumentError("broken", "XML lồng quá sâu")
            continue
        stack.pop()
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "t":
            piece = element.text or ""
        elif tag == "tab":
            piece = "\t"
        elif tag in ("br", "cr", "p"):
            piece = "\n"
        else:
            piece = ""
        element.clear()
        if stack:
            del stack[-1][:]
        if not piece:
            continue
        parts.append(piece)
        size += len(piece)
        if size > limit:
            truncated = True
            break
    text, cut = _cut("".join(parts), limit)
    return text, truncated or cut


def extract_text(path: Path, kind: str, limit: int) -> tuple[str, bool]:
    """(chữ đã tách, có bị cắt) cho TXT/DOCX - chạy trong thread (đọc đĩa, parse)."""
    if kind == KIND_DOCX:
        return extract_docx(path, limit)
    with open(path, "rb") as file:
        # Đọc dư 4 byte/ký tự là đủ cho `limit` ký tự UTF-8; phần sau bỏ qua.
        data = file.read(limit * 4 + 4)
    return _cut(decode_text(data), limit)


# ─── "Lưu vào kho" ──────────────────────────────────────────────────────────

_SAVE_RE = re.compile(
    r"\b(?:lưu|cất|luu|cat)\s+(?:(?:lại\s+|lai\s+)?(?:vào|vô|vao|vo)\s+)?"
    r"(?:kho(?:\s+kiến thức|\s+kien thuc)?|rag)\b|\bsave\s+(?:to\s+)?(?:rag|kb)\b",
    re.IGNORECASE,
)
_LEFTOVER_RE = re.compile(
    r"^[\s,.;:!\-–]*(?:(?:và|rồi|xong|va|roi|nhé|nha|giúp|giùm)\b)?[\s,.;:!]*"
)


def split_save_request(caption: str) -> tuple[bool, str]:
    """(có yêu cầu lưu vào kho, phần câu hỏi còn lại)."""
    caption = nfc(caption or "").strip()
    if not _SAVE_RE.search(caption):
        return False, caption
    rest = _SAVE_RE.sub(" ", caption)
    rest = _LEFTOVER_RE.sub("", " ".join(rest.split())).strip(" ,.;:!")
    return True, rest if len(rest.split()) >= 2 else ""


def _slug(filename: str) -> str:
    from services.rag_service import strip_vn_diacritics

    stem = Path(filename).stem
    slug = re.sub(r"[^a-z0-9]+", "-", strip_vn_diacritics(stem)).strip("-")
    return slug[:60].strip("-") or "tai-lieu"


def _write_rag_file(filename: str, markdown: str) -> Path:
    from services.rag_service import RAG_DIR

    folder = RAG_DIR / RAG_SAVE_SUBDIR
    folder.mkdir(parents=True, exist_ok=True)
    used = sum(item.stat().st_size for item in folder.glob("*.md") if item.is_file())
    if used + len(markdown.encode("utf-8")) > RAG_SAVE_DIR_MAX_BYTES:
        raise DocumentError("kb_full")
    base = _slug(filename)
    path = folder / f"{base}.md"
    for index in range(2, 100):
        if not path.exists():
            break
        path = folder / f"{base}-{index}.md"
    else:
        path = folder / f"{base}-{datetime.now(_VN_TZ):%Y%m%d%H%M%S}.md"
    title = Path(filename).stem or base
    today = datetime.now(_VN_TZ).strftime("%d/%m/%Y")
    path.write_text(
        f"# {title}\n\n> Nguồn: file {filename}, lưu vào kho ngày {today}.\n\n{markdown}\n",
        encoding="utf-8",
    )
    return path


_SAVED_TEMPLATE = "📚 {Ai} lưu vào kho rồi nè: {path} ({chars} ký tự).{sync}"


async def save_to_kb(user_id: int, path: Path, filename: str, kind: str) -> str:
    """Chép tài liệu thành Markdown vào rag/tailieu/ + đồng bộ GitHub. CHỈ chủ bot
    (ALLOWED_USER_ID) - Zalo (id âm) luôn bị từ chối."""
    from services import rag_clean_service, rag_service

    if not config.is_owner_user_id(user_id):
        raise DocumentError("kb_denied")
    truncated = False
    if kind == KIND_PDF:
        with orchestrator.allow_cjk_output():
            response = await orchestrator.analyze_document(
                DOCUMENT_TO_MARKDOWN_INSTRUCTION, str(path), filename=filename
            )
        markdown = (getattr(response, "text", None) or "").strip()[:RAG_SAVE_MAX_CHARS]
    else:
        markdown, truncated = await asyncio.to_thread(extract_text, path, kind, RAG_SAVE_MAX_CHARS)
    markdown = untrusted.sanitize(markdown).strip()
    if not markdown:
        raise DocumentError("empty")
    saved = await asyncio.to_thread(_write_rag_file, filename, markdown)
    rag_service.clear_index_cache()
    content = await asyncio.to_thread(saved.read_text, encoding="utf-8")
    try:
        synced, detail = await rag_clean_service.sync_file_to_github(saved, content, action="kho")
    except Exception as exc:
        logger.warning("Đồng bộ file kho lên GitHub lỗi.", exc_info=True)
        synced, detail = False, messages.exception_detail(exc, limit=200)
    sync = (
        f"\n☁️ Đã đồng bộ {detail}."
        if synced
        else "\n⚠️ Chưa đồng bộ GitHub nên file mất khi máy chủ khởi động lại "
        f"({detail}) - cấu hình GITHUB_RAG_TOKEN để giữ lâu dài."
    )
    if truncated:
        sync += f"\n✂️ Tài liệu dài nên chỉ lưu {RAG_SAVE_MAX_CHARS:,} ký tự đầu.".replace(",", ".")
    if kind == KIND_PDF:
        sync += "\nℹ️ Bản này do AI chép lại từ PDF - tài liệu rất dài có thể thiếu phần cuối."
    return media_text.say(
        _SAVED_TEMPLATE,
        owner=True,
        path=rag_clean_service._display_path(saved),
        chars=f"{len(content):,}".replace(",", "."),
        sync=sync,
    )


# ─── Hỏi đáp ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DocumentReply:
    text: str
    used_fallback: bool = False


_ERROR_TEMPLATES = {
    "disabled": "📄 {Ai} chưa được bật đọc tài liệu, dán nội dung dạng chữ giúp {ai} nha{tail}.",
    "too_big": (
        "📄 File lớn quá (tối đa {max_mb} MB) nên {ai} chưa đọc được. Gửi bản nhỏ hơn hoặc "
        "tách file giúp {ai} nha{tail}."
    ),
    "format": (
        "📄 {Ai} mới đọc được file PDF, TXT/MD và Word (.docx) thôi. Gửi lại đúng định dạng "
        "giúp {ai} nha{tail}."
    ),
    "broken": (
        "📄 File này hình như bị hỏng hoặc có cấu trúc lạ nên {ai} không mở được. Gửi lại "
        "bản khác giúp {ai} nha{tail}."
    ),
    "empty": (
        "📄 {Ai} không thấy chữ nào đọc được trong file này. Gửi bản có chữ (không phải ảnh "
        "scan) hoặc chụp ảnh từng trang giúp {ai} nha{tail}."
    ),
    "failed": (
        "😥 Bên AI đang bận nên {ai} chưa đọc được tài liệu này. Thử lại sau ít phút giúp "
        "{ai} nha{tail}."
    ),
    "kb_full": (
        "📚 Kho tài liệu rag/tailieu/ đã đầy (tối đa {kb_mb} MB). Xoá bớt file cũ rồi lưu lại "
        "giúp {ai} nha{tail}."
    ),
    "kb_denied": (
        "📚 Lưu vào kho kiến thức chỉ làm được từ Telegram của chủ bot - {ai} chỉ đọc và trả "
        "lời về tài liệu này thôi nha{tail}."
    ),
}
_TRUNCATED_NOTE = "\n\n✂️ Tài liệu dài nên {ai} chỉ đọc khoảng {chars} ký tự (phần đầu và cuối)."
_TRANSLATE_RE = re.compile(r"\bdịch\b|\bdich\b|translate", re.IGNORECASE)


async def error_text(kind: str, user_id: int, detail: str = "") -> str:
    return await media_text.say_to(
        _ERROR_TEMPLATES.get(kind, _ERROR_TEMPLATES["failed"]),
        user_id,
        detail=detail,
        max_mb=max(1, max_bytes() // _MB),
        kb_mb=RAG_SAVE_DIR_MAX_BYTES // _MB,
    )


async def _answer(
    user_id: int, path: Path, filename: str, kind: str, question: str, channel: str
) -> DocumentReply:
    you, me, owner = media_text.persona_forms(user_id)
    time_line = official_client.now_vn_context()
    prompt_id = await telemetry.start(
        user_id, "document", f"[{filename}] {question}"[:500], channel=channel
    )
    truncated = False
    try:
        if kind == KIND_PDF:
            instruction = build_document_instruction(
                question, filename=filename, you=you, me=me, time_line=time_line
            )
            translate = bool(_TRANSLATE_RE.search(question))
            with orchestrator.allow_cjk_output() if translate else nullcontext():
                response = await orchestrator.analyze_document(
                    instruction, str(path), filename=filename
                )
        else:
            limit = max_chars()
            text, truncated = await asyncio.to_thread(extract_text, path, kind, limit)
            if not text.strip():
                raise DocumentError("empty")
            block = untrusted.wrap_untrusted(text, f"tài liệu {filename}", max_chars=limit)
            instruction = build_document_instruction(
                question,
                filename=filename,
                you=you,
                me=me,
                time_line=time_line,
                document_block=f"{untrusted.SYSTEM_RULE}\n\n{block}",
            )
            response = await orchestrator.ask(instruction)
    except (DocumentError, TurnTimeoutError) as exc:
        await telemetry.failure(prompt_id, "document", exc)
        raise
    except Exception as exc:
        await telemetry.failure(prompt_id, "document", exc)
        raise DocumentError("failed", messages.exception_detail(exc, limit=250)) from exc
    reply = (getattr(response, "text", None) or "").strip()[:REPLY_MAX_CHARS]
    await telemetry.success(prompt_id, "document", reply or "(không có nội dung)")
    if not reply:
        raise DocumentError("empty")
    if truncated:
        reply += media_text.say(
            _TRUNCATED_NOTE,
            owner=owner,
            address_form=reply_audience.current().address_form,
            chars=f"{max_chars():,}".replace(",", "."),
        )
    await conversation_log.record_exchange(
        user_id, f"[tài liệu: {filename}] {question}", reply, conversation_log.KIND_DOCUMENT
    )
    return DocumentReply(reply, bool(getattr(response, "used_fallback", False)))


def _read_head(path: Path) -> tuple[int, bytes]:
    with open(path, "rb") as file:
        return path.stat().st_size, file.read(16)


async def handle_file(
    user_id: int,
    path: Path,
    *,
    filename: str,
    mime: str = "",
    caption: str = "",
    channel: str = "telegram",
) -> DocumentReply:
    """Trả lời về 1 tài liệu đã nằm trên đĩa (nơi gọi xoá file sau đó). Không
    raise: lỗi -> câu giọng Lan Anh theo người nhận (chủ bot thấy chi tiết)."""
    name = display_name(filename)
    if not enabled():
        return DocumentReply(await error_text("disabled", user_id))
    try:
        size, head = await asyncio.to_thread(_read_head, path)
    except OSError as exc:
        return DocumentReply(await error_text("broken", user_id, str(exc)))
    if size > max_bytes():
        return DocumentReply(await error_text("too_big", user_id))
    kind = detect_kind(name, mime, head)
    if kind is None:
        return DocumentReply(await error_text("format", user_id))
    wants_save, question = split_save_request(caption)
    parts: list[str] = []
    used_fallback = False
    # MỘT hạn chót cho cả lượt (lưu kho + trả lời, mọi provider cộng dồn).
    with turn_deadline():
        try:
            if wants_save and config.is_owner_user_id(user_id):
                try:
                    parts.append(await save_to_kb(user_id, path, name, kind))
                except DocumentError as exc:
                    parts.append(await error_text(exc.kind, user_id, exc.detail))
                if not question:
                    return DocumentReply("\n\n".join(parts))
            elif wants_save:
                # Thành viên/Zalo: không bao giờ vào kho của chủ bot, vẫn trả lời.
                parts.append(await error_text("kb_denied", user_id))
            answer = await _answer(user_id, path, name, kind, question or DEFAULT_QUESTION, channel)
            parts.append(answer.text)
            used_fallback = answer.used_fallback
        except DocumentError as exc:
            parts.append(await error_text(exc.kind, user_id, exc.detail))
        except TurnTimeoutError as exc:
            parts.append(await reply_audience.error_text("timeout", user_id, str(exc)))
    return DocumentReply("\n\n".join(parts), used_fallback)


def safe_extension(filename: str, mime: str = "") -> str:
    """Đuôi file tạm an toàn (chỉ .pdf/.txt/.docx/.bin) - không dùng tên người gửi."""
    ext = Path(filename or "").suffix.lower()
    if ext in (".pdf", ".docx", ".txt", ".md"):
        return ext
    if (mime or "").startswith("text/"):
        return ".txt"
    return ".bin"


async def handle_channel_file(
    user_id: int,
    data: bytes,
    *,
    filename: str,
    content_type: str,
    caption: str,
    message_id: str,
) -> tuple[list[str], str | None]:
    """Zalo (POST /internal/zalo/file): ghi bytes ra file tạm, trả lời, xoá file."""
    if len(data) > max_bytes():
        return [await error_text("too_big", user_id)], None
    config.ensure_media_dir()
    safe_id = "".join(ch for ch in message_id if ch.isalnum())[:48] or "file"
    path = config.MEDIA_DIR / f"zalo_doc_{safe_id}{safe_extension(filename, content_type)}"
    try:
        await asyncio.to_thread(path.write_bytes, data)
        reply = await handle_file(
            user_id, path, filename=filename, mime=content_type, caption=caption, channel="zalo"
        )
    finally:
        await safe_delete(path)
    return [reply.text], "api" if reply.used_fallback else None
