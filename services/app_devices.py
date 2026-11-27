"""Ghép và xác thực thiết bị app màn hình thông tin (android/, GET /api/v1/app/dashboard).

Luồng ghép (chủ bot, Telegram):
1. ``/app moi <tên>`` -> mã ghép 8 ký tự (vd ``K7QF-3MXA``) hết hạn sau
   ``PAIR_TTL_SEC``; DB chỉ lưu SHA-256 của mã.
2. App gửi mã lên ``POST /api/v1/app/pair`` -> nhận token dài (``la_...``), app tự
   lưu. Mã chỉ dùng được 1 lần. Token cũng chỉ lưu SHA-256 (lộ DB không lộ token;
   không cần core.crypto vì không bao giờ phải đọc lại bản gốc).
3. ``/app`` xem thiết bị, ``/app xoa <số>`` thu hồi (có hiệu lực ngay: xoá cache).

Chống dò mã: mã 8 ký tự từ bảng 31 chữ (~40 bit), hết hạn sau 10 phút, cộng trần số
lần ghép SAI toàn hệ thống (``_PAIR_FAIL_LIMIT`` trong ``_PAIR_FAIL_WINDOW_SEC``) -
vượt trần thì từ chối mọi lượt ghép tới khi cửa sổ trôi qua.

Xác thực mỗi 5 phút: cache RAM có trần ``_AUTH_CACHE_MAX`` thiết bị, đọc lại DB sau
``_AUTH_TTL_SEC``. DB chập chờn thì dùng tiếp bản cache cũ (tối đa ``_AUTH_STALE_SEC``)
để màn hình không tắt dữ liệu chỉ vì Supabase lỗi thoáng qua.

Token sai (401): hash token DB báo không có được nhớ ``_UNKNOWN_TTL_SEC`` giây (trần
``_UNKNOWN_MAX`` hash) để app cũ gửi lặp token đã thu hồi không đọc DB mỗi lần; mỗi IP
chỉ được sai ``_AUTH_FAIL_LIMIT`` lần trong ``_AUTH_FAIL_WINDOW_SEC`` (bảng IP có trần
``_AUTH_FAIL_MAX_IPS``), quá thì ``AuthLocked`` (429) mà không đọc DB. Thiết bị đã xác
thực gần đây vẫn qua được dù cùng IP (màn hình thật chung mạng với máy gửi token sai).
"""

from __future__ import annotations

import collections
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from core import database as db

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
COMMAND = "/app"
PAIR_TTL_SEC = 600
MAX_DEVICES = 5
NAME_MAX = 40
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # bỏ 0/O, 1/I/L dễ nhầm
_CODE_LEN = 8
TOKEN_PREFIX = "la_"
_TOKEN_MAX_LEN = 128

_PAIR_FAIL_LIMIT = 20
_PAIR_FAIL_WINDOW_SEC = 600
_pair_failures: collections.deque[float] = collections.deque()


@dataclass(frozen=True)
class Device:
    id: int
    user_id: int
    name: str


_AUTH_TTL_SEC = 300
_AUTH_STALE_SEC = 6 * 3600
_AUTH_CACHE_MAX = 16
# hash token -> (lúc đọc DB, thiết bị)
_auth_cache: collections.OrderedDict[str, tuple[float, Device]] = collections.OrderedDict()
# hash token DB báo không có -> lúc đọc DB (cache âm).
_UNKNOWN_TTL_SEC = 60
_UNKNOWN_MAX = 64
_unknown_tokens: collections.OrderedDict[str, float] = collections.OrderedDict()
# IP -> các mốc trả 401 gần đây.
_AUTH_FAIL_LIMIT = 20
_AUTH_FAIL_WINDOW_SEC = 600
_AUTH_FAIL_MAX_IPS = 256
_auth_failures: collections.OrderedDict[str, collections.deque[float]] = collections.OrderedDict()


class PairError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class AuthLocked(Exception):
    """IP gửi token sai quá nhiều lần: từ chối (429) mà không đọc DB."""

    def __init__(self, retry_after: int):
        super().__init__("too many failed device authentications")
        self.retry_after = retry_after


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))


def normalize_code(raw: str) -> str:
    """Bỏ khoảng trắng, gạch nối; viết hoa (người nhập gõ thường cũng được)."""
    return "".join(ch for ch in str(raw or "")[:40].upper() if ch.isascii() and ch.isalnum())


def display_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def clear_cache() -> None:
    _auth_cache.clear()
    _pair_failures.clear()
    _unknown_tokens.clear()
    _auth_failures.clear()


def _prune_failures(now: float) -> None:
    while _pair_failures and now - _pair_failures[0] > _PAIR_FAIL_WINDOW_SEC:
        _pair_failures.popleft()


async def pair(raw_code: str) -> tuple[str, Device]:
    """Mã ghép -> (token mới, thiết bị). Raise PairError khi sai/hết hạn/bị chặn."""
    now = time.monotonic()
    _prune_failures(now)
    if len(_pair_failures) >= _PAIR_FAIL_LIMIT:
        raise PairError("PAIR_LOCKED", "Nhập sai quá nhiều lần, thử lại sau 10 phút.", 429)
    code = normalize_code(raw_code)
    if len(code) != _CODE_LEN or any(ch not in _CODE_ALPHABET for ch in code):
        _pair_failures.append(now)
        raise PairError("INVALID_CODE", "Mã ghép gồm 8 ký tự, vd K7QF-3MXA.")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = await db.app_device_pair(_hash(code), _hash(token))
    if row is None:
        _pair_failures.append(now)
        raise PairError("CODE_NOT_FOUND", "Mã ghép sai hoặc đã hết hạn. Gõ /app moi để lấy mã mới.")
    device = Device(int(row["id"]), int(row["telegram_user_id"]), str(row["name"]))
    logger.info("app_devices: ghép thiết bị #%d (%s).", device.id, device.name)
    return token, device


def _cache_put(key: str, device: Device, at: float) -> None:
    _auth_cache[key] = (at, device)
    _auth_cache.move_to_end(key)
    while len(_auth_cache) > _AUTH_CACHE_MAX:
        _auth_cache.popitem(last=False)


def _recent_failures(ip: str, now: float) -> collections.deque[float]:
    times = _auth_failures.get(ip) or collections.deque()
    while times and now - times[0] > _AUTH_FAIL_WINDOW_SEC:
        times.popleft()
    return times


def _locked_for(ip: str, now: float) -> int:
    """Số giây IP còn bị chặn (0 = không chặn)."""
    if not ip:
        return 0
    times = _recent_failures(ip, now)
    if len(times) < _AUTH_FAIL_LIMIT:
        return 0
    return max(1, int(_AUTH_FAIL_WINDOW_SEC - (now - times[0])) + 1)


def _record_failure(ip: str, now: float) -> None:
    if not ip:
        return
    times = _recent_failures(ip, now)
    times.append(now)
    _auth_failures[ip] = times
    _auth_failures.move_to_end(ip)  # IP vừa sai đứng cuối; bảng đầy thì bỏ IP cũ nhất
    while len(_auth_failures) > _AUTH_FAIL_MAX_IPS:
        _auth_failures.popitem(last=False)


def _remember_unknown(key: str, now: float) -> None:
    _unknown_tokens[key] = now
    _unknown_tokens.move_to_end(key)
    while len(_unknown_tokens) > _UNKNOWN_MAX:
        _unknown_tokens.popitem(last=False)


async def authenticate(authorization: str, ip: str = "") -> Device | None:
    """Header ``Authorization: Bearer la_...`` -> thiết bị, hoặc None (401).

    ``ip``: IP client (web_admin.client_ip) cho giới hạn số lần sai; raise
    ``AuthLocked`` khi IP đó đã sai quá ``_AUTH_FAIL_LIMIT`` lần trong cửa sổ."""
    scheme, _, token = str(authorization or "").partition(" ")
    token = token.strip()
    well_formed = (
        scheme.lower() == "bearer"
        and token.startswith(TOKEN_PREFIX)
        and len(token) <= _TOKEN_MAX_LEN
    )
    key = _hash(token) if well_formed else ""
    now = time.monotonic()
    hit = _auth_cache.get(key) if key else None
    if hit is not None and now - hit[0] < _AUTH_TTL_SEC:
        return hit[1]
    locked = _locked_for(ip, now)
    if locked:
        # Thiết bị thật đã xác thực trong vài giờ qua vẫn qua (token thu hồi đã bị xoá
        # khỏi cache nên không lọt); còn lại từ chối mà không đọc DB.
        if hit is not None and now - hit[0] < _AUTH_STALE_SEC:
            return hit[1]
        raise AuthLocked(locked)
    seen = _unknown_tokens.get(key) if key else None
    if not key or (seen is not None and now - seen < _UNKNOWN_TTL_SEC):
        _record_failure(ip, now)
        return None
    try:
        row = await db.app_device_by_token(key)
    except Exception:
        if hit is not None and now - hit[0] < _AUTH_STALE_SEC:
            logger.warning(
                "app_devices: DB lỗi, dùng xác thực đã cache của thiết bị #%d.", hit[1].id
            )
            return hit[1]
        raise
    if row is None:
        _auth_cache.pop(key, None)
        _remember_unknown(key, now)
        _record_failure(ip, now)
        return None
    _unknown_tokens.pop(key, None)
    device = Device(int(row["id"]), int(row["telegram_user_id"]), str(row["name"]))
    _cache_put(key, device, now)
    return device


def _fmt_time(value) -> str:
    if not isinstance(value, datetime):
        return "chưa"
    return value.astimezone(_VN_TZ).strftime("%H:%M %d/%m")


def _usage() -> str:
    return (
        "📱 App màn hình thông tin (lịch, thời tiết, vàng, xăng, tin, watchlist):\n"
        "/app moi <tên> — lấy mã ghép (hết hạn sau 10 phút), nhập mã vào app\n"
        "/app xoa <số> — thu hồi thiết bị\n"
        "/app — danh sách thiết bị"
    )


async def _list_text(user_id: int) -> str:
    rows = await db.app_device_list(user_id)
    if not rows:
        return "📭 Chưa có thiết bị nào.\n\n" + _usage()
    lines = [f"📱 Thiết bị app ({len(rows)}/{MAX_DEVICES}):"]
    for row in rows:
        if row.get("paired_at") is None:
            state = f"đang chờ ghép, mã hết hạn lúc {_fmt_time(row.get('pair_expires_at'))}"
        else:
            state = f"lần cuối {_fmt_time(row.get('last_seen_at'))}"
        lines.append(f"• #{row['id']} {row['name']} — {state}")
    return "\n".join(lines) + "\n\n" + _usage()


async def _create(user_id: int, raw_name: str) -> str:
    name = " ".join(raw_name.split())[:NAME_MAX] or "Màn hình"
    code = new_code()
    device_id = await db.app_device_create_pending(
        user_id, name, _hash(code), PAIR_TTL_SEC, MAX_DEVICES
    )
    if device_id is None:
        return (
            f"⚠️ Anh đã có {MAX_DEVICES} thiết bị. Gõ /app để xem rồi /app xoa <số> bớt "
            "thiết bị cũ nha."
        )
    return (
        f"🔑 Mã ghép cho “{name}” (#{device_id}): {display_code(code)}\n"
        f"Mở app → Cài đặt → nhập địa chỉ bot và mã này. Mã hết hạn sau "
        f"{PAIR_TTL_SEC // 60} phút và chỉ dùng được 1 lần."
    )


async def _revoke(user_id: int, raw_id: str) -> str:
    try:
        device_id = int(raw_id.lstrip("#"))
    except ValueError:
        return "Gõ /app xoa <số thiết bị>, số xem bằng /app."
    if not await db.app_device_revoke(user_id, device_id):
        return f"Không thấy thiết bị #{device_id} (gõ /app để xem danh sách)."
    _auth_cache.clear()  # token đã thu hồi hết hiệu lực ngay, không chờ hết TTL cache
    return f"🗑️ Đã thu hồi thiết bị #{device_id}. App trên máy đó sẽ phải ghép lại."


async def handle_command(user_id: int, args: list[str]) -> str:
    """``/app``, ``/app moi [tên]``, ``/app xoa <số>`` (chỉ chủ bot)."""
    action = args[0].lower() if args else ""
    if action in ("", "ds", "list"):
        return await _list_text(user_id)
    if action in ("moi", "mới", "new", "them", "thêm"):
        return await _create(user_id, " ".join(args[1:]))
    if action in ("xoa", "xoá", "xóa", "del") and len(args) >= 2:
        return await _revoke(user_id, args[1])
    return _usage()
