"""Pairing nhiều tài khoản Zalo cho ĐÚNG 1 bot Zalo (Zalo B), với phân quyền:

- role="admin": dùng được tính năng nhóm (/nhom, /themnhom, /xoanhom, /tongket,
  /dangnoi) và luồng Facebook (/fb_*) NGOÀI các tính năng bình thường.
- role="user": chỉ dùng được tính năng bình thường (chat, /prompt, /gia, /dich,
  /reset...), KHÔNG thấy/dùng được lệnh nhóm - channels/router.py chỉ gọi
  channels.group_commands.maybe_handle_group_command() khi role=="admin".

MỖI external_id được cấp 1 `internal_user_id` (BIGINT) RIÊNG, ÂM (khác dải số
dương của Telegram user id) - dùng làm user_id khi gọi
services/channel_chat_service.py (chat session, trí nhớ dài hạn, ghi chú, danh
mục...). Mục đích: CÁCH LY hoàn toàn ngữ cảnh/bí mật giữa từng người nhắn qua
Zalo VÀ giữa họ với chủ bot Telegram (config.ALLOWED_USER_ID) - trước đây MỌI
người nhắn Zalo (và cả Telegram) dùng chung 1 user_id nên vô tình thấy tiếp
được lịch sử/trí nhớ của nhau. internal_user_id được cấp phát 1 LẦN DUY NHẤT
khi pair lần đầu (qua sequence `zalo_users_uid_seq`, luôn giảm dần: -1, -2,
-3...) và giữ nguyên vĩnh viễn cho external_id đó kể cả sau khi đổi role/khoá/
mở khoá - chỉ mất khi /zaloxoa (xoá hẳn pairing).

Khác thiết kế "1 controller duy nhất, dùng chung ALLOWED_USER_ID" trước đây
(channels/zalo_session.py) - giờ nhiều id Zalo có thể cùng nhắn cho bot, MỖI
NGƯỜI có bộ nhớ/ngữ cảnh RIÊNG. Người ghép đôi ĐẦU TIÊN qua flow `/pair <code>`
cũ (channels/router.py::put_controller) tự động là admin đầu tiên NHƯNG vẫn
được cấp internal_user_id riêng (KHÔNG dùng chung với Telegram) - nếu chủ bot
muốn tiếp tục đúng mạch hội thoại Telegram khi chat qua Zalo, họ cần tự ý thức
đây là 2 ngữ cảnh tách biệt.

Mọi id Zalo CHƯA pair khi nhắn cho bot đều bị bỏ qua ÂM THẦM (không lộ ra là
bot chưa cấu hình xong cho người lạ) - chỉ báo cho owner Telegram qua
send_alert(), owner tự quyết định có pair hay không."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from core import address, database as db
from services import personal_settings

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
ROLE_ADMIN = "admin"
ROLE_USER = "user"

VALID_ROLES = {ROLE_ADMIN, ROLE_USER}
VALID_STATUSES = {STATUS_ACTIVE, STATUS_SUSPENDED}

_alert_callback: Optional[Callable[[str], Awaitable[None]]] = None
_background_tasks: set[asyncio.Task] = set()
# id chưa pair đã báo owner (dict dùng như set có thứ tự chèn) - có trần để
# 1 đợt spam từ hàng nghìn id lạ không làm phình RAM; vượt trần thì quên id cũ
# nhất (cùng lắm owner nhận lại 1 cảnh báo cho id đó).
_NOTIFIED_UNPAIRED_MAX = 1000
_notified_unpaired: dict[str, None] = {}

# Dữ liệu cá nhân khoá theo internal_user_id (cột telegram_user_id) - /zaloxoa
# xoá sạch cùng lúc với pairing (chat, trí nhớ, ghi chú, lịch sử, danh mục).
# user_calls (chỉ đếm lượt gọi, không có nội dung) được giữ cho thống kê.
_USER_DATA_TABLES = (
    "chat_messages",
    "user_facts",
    "user_memory_summary",
    "user_memory_highlights",
    "notes",
    "prompts",
    "stock_holdings",
    "stock_closed_positions",
    # migrations/022: sổ giao dịch, theo dõi/cảnh báo giá, nhật ký thông báo.
    "stock_transactions",
    "stock_watchlist",
    "stock_price_alerts",
    "stock_alert_log",
)


def set_alert_callback(fn: Callable[[str], Awaitable[None]]) -> None:
    global _alert_callback
    _alert_callback = fn


def _send_alert(text: str) -> None:
    if _alert_callback is None:
        return
    try:
        task = asyncio.create_task(_alert_callback(text))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except Exception:
        logger.warning("Không gửi được cảnh báo Zalo chưa pair.", exc_info=True)


def notify_unpaired(external_id: str, display_name: str = "") -> None:
    """Báo owner Telegram 1 lần cho mỗi external_id chưa pair (tránh spam nếu
    người đó nhắn liên tục). Reset khi process restart - chấp nhận được vì chỉ
    ảnh hưởng tần suất cảnh báo, không ảnh hưởng bảo mật (người chưa pair vẫn
    luôn bị chặn ở tầng resolve())."""
    if not external_id or external_id in _notified_unpaired:
        return
    _notified_unpaired[external_id] = None
    while len(_notified_unpaired) > _NOTIFIED_UNPAIRED_MAX:
        _notified_unpaired.pop(next(iter(_notified_unpaired)))
    ten = f" ({display_name})" if display_name else ""
    text = (
        f"🔔 Zalo id={external_id}{ten} vừa nhắn cho bot nhưng chưa được cấp quyền.\n"
        f"Dùng /zalopair {external_id} để cấp quyền thành viên, hoặc "
        f"/zaloadmin {external_id} để cấp quyền admin (dùng được lệnh nhóm và /fb_*)."
    )
    _send_alert(text)


@dataclass(frozen=True)
class ZaloUser:
    external_id: str
    internal_user_id: int
    display_name: str
    role: str  # "admin" | "user"
    status: str  # "active" | "suspended"
    # Cách gọi người này do chủ bot đặt (core/address.py); None = mặc định persona.
    address_form: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE


async def ensure_schema() -> None:
    await db.ensure_migrations()


def _row_to_user(row) -> ZaloUser:
    return ZaloUser(
        external_id=row["external_id"],
        internal_user_id=row["internal_user_id"],
        display_name=row["display_name"] or "",
        role=row["role"],
        status=row["status"],
        address_form=address.normalize(row.get("address_form")),
    )


_USER_COLUMNS = "external_id, internal_user_id, display_name, role, status, address_form"


@db.with_reconnect
async def resolve(external_id: str) -> Optional[ZaloUser]:
    await ensure_schema()
    row = await (await db.get_pool()).fetchrow(
        f"SELECT {_USER_COLUMNS} FROM zalo_users WHERE external_id = $1",
        external_id,
    )
    return _row_to_user(row) if row else None


def _checked_address_form(address_form: str | None) -> str | None:
    if address_form is None:
        return None
    form = address.normalize(address_form)
    if form is None:
        raise ValueError(f"cách xưng hô không hợp lệ: {address_form!r}")
    return form


@db.with_reconnect
async def _upsert(
    external_id: str, display_name: str, role: str, address_form: str | None = None
) -> ZaloUser:
    """address_form=None giữ nguyên cách xưng hô đã đặt trước đó (nếu có)."""
    await ensure_schema()
    row = await (await db.get_pool()).fetchrow(
        f"""
        INSERT INTO zalo_users (external_id, display_name, role, status, address_form)
        VALUES ($1, $2, $3, 'active', $4)
        ON CONFLICT (external_id) DO UPDATE SET
            display_name = CASE WHEN EXCLUDED.display_name <> '' THEN EXCLUDED.display_name
                                 ELSE zalo_users.display_name END,
            role = $3,
            status = 'active',
            address_form = COALESCE(EXCLUDED.address_form, zalo_users.address_form),
            updated_at = now()
        RETURNING {_USER_COLUMNS}
        """,
        external_id,
        display_name.strip()[:500],
        role,
        _checked_address_form(address_form),
    )
    return _row_to_user(row)


async def pair(
    external_id: str, display_name: str = "", *, address_form: str | None = None
) -> ZaloUser:
    """Pair 1 id Zalo với quyền THÀNH VIÊN (chỉ tính năng bình thường). Nếu id
    đã pair sẵn (kể cả admin), gọi lại KHÔNG hạ quyền - dùng /zalohaquyen nếu
    thật sự muốn hạ quyền admin xuống user.

    ``address_form`` (core/address.py): cách gọi người này; None = giữ nguyên."""
    existing = await resolve(external_id)
    role = existing.role if existing else ROLE_USER
    return await _upsert(external_id, display_name, role, address_form)


async def pair_as_admin(
    external_id: str, display_name: str = "", *, address_form: str | None = None
) -> ZaloUser:
    """Pair (hoặc nâng quyền id đã pair) thành ADMIN — dùng được tính năng
    nhóm. Hỗ trợ NHIỀU admin cùng lúc (không giới hạn 1 admin duy nhất)."""
    return await _upsert(external_id, display_name, ROLE_ADMIN, address_form)


@db.with_reconnect
async def demote_to_user(external_id: str) -> bool:
    """Hạ quyền 1 admin về user (thành viên thường). Trả False nếu chưa pair."""
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        "UPDATE zalo_users SET role = 'user', updated_at = now() WHERE external_id = $1",
        external_id,
    )
    return result != "UPDATE 0"


@db.with_reconnect
async def set_status(external_id: str, status: str) -> bool:
    if status not in VALID_STATUSES:
        raise ValueError(f"status không hợp lệ: {status}")
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        "UPDATE zalo_users SET status = $2, updated_at = now() WHERE external_id = $1",
        external_id,
        status,
    )
    return result != "UPDATE 0"


async def _delete_user_data(conn, internal_user_id: int) -> None:
    """Xoá mọi dữ liệu cá nhân của 1 Zalo user (trong transaction của caller).

    Chỉ nhận id ÂM (dải Zalo) - không bao giờ được đụng dữ liệu chủ bot."""
    if internal_user_id >= 0:
        raise ValueError("internal_user_id của Zalo user phải âm")
    # Tin nhắc việc đang chờ gửi qua outbox cũng bỏ (FK chỉ SET NULL, tin vẫn sẽ đi).
    await conn.execute(
        "DELETE FROM zalo_outbox WHERE sent_at IS NULL AND reminder_id IN "
        "(SELECT id FROM reminders WHERE telegram_user_id = $1)",
        internal_user_id,
    )
    await conn.execute("DELETE FROM reminders WHERE telegram_user_id = $1", internal_user_id)
    for table in _USER_DATA_TABLES:
        await conn.execute(f"DELETE FROM {table} WHERE telegram_user_id = $1", internal_user_id)
    await conn.execute("DELETE FROM settings WHERE key = $1", f"memory_enabled_{internal_user_id}")
    # Thành phố thời tiết + bản tin sáng cá nhân (services/personal_settings.py).
    await conn.execute(
        "DELETE FROM settings WHERE key = ANY($1::text[])",
        personal_settings.zalo_user_keys(internal_user_id),
    )


@db.with_reconnect(idempotent=False)
async def remove(external_id: str) -> bool:
    """Gỡ pairing VÀ xoá toàn bộ dữ liệu cá nhân của user đó (1 transaction)."""
    await ensure_schema()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            internal_user_id = await conn.fetchval(
                "DELETE FROM zalo_users WHERE external_id = $1 RETURNING internal_user_id",
                external_id,
            )
            if internal_user_id is not None:
                await _delete_user_data(conn, int(internal_user_id))
    _notified_unpaired.pop(external_id, None)
    return internal_user_id is not None


@db.with_reconnect
async def list_users() -> list[ZaloUser]:
    await ensure_schema()
    rows = await (await db.get_pool()).fetch(
        f"SELECT {_USER_COLUMNS} FROM zalo_users "
        "ORDER BY (role = 'admin') DESC, display_name, external_id"
    )
    return [_row_to_user(row) for row in rows]
