"""Thành phố mặc định cho thời tiết của TỪNG người (thay cho TP.HCM chung).

Lưu ở bảng settings, khoá ``weather_home:<internal_user_id>`` (không cần
migration). Đặt bằng lệnh ``/thoitiet nha <tỉnh>`` hoặc câu khai báo rõ ràng
("anh ở Hà Nội", "nhà em ở Đà Nẵng") - parse tất định bằng code, KHÔNG dựa vào
fact trí nhớ do LLM tự đặt tên khoá.

Zalo user (id âm) và chủ bot Telegram/Zoom có khoá riêng: Zalo của chính chủ
bot cũng phải đặt riêng (ngữ cảnh Zalo tách hẳn Telegram). Cache RAM có trần.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict

from core import database as db
from core.text_normalize import fold
from services import personal_settings, reply_audience, weather

logger = logging.getLogger(__name__)

MAX_NAME_LEN = 40
_CACHE_MAX = 256
_GEOCODE_TIMEOUT_SEC = 10
# user_id -> tên thành phố ("" = chưa đặt)
_cache: OrderedDict[int, str] = OrderedDict()


class HomeError(ValueError):
    """Tên không hợp lệ/không tìm thấy hoặc lỗi lưu - message đọc được cho người dùng."""


def _remember(user_id: int, name: str) -> None:
    _cache[user_id] = name
    _cache.move_to_end(user_id)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def clear_cache() -> None:
    _cache.clear()


def forget(user_id: int) -> None:
    """Bỏ thành phố của 1 người khỏi cache RAM - gọi sau khi dòng DB đã bị xoá
    (/xoadulieu, /zaloxoa) để không dùng tiếp thành phố cũ tới lần khởi động lại."""
    _cache.pop(user_id, None)


async def get(user_id: int) -> str:
    """Thành phố mặc định của người dùng, "" nếu chưa đặt hoặc đọc DB lỗi."""
    if user_id in _cache:
        _cache.move_to_end(user_id)
        return _cache[user_id]
    try:
        name = (await db.get_setting(personal_settings.weather_home_key(user_id))) or ""
    except Exception:
        logger.warning("Không đọc được thành phố mặc định của user %s.", user_id, exc_info=True)
        return ""  # không cache: lần sau đọc lại
    _remember(user_id, name)
    return name


def known_place(raw: str) -> str | None:
    """Tên chuẩn nếu ``raw`` là địa danh quen (danh sách weather_chat) hoặc tên khác của TP.HCM."""
    from services import weather_chat

    key = fold(" ".join(raw.split()))
    for folded, name in weather_chat.known_places():
        if key == folded:
            return name
    if weather.is_default_location(weather._clean_location(raw)):
        return "TP.HCM"
    return None


def clean_name(raw: str) -> str:
    name = " ".join((raw or "").split()).strip(" .,!?")
    if not name or len(name) > MAX_NAME_LEN or any(ch in name for ch in "<>\"'`\\/{}[]"):
        raise HomeError(f"Tên tỉnh/thành 1-{MAX_NAME_LEN} ký tự, không chứa ký tự đặc biệt.")
    return name


async def set_home(user_id: int, raw: str) -> str:
    """Lưu thành phố mặc định, trả tên đã lưu. Tên lạ phải tra được toạ độ."""
    name = clean_name(raw)
    you, me = reply_audience.forms()
    canonical = known_place(name)
    if canonical is None:
        try:
            geo = await asyncio.wait_for(weather.resolve_location(name), _GEOCODE_TIMEOUT_SEC)
        except (weather.WeatherError, TimeoutError) as exc:
            raise HomeError(
                f"{me.capitalize()} chưa kiểm tra được địa điểm '{name}' lúc này, {you} thử "
                "lại sau nha."
            ) from exc
        if not geo:
            raise HomeError(
                f"{me.capitalize()} không tìm thấy địa điểm '{name}', {you} gõ tên tỉnh/thành "
                "rõ hơn nha."
            )
    stored = canonical or name
    try:
        await db.set_setting(personal_settings.weather_home_key(user_id), stored)
    except Exception as exc:
        logger.warning("Không lưu được thành phố mặc định.", exc_info=True)
        raise HomeError(f"{me.capitalize()} chưa lưu được, {you} thử lại sau nha.") from exc
    _remember(user_id, stored)
    return stored


async def clear_home(user_id: int) -> None:
    try:
        await db.set_setting(personal_settings.weather_home_key(user_id), "")
    except Exception as exc:
        logger.warning("Không xoá được thành phố mặc định.", exc_info=True)
        you, me = reply_audience.forms()
        raise HomeError(f"{me.capitalize()} chưa xoá được, {you} thử lại sau nha.") from exc
    _remember(user_id, "")


# Câu khai báo NGẮN, cả tin nhắn chỉ có vậy: "anh ở Hà Nội", "nhà em ở Đà Nẵng
# nha", "mình sống ở Cần Thơ". "đang ở" (đi công tác/du lịch) không tính.
_STATEMENT_RE = re.compile(
    r"^(?:nha )?(?:anh|em|minh|toi|tui|chi|tao) (?:song |o )?(?:o|tai) (?P<place>.+?)"
    r"(?: (?:nha|nhe|do|day|a|nghe|ha|ne|roi))*$"
)


# Câu HỎI ("em ở Đà Nẵng à?", "anh ở Hà Nội hả") không phải khai báo - so trên chữ
# có dấu vì bỏ dấu thì "à" (hỏi) trùng "ạ" (lễ phép: "em ở Hà Nội ạ" vẫn là khai báo).
_QUESTION_END_RE = re.compile(r"(?:^|\s)(?:à|hả|hở|hử|sao|chưa|không|hông|ko|k)\s*$")


def parse_statement(text: str) -> str | None:
    """Tên địa danh quen nếu tin nhắn là câu khai báo nơi ở, ngược lại None."""
    low = (text or "").lower()
    if "?" in low or _QUESTION_END_RE.search(re.sub(r"[!.,;:…]+", " ", low)):
        return None
    folded = re.sub(r"[?!.,;:]+", " ", fold(text or ""))
    folded = re.sub(r"\s+", " ", folded).strip()
    if len(folded) > 60:
        return None
    match = _STATEMENT_RE.match(folded)
    if not match:
        return None
    return known_place(match.group("place"))


async def maybe_handle_statement(user_id: int, text: str) -> str | None:
    """Lưu thành phố từ câu khai báo; trả câu xác nhận, None nếu không phải."""
    place = parse_statement(text)
    if place is None:
        return None
    try:
        stored = await set_home(user_id, place)
    except HomeError as exc:
        return f"❌ {exc}"
    you, me = reply_audience.forms()
    return (
        f"📍 Dạ {me} nhớ rồi: thời tiết mặc định của {you} là {stored}. "
        "Đổi bằng /thoitiet nha <tỉnh>, xoá bằng /thoitiet nha xoa."
    )
