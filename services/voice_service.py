"""Tin nhắn thoại -> chữ (speech-to-text) rồi đi tiếp như 1 tin nhắn chữ.

- ``transcribe()``: kiểm trần kích thước/thời lượng, nhận định dạng bằng magic
  bytes (không tin Content-Type), gọi ``orchestrator.transcribe_audio`` (Groq
  Whisper, dự phòng Gemini nghe audio - AAC thô chỉ Gemini nhận), lọc câu
  "ảo giác" quen thuộc của Whisper khi đoạn ghi âm im lặng.
- ``handle_channel_voice()``: điểm vào cho Zalo (POST /internal/zalo/voice):
  chép lời rồi gọi ``handle_channel_text(..., allow_side_effects=False)``.
  Telegram làm tương tự trong handlers/voice_handler.py.

An toàn: chữ chép từ tin thoại có thể nghe nhầm số ("120" thành "1200"), nên
lượt này CHỈ ĐỌC - không ghi danh mục, chi tiêu, nhắc việc, ghi chú hay chạy lệnh
"/" (allow_side_effects=False); trả lời, tra giá, phân tích cổ phiếu vẫn chạy.
Câu "🎙️ Em nghe là: ..." luôn gửi trước để người dùng thấy em nghe đúng chưa.

Hạn chót: caller bọc CẢ bước nghe lẫn lượt chat sau đó trong MỘT turn_deadline()
(bounded() không có ngân sách lượt nếu ngoài turn_deadline). RAM: tối đa 1 đoạn
ghi âm <= VOICE_MAX_BYTES mỗi lượt (bản base64 cho Gemini ~1,33 lần).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta

import messages
from ai import orchestrator
from ai.timeouts import TurnTimeoutError, turn_deadline
from core import database as db
from core.envutil import env_int, env_on
from core.text_normalize import fold
from services import channel_chat_service, conversation_log, media_text, reply_audience
from services.telemetry import telemetry

logger = logging.getLogger(__name__)

_MB = 1024 * 1024
# Đoạn ghi âm 3 phút OGG/Opus hoặc m4a 32-64 kbps chỉ ~0,7-1,5 MB: 5 MB là dư.
DEFAULT_MAX_BYTES = 5 * _MB
DEFAULT_MAX_SECONDS = 180
# Groq giới hạn prompt 224 token: giữ gợi ý từ vựng thật ngắn.
PROMPT_MAX_CHARS = 300
_SYMBOLS_TIMEOUT_SEC = 2.0
_MAX_PROMPT_SYMBOLS = 15
TRANSCRIPT_MAX_CHARS = 4000
_ECHO_MAX_CHARS = 600


def enabled() -> bool:
    return env_on("VOICE_STT_ENABLED")


def max_bytes() -> int:
    # Trần cứng 8 MB = trần body của /internal/zalo/voice (services/request_limits.py).
    return env_int("VOICE_MAX_BYTES", DEFAULT_MAX_BYTES, 64 * 1024, 8 * _MB)


def max_seconds() -> int:
    return env_int("VOICE_MAX_SECONDS", DEFAULT_MAX_SECONDS, 10, 600)


def language() -> str:
    """Mã ngôn ngữ ISO-639-1 gửi Whisper; rỗng/"auto" = để Whisper tự nhận."""
    value = os.getenv("VOICE_STT_LANGUAGE", "vi").strip().lower()
    return "" if value in ("", "auto") else value


# ─── Định dạng âm thanh ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class AudioFormat:
    name: str
    extension: str
    # MIME gửi Groq (None = Groq không nhận, vd AAC thô) và gửi Gemini.
    groq_mime: str | None
    gemini_mime: str | None


FORMATS = {
    "ogg": AudioFormat("ogg", "ogg", "audio/ogg", "audio/ogg"),  # Telegram voice (Opus)
    "m4a": AudioFormat("m4a", "m4a", "audio/mp4", "audio/m4a"),  # Zalo voice, iPhone
    "aac": AudioFormat("aac", "aac", None, "audio/aac"),  # AAC thô (ADTS)
    "mp3": AudioFormat("mp3", "mp3", "audio/mpeg", "audio/mp3"),
    "wav": AudioFormat("wav", "wav", "audio/wav", "audio/wav"),
    "flac": AudioFormat("flac", "flac", "audio/flac", "audio/flac"),
    "webm": AudioFormat("webm", "webm", "audio/webm", "audio/webm"),
}


def sniff(data: bytes) -> AudioFormat | None:
    """Nhận định dạng theo magic bytes; None = không phải âm thanh/không hỗ trợ
    (vd AMR - cả Groq lẫn Gemini đều không nhận)."""
    head = data[:16]
    if head.startswith(b"OggS"):
        return FORMATS["ogg"]
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return FORMATS["m4a"]
    if head.startswith(b"ID3"):
        return FORMATS["mp3"]
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return FORMATS["wav"]
    if head.startswith(b"fLaC"):
        return FORMATS["flac"]
    if head.startswith(b"\x1aE\xdf\xa3"):
        return FORMATS["webm"]
    if len(head) >= 2 and head[0] == 0xFF:
        # ADTS (AAC): 12 bit đồng bộ 0xFFF + 2 bit layer luôn 00; MP3: 11 bit đồng
        # bộ + layer khác 00.
        if head[1] & 0xF6 == 0xF0:
            return FORMATS["aac"]
        if head[1] & 0xE0 == 0xE0 and head[1] & 0x06:
            return FORMATS["mp3"]
    return None


def duration_seconds(value: object) -> float | None:
    """Thời lượng Telegram (PTB 22: int hoặc timedelta tuỳ PTB_TIMEDELTA) -> giây."""
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def limit_error(size: int | None, duration: float | None) -> str | None:
    """ "too_big" | "too_long" | None - kiểm TRƯỚC khi tải file về."""
    if size is not None and size > max_bytes():
        return "too_big"
    if duration is not None and duration > max_seconds():
        return "too_long"
    return None


# ─── Chép lời ────────────────────────────────────────────────────────────────


class VoiceError(Exception):
    """Không có bản chép lời: kind in disabled|too_big|too_long|format|empty|failed|download."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


# Câu Whisper hay "nghe" ra từ đoạn ghi âm im lặng/ồn (học từ phụ đề YouTube
# tiếng Việt) - cả bản chép lời chỉ có vậy thì coi như không nghe ra lời.
_HALLUCINATION_RE = re.compile(
    r"subscribe|dang ky kenh|ghien mi go|cam on cac ban da (?:theo doi|xem|lang nghe)|"
    r"hen gap lai cac ban|video tiep theo|like va share|nhan chuong thong bao"
)
_HALLUCINATION_MAX_CHARS = 160


def clean_transcript(text: str) -> str:
    text = unicodedata.normalize("NFC", " ".join((text or "").split())).strip()
    if not text:
        return ""
    if len(text) <= _HALLUCINATION_MAX_CHARS and _HALLUCINATION_RE.search(fold(text)):
        logger.info("Bỏ bản chép lời nghi là ảo giác của Whisper: %r", text[:80])
        return ""
    return text[:TRANSCRIPT_MAX_CHARS]


async def _user_symbols(user_id: int) -> list[str]:
    holdings = await db.portfolio_list_holdings(user_id)
    symbols = [str(row["symbol"]) for row in holdings or []]
    for symbol in await db.portfolio_watchlist(user_id) or []:
        if symbol not in symbols:
            symbols.append(str(symbol))
    return symbols[:_MAX_PROMPT_SYMBOLS]


async def vocabulary_prompt(user_id: int | None) -> str:
    """Gợi ý từ vựng cho Whisper: tên trợ lý + mã cổ phiếu người này đang giữ/theo
    dõi (Whisper hay nghe "Ép Pê Tê" thay vì FPT). DB chậm/lỗi -> bỏ phần mã."""
    prompt = "Tin nhắn thoại tiếng Việt gửi trợ lý Lan Anh"
    if user_id is not None:
        try:
            symbols = await asyncio.wait_for(_user_symbols(user_id), _SYMBOLS_TIMEOUT_SEC)
        except Exception:
            logger.debug("Không đọc được mã cổ phiếu cho gợi ý STT.", exc_info=True)
            symbols = []
        if symbols:
            prompt += f". Mã cổ phiếu: {', '.join(symbols)}"
    return prompt[:PROMPT_MAX_CHARS]


async def transcribe(
    audio: bytes,
    *,
    user_id: int | None = None,
    channel: str = "telegram",
    duration: float | None = None,
) -> str:
    """Bản chép lời (đã làm sạch, khác rỗng). Raise VoiceError (không có chữ) hoặc
    TurnTimeoutError (hết hạn chót lượt)."""
    if not enabled():
        raise VoiceError("disabled")
    error = limit_error(len(audio), duration)
    if error:
        raise VoiceError(error)
    fmt = sniff(audio)
    if fmt is None:
        raise VoiceError("format")
    prompt = await vocabulary_prompt(user_id)
    prompt_id = await telemetry.start(
        user_id or 0, "stt", f"(tin thoại {fmt.name}, {len(audio) // 1024} KB)", channel=channel
    )
    try:
        result = await orchestrator.transcribe_audio(
            audio,
            filename=f"voice.{fmt.extension}",
            groq_mime=fmt.groq_mime,
            gemini_mime=fmt.gemini_mime,
            prompt=prompt,
            language=language(),
        )
    except TurnTimeoutError as exc:
        await telemetry.failure(prompt_id, "stt", exc)
        raise
    except Exception as exc:
        logger.warning("Chép lời tin thoại lỗi: %s", messages.exception_detail(exc))
        await telemetry.failure(prompt_id, "stt", exc)
        raise VoiceError("failed", messages.exception_detail(exc)) from exc
    text = clean_transcript(result.text)
    await telemetry.success(prompt_id, "stt", text or "(không nghe ra lời)")
    if not text:
        raise VoiceError("empty")
    return text


# ─── Câu trả lời do code viết ────────────────────────────────────────────────

_ERROR_TEMPLATES = {
    "disabled": "🎙️ {Ai} chưa được bật nghe tin nhắn thoại, gõ chữ giúp {ai} nha{tail}.",
    "too_long": (
        "🎙️ Tin thoại dài quá - {ai} chỉ nghe được tối đa {max_seconds} giây mỗi tin. "
        "Nói ngắn lại hoặc gõ chữ giúp {ai} nha{tail}."
    ),
    "too_big": (
        "🎙️ File ghi âm lớn quá (tối đa {max_mb} MB) nên {ai} chưa nghe được. "
        "Gửi đoạn ngắn hơn giúp {ai} nha{tail}."
    ),
    "format": (
        "🎙️ {Ai} chưa mở được định dạng ghi âm này. Gửi tin thoại bình thường hoặc gõ chữ "
        "giúp {ai} nha{tail}."
    ),
    "empty": (
        "🎙️ {Ai} nghe mãi mà chưa rõ lời trong tin này. Nói lại to rõ hơn hoặc gõ chữ giúp "
        "{ai} nha{tail}."
    ),
    "failed": (
        "😥 {Ai} chưa nghe được tin thoại (bên AI đang bận). Gõ chữ giúp {ai} hoặc gửi lại sau "
        "ít phút nha{tail}."
    ),
    "download": "📥 {Ai} chưa tải được tin thoại về (mạng chập chờn). Gửi lại giúp {ai} nha{tail}.",
}


def error_reply(
    kind: str, *, owner: bool, address_form: str | None = None, detail: str = ""
) -> str:
    return media_text.say(
        _ERROR_TEMPLATES.get(kind, _ERROR_TEMPLATES["failed"]),
        owner=owner,
        address_form=address_form,
        detail=detail,
        max_seconds=max_seconds(),
        max_mb=max(1, max_bytes() // _MB),
    )


async def error_reply_to(kind: str, user_id: int, detail: str = "") -> str:
    """error_reply cho người nhận của lượt hiện tại (services/reply_audience.py)."""
    return await media_text.say_to(
        _ERROR_TEMPLATES.get(kind, _ERROR_TEMPLATES["failed"]),
        user_id,
        detail=detail,
        max_seconds=max_seconds(),
        max_mb=max(1, max_bytes() // _MB),
    )


def echo_line(transcript: str, *, owner: bool = False, address_form: str | None = None) -> str:
    """ "🎙️ Em nghe là: «...»" - gửi trước câu trả lời để người dùng kiểm."""
    shown = transcript if len(transcript) <= _ECHO_MAX_CHARS else transcript[:_ECHO_MAX_CHARS] + "…"
    return media_text.say(
        "🎙️ {Ai} nghe là: «{text}»", owner=owner, address_form=address_form, text=shown
    )


_WRITE_BLOCKED_TEMPLATE = (
    "🎙️ Câu này là lệnh ghi {what} - tin thoại có thể nghe nhầm số nên {ai} không tự ghi. "
    "Gõ lại bằng chữ giúp {ai} nha{tail}."
)
_WRITE_KINDS = {"portfolio": "danh mục cổ phiếu", "expense": "chi tiêu"}


def write_statement_kind(text: str) -> str | None:
    """ "portfolio" | "expense" | None: chữ chép từ tin thoại là câu mà nhánh nhanh
    SẼ ghi dữ liệu (lượt chỉ đọc bỏ qua nhánh đó - phải nói rõ là chưa ghi, không
    để chat AI tự "xác nhận" bừa)."""
    from services import expense_service, portfolio_service

    if portfolio_service.looks_like_write_statement(text):
        return "portfolio"
    if expense_service.chat_capture_enabled() and expense_service.parse_expense(text) is not None:
        return "expense"
    return None


def write_blocked_reply(kind: str, *, owner: bool = False, address_form: str | None = None) -> str:
    return media_text.say(
        _WRITE_BLOCKED_TEMPLATE,
        owner=owner,
        address_form=address_form,
        what=_WRITE_KINDS.get(kind, "dữ liệu"),
    )


def write_record_kind(kind: str) -> str:
    """Loại lượt lưu lịch sử cho câu báo "chưa ghi" (services/conversation_log.py)."""
    return conversation_log.KIND_PORTFOLIO if kind == "portfolio" else conversation_log.KIND_EXPENSE


async def handle_channel_voice(
    user_id: int,
    audio: bytes,
    *,
    is_admin: bool = False,
    duration_ms: int | None = None,
) -> tuple[list[str], str | None]:
    """Zalo: chép lời -> handle_channel_text lượt CHỈ ĐỌC. Không raise; trả
    (các tin trả lời, provider) như handle_channel_image. Gọi trong
    assistant_turn của người gửi và reply_audience.use() của router."""
    duration = duration_ms / 1000 if duration_ms and duration_ms > 0 else None
    # MỘT hạn chót cho bước nghe + lượt chat (handle_channel_text lồng turn_deadline
    # bên trong thì giữ hạn sớm hơn - không cộng thêm CHAT_TURN_TIMEOUT_SEC nữa).
    with turn_deadline():
        try:
            transcript = await transcribe(audio, user_id=user_id, channel="zalo", duration=duration)
        except VoiceError as exc:
            return [await error_reply_to(exc.kind, user_id, exc.detail)], None
        except TurnTimeoutError as exc:
            return [await reply_audience.error_text("timeout", user_id, str(exc))], None
        # Lời xưng hô của câu nhắc lại chỉ phụ thuộc cách gọi đã đặt (chủ bot dùng "em").
        form = reply_audience.current().address_form
        echo = echo_line(transcript, address_form=form)
        blocked = write_statement_kind(transcript)
        if blocked is not None:
            reply = write_blocked_reply(blocked, address_form=form)
            await conversation_log.record_exchange(
                user_id, transcript, reply, write_record_kind(blocked)
            )
            return [echo, reply], None
        result = await channel_chat_service.handle_channel_text(
            user_id, transcript, is_admin, channel="zalo", allow_side_effects=False
        )
    return [echo, *result.messages], result.provider
