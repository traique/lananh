"""Pairing nhiều tài khoản Zalo cho ĐÚNG 1 bot Zalo (Zalo B), với phân quyền:

- role="admin": dùng được tính năng nhóm (/nhom, /themnhom, /xoanhom, /tongket,
  /dangnoi) NGOÀI các tính năng bình thường.
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

from core import database as db

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
ROLE_ADMIN = "admin"
ROLE_USER = "user"

VALID_ROLES = {ROLE_ADMIN, ROLE_USER}
VALID_STATUSES = {STATUS_ACTIVE, STATUS_SUSPENDED}

_schema_lock = asyncio.Lock()
_schema_ready = False

_alert_callback: Optional[Callable[[str], Awaitable[None]]] = None
_background_tasks: set[asyncio.Task] = set()
_notified_unpaired: set[str] = set()


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
    _notified_unpaired.add(external_id)
    ten = f" ({display_name})" if display_name else ""
    text = (
        f"🔔 Zalo id={external_id}{ten} vừa nhắn cho bot nhưng chưa được cấp quyền.\n"
        f"Dùng /zalopair {external_id} để cấp quyền thành viên, hoặc "
        f"/zaloadmin {external_id} để cấp quyền admin (dùng được lệnh nhóm)."
    )
    _send_alert(text)


@dataclass(frozen=True)
class ZaloUser:
    external_id: str
    internal_user_id: int
    display_name: str
    role: str  # "admin" | "user"
    status: str  # "active" | "suspended"

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE


async def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    async with _schema_lock:
        if _schema_ready:
            return
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            # Sequence tăng dần bình thường; internal_user_id thực tế lấy giá
            # trị ÂM của sequence (xem DEFAULT bên dưới) để không bao giờ đụng
            # dải số dương của Telegram user id.
            await conn.execute("CREATE SEQUENCE IF NOT EXISTS zalo_users_uid_seq")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS zalo_users (
                    external_id TEXT PRIMARY KEY,
                    internal_user_id BIGINT UNIQUE NOT NULL
                        DEFAULT (-(nextval('zalo_users_uid_seq'))),
                    display_name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            # An toàn khi nâng cấp từ bản trước khi có internal_user_id (bảng
            # đã tồn tại nhưng thiếu cột) - vô hại nếu cột đã có sẵn.
            await conn.execute(
                """
                ALTER TABLE zalo_users
                ADD COLUMN IF NOT EXISTS internal_user_id BIGINT
                    UNIQUE NOT NULL DEFAULT (-(nextval('zalo_users_uid_seq')))
                """
            )
        _schema_ready = True


def _row_to_user(row) -> ZaloUser:
    return ZaloUser(
        external_id=row["external_id"],
        internal_user_id=row["internal_user_id"],
        display_name=row["display_name"] or "",
        role=row["role"],
        status=row["status"],
    )


async def resolve(external_id: str) -> Optional[ZaloUser]:
    await ensure_schema()
    row = await (await db.get_pool()).fetchrow(
        "SELECT external_id, internal_user_id, display_name, role, status "
        "FROM zalo_users WHERE external_id = $1",
        external_id,
    )
    return _row_to_user(row) if row else None


async def _upsert(external_id: str, display_name: str, role: str) -> ZaloUser:
    await ensure_schema()
    row = await (await db.get_pool()).fetchrow(
        """
        INSERT INTO zalo_users (external_id, display_name, role, status)
        VALUES ($1, $2, $3, 'active')
        ON CONFLICT (external_id) DO UPDATE SET
            display_name = CASE WHEN EXCLUDED.display_name <> '' THEN EXCLUDED.display_name
                                 ELSE zalo_users.display_name END,
            role = $3,
            status = 'active',
            updated_at = now()
        RETURNING external_id, internal_user_id, display_name, role, status
        """,
        external_id,
        display_name.strip()[:500],
        role,
    )
    return _row_to_user(row)


async def pair(external_id: str, display_name: str = "") -> ZaloUser:
    """Pair 1 id Zalo với quyền THÀNH VIÊN (chỉ tính năng bình thường). Nếu id
    đã pair sẵn (kể cả admin), gọi lại KHÔNG hạ quyền - dùng /zalohaquyen nếu
    thật sự muốn hạ quyền admin xuống user."""
    existing = await resolve(external_id)
    role = existing.role if existing else ROLE_USER
    return await _upsert(external_id, display_name, role)


async def pair_as_admin(external_id: str, display_name: str = "") -> ZaloUser:
    """Pair (hoặc nâng quyền id đã pair) thành ADMIN — dùng được tính năng
    nhóm. Hỗ trợ NHIỀU admin cùng lúc (không giới hạn 1 admin duy nhất)."""
    return await _upsert(external_id, display_name, ROLE_ADMIN)


async def demote_to_user(external_id: str) -> bool:
    """Hạ quyền 1 admin về user (thành viên thường). Trả False nếu chưa pair."""
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        "UPDATE zalo_users SET role = 'user', updated_at = now() WHERE external_id = $1",
        external_id,
    )
    return result != "UPDATE 0"


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


async def remove(external_id: str) -> bool:
    await ensure_schema()
    result = await (await db.get_pool()).execute(
        "DELETE FROM zalo_users WHERE external_id = $1", external_id
    )
    _notified_unpaired.discard(external_id)
    return result != "DELETE 0"


async def list_users() -> list[ZaloUser]:
    await ensure_schema()
    rows = await (await db.get_pool()).fetch(
        "SELECT external_id, internal_user_id, display_name, role, status FROM zalo_users "
        "ORDER BY (role = 'admin') DESC, display_name, external_id"
    )
    return [_row_to_user(row) for row in rows]
