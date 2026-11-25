"""Lưu trữ tín hiệu cổ phiếu từ nhóm Zalo (migrations/031, services/zalo_signals.py).

- Chế độ quét theo nhóm: ``zalo_groups.signal_mode`` = off | admin | all.
- Người theo dõi thêm ở chế độ admin: ``zalo_signal_people``.
- Tín hiệu: ``zalo_stock_signals`` - 1 người/mã/hướng/loại 1 dòng mỗi ngày (giờ VN);
  ``zoom_sent_at``/``zalo_sent_at`` đánh dấu đã giao từng kênh.
- Con trỏ quét (id cuối của ``zalo_group_messages`` đã xét) ở settings
  ``zalo:signal_cursor:<account_id>``. Gateway lưu tin nhóm tuần tự (1 hàng đợi nối
  tiếp) nên id tăng đúng thứ tự commit; vẫn chừa 3 giây cho chắc.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime

from core import database as db

MODES = ("off", "admin", "all")
CHANNELS = ("zoom", "zalo")
_CURSOR_KEY = "zalo:signal_cursor:{}"
# Tin vừa lưu chưa tới 3 giây chưa quét (phòng lượt lưu chậm commit sau lượt kế).
_SETTLE_SQL = "now() - interval '3 seconds'"


@dataclass(frozen=True)
class GroupMessage:
    row_id: int
    group_id: str
    alias: str
    mode: str
    message_id: str
    sender_id: str
    sender_name: str
    sender_role: str
    content: str
    quote_text: str
    sent_at: datetime


@dataclass(frozen=True)
class Signal:
    id: int
    group_id: str
    alias: str
    sender_id: str
    sender_name: str
    sender_role: str
    symbol: str
    action: str
    kind: str
    entry: str
    target: str
    stop: str
    quote: str
    sent_at: datetime


def retention_days() -> int:
    try:
        return max(7, min(365, int(os.getenv("ZALO_SIGNAL_RETENTION_DAYS", "90"))))
    except ValueError:
        return 90


async def _pool():
    await db.ensure_migrations()
    return await db.get_pool()


# ─── Chế độ quét theo nhóm ──────────────────────────────────────────────────


async def list_groups(account_id: str) -> list[tuple[str, str, str]]:
    """(group_id, alias, signal_mode) của mọi nhóm đang theo dõi /tongket."""
    rows = await (await _pool()).fetch(
        "SELECT group_id, alias, signal_mode FROM zalo_groups WHERE account_id = $1 ORDER BY alias",
        account_id,
    )
    return [(row["group_id"], row["alias"], row["signal_mode"]) for row in rows]


async def set_mode(account_id: str, target: str | None, mode: str) -> int:
    """Đặt chế độ cho 1 nhóm (alias hoặc group_id) hoặc mọi nhóm (target=None)."""
    if mode not in MODES:
        raise ValueError(mode)
    pool = await _pool()
    if target is None:
        result = await pool.execute(
            "UPDATE zalo_groups SET signal_mode = $2, updated_at = now() WHERE account_id = $1",
            account_id,
            mode,
        )
    else:
        result = await pool.execute(
            "UPDATE zalo_groups SET signal_mode = $3, updated_at = now() "
            "WHERE account_id = $1 AND (alias = $2 OR group_id = $2)",
            account_id,
            target,
            mode,
        )
    return int(result.split()[-1])


# ─── Người theo dõi thêm ────────────────────────────────────────────────────


async def add_person(account_id: str, group_id: str, sender_id: str, sender_name: str) -> None:
    await (await _pool()).execute(
        """
        INSERT INTO zalo_signal_people (account_id, group_id, sender_id, sender_name)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (account_id, group_id, sender_id)
        DO UPDATE SET sender_name = EXCLUDED.sender_name
        """,
        account_id,
        group_id,
        sender_id,
        sender_name[:200],
    )


async def remove_person(account_id: str, group_id: str, sender_id: str) -> bool:
    result = await (await _pool()).execute(
        "DELETE FROM zalo_signal_people WHERE account_id = $1 AND group_id = $2 AND sender_id = $3",
        account_id,
        group_id,
        sender_id,
    )
    return result == "DELETE 1"


async def list_people(account_id: str) -> dict[str, dict[str, str]]:
    """group_id -> {sender_id: tên}."""
    rows = await (await _pool()).fetch(
        "SELECT group_id, sender_id, sender_name FROM zalo_signal_people "
        "WHERE account_id = $1 ORDER BY sender_name",
        account_id,
    )
    people: dict[str, dict[str, str]] = {}
    for row in rows:
        people.setdefault(row["group_id"], {})[row["sender_id"]] = row["sender_name"]
    return people


async def recent_senders(account_id: str, group_id: str, days: int = 30, limit: int = 300):
    """Người đã nhắn trong nhóm gần đây, nhắn nhiều trước:
    (sender_id, tên mới nhất, vai trò mới nhất, số tin)."""
    rows = await (await _pool()).fetch(
        """
        SELECT sender_id, sender_name, sender_role, total FROM (
            SELECT DISTINCT ON (sender_id) sender_id, sender_name, sender_role,
                   count(*) OVER (PARTITION BY sender_id) AS total
            FROM zalo_group_messages
            WHERE account_id = $1 AND group_id = $2
              AND sent_at >= now() - ($3::integer * INTERVAL '1 day')
            ORDER BY sender_id, sent_at DESC
        ) latest
        ORDER BY total DESC, sender_id
        LIMIT $4
        """,
        account_id,
        group_id,
        days,
        limit,
    )
    return [
        (row["sender_id"], row["sender_name"], row["sender_role"], int(row["total"]))
        for row in rows
    ]


# ─── Quét tin ───────────────────────────────────────────────────────────────


async def load_cursor(account_id: str) -> int | None:
    raw = await db.get_setting(_CURSOR_KEY.format(account_id))
    try:
        return int(raw) if raw not in (None, "") else None
    except ValueError:
        return None


async def save_cursor(account_id: str, row_id: int) -> None:
    await db.set_setting(_CURSOR_KEY.format(account_id), str(int(row_id)))


async def settled_max_id(account_id: str) -> int:
    """id lớn nhất của tin nhóm đã lưu xong (0 nếu chưa có tin nào)."""
    value = await (await _pool()).fetchval(
        f"SELECT COALESCE(max(id), 0) FROM zalo_group_messages "
        f"WHERE account_id = $1 AND created_at < {_SETTLE_SQL}",
        account_id,
    )
    return int(value or 0)


async def messages_between(
    account_id: str, after_id: int, upto_id: int, limit: int
) -> list[GroupMessage]:
    """Tin của các nhóm ĐANG BẬT tín hiệu, after_id < id <= upto_id, theo thứ tự id."""
    rows = await (await _pool()).fetch(
        """
        SELECT m.id, m.group_id, g.alias, g.signal_mode, m.message_id, m.sender_id,
               m.sender_name, m.sender_role, m.content, m.quote_text, m.sent_at
        FROM zalo_group_messages m
        JOIN zalo_groups g ON g.account_id = m.account_id AND g.group_id = m.group_id
        WHERE m.account_id = $1 AND m.id > $2 AND m.id <= $3 AND g.signal_mode <> 'off'
        ORDER BY m.id
        LIMIT $4
        """,
        account_id,
        after_id,
        upto_id,
        limit,
    )
    return [
        GroupMessage(
            row_id=row["id"],
            group_id=row["group_id"],
            alias=row["alias"],
            mode=row["signal_mode"],
            message_id=row["message_id"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            sender_role=row["sender_role"],
            content=row["content"],
            quote_text=row["quote_text"],
            sent_at=row["sent_at"],
        )
        for row in rows
    ]


# ─── Tín hiệu ───────────────────────────────────────────────────────────────


async def save_signal(
    account_id: str,
    message: GroupMessage,
    *,
    symbol: str,
    action: str,
    kind: str,
    entry: str,
    target: str,
    stop: str,
    quote: str,
    signal_date: date,
) -> int | None:
    """id của tín hiệu mới; None nếu người này đã có tín hiệu cùng mã/hướng/loại hôm đó
    (nhóm vừa bị xoá cũng None - không có dòng nào được ghi)."""
    return await (await _pool()).fetchval(
        """
        INSERT INTO zalo_stock_signals (
            account_id, group_id, message_id, sender_id, sender_name, sender_role,
            symbol, action, kind, entry, target, stop, quote, sent_at, signal_date
        )
        SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
        WHERE EXISTS (SELECT 1 FROM zalo_groups WHERE account_id = $1 AND group_id = $2)
        ON CONFLICT (account_id, group_id, sender_id, symbol, action, kind, signal_date)
        DO NOTHING
        RETURNING id
        """,
        account_id,
        message.group_id,
        message.message_id,
        message.sender_id,
        message.sender_name[:200],
        message.sender_role,
        symbol,
        action,
        kind,
        entry[:60],
        target[:60],
        stop[:60],
        quote[:400],
        message.sent_at,
        signal_date,
    )


_SIGNAL_SELECT = """
    SELECT s.id, s.group_id, COALESCE(g.alias, s.group_id) AS alias, s.sender_id,
           s.sender_name, s.sender_role, s.symbol, s.action, s.kind, s.entry, s.target,
           s.stop, s.quote, s.sent_at
    FROM zalo_stock_signals s
    LEFT JOIN zalo_groups g ON g.account_id = s.account_id AND g.group_id = s.group_id
"""


def _signal(row) -> Signal:
    return Signal(
        id=row["id"],
        group_id=row["group_id"],
        alias=row["alias"],
        sender_id=row["sender_id"],
        sender_name=row["sender_name"],
        sender_role=row["sender_role"],
        symbol=row["symbol"],
        action=row["action"],
        kind=row["kind"],
        entry=row["entry"],
        target=row["target"],
        stop=row["stop"],
        quote=row["quote"],
        sent_at=row["sent_at"],
    )


def _sent_column(channel: str) -> str:
    if channel not in CHANNELS:
        raise ValueError(channel)
    return f"{channel}_sent_at"


async def claim_pending(
    account_id: str, channel: str, max_age_hours: int, limit: int
) -> list[Signal]:
    """Nhận (đánh dấu đã giao) tối đa ``limit`` tín hiệu chưa giao cho ``channel``, mới
    hơn ``max_age_hours`` giờ, cũ trước. Nguyên tử (SKIP LOCKED): 2 instance chạy chồng
    lúc deploy không gửi đôi. Gửi lỗi thì ``release`` để lượt sau thử lại."""
    column = _sent_column(channel)
    rows = await (await _pool()).fetch(
        f"""
        WITH picked AS (
            SELECT id FROM zalo_stock_signals
            WHERE account_id = $1 AND {column} IS NULL
              AND created_at >= now() - ($2::integer * INTERVAL '1 hour')
            ORDER BY id
            LIMIT $3
            FOR UPDATE SKIP LOCKED
        ), claimed AS (
            UPDATE zalo_stock_signals AS s SET {column} = now()
            FROM picked WHERE s.id = picked.id
            RETURNING s.*
        )
        SELECT c.id, c.group_id, COALESCE(g.alias, c.group_id) AS alias, c.sender_id,
               c.sender_name, c.sender_role, c.symbol, c.action, c.kind, c.entry, c.target,
               c.stop, c.quote, c.sent_at
        FROM claimed c
        LEFT JOIN zalo_groups g ON g.account_id = c.account_id AND g.group_id = c.group_id
        ORDER BY c.id
        """,
        account_id,
        max_age_hours,
        limit,
    )
    return [_signal(row) for row in rows]


async def already_delivered(account_id: str, channel: str, ids: list[int]) -> set[int]:
    """Trong ``ids``, các tín hiệu mà CÙNG người - mã - hướng - loại - ngày đã được giao
    cho ``channel`` từ một dòng khác (thường là cùng người nói ở nhóm khác): không báo lại."""
    if not ids:
        return set()
    column = _sent_column(channel)
    rows = await (await _pool()).fetch(
        f"""
        SELECT s.id FROM zalo_stock_signals s
        WHERE s.account_id = $1 AND s.id = ANY($2::bigint[]) AND EXISTS (
            SELECT 1 FROM zalo_stock_signals o
            WHERE o.account_id = s.account_id AND o.sender_id = s.sender_id
              AND o.symbol = s.symbol AND o.action = s.action AND o.kind = s.kind
              AND o.signal_date = s.signal_date AND o.{column} IS NOT NULL
              AND NOT (o.id = ANY($2::bigint[]))
        )
        """,
        account_id,
        ids,
    )
    return {row["id"] for row in rows}


async def release(ids: list[int], channel: str) -> None:
    """Trả tín hiệu đã nhận nhưng gửi lỗi về trạng thái chưa giao."""
    if not ids:
        return
    column = _sent_column(channel)
    await (await _pool()).execute(
        f"UPDATE zalo_stock_signals SET {column} = NULL WHERE id = ANY($1::bigint[])",
        ids,
    )


async def signals_since(
    account_id: str,
    start: datetime,
    *,
    end: datetime | None = None,
    symbol: str | None = None,
    limit: int = 30,
) -> list[Signal]:
    """Tín hiệu trong [start, end) (end None = tới giờ), mới nhất trước, lọc theo mã."""
    rows = await (await _pool()).fetch(
        _SIGNAL_SELECT
        + """
        WHERE s.account_id = $1 AND s.sent_at >= $2
          AND ($3::timestamptz IS NULL OR s.sent_at < $3)
          AND ($4::text IS NULL OR s.symbol = $4)
        ORDER BY s.sent_at DESC, s.id DESC
        LIMIT $5
        """,
        account_id,
        start,
        end,
        symbol,
        limit,
    )
    return [_signal(row) for row in rows]


async def top_symbols(
    account_id: str, start: datetime, end: datetime | None = None, limit: int = 10
):
    """(mã, hướng, số người khác nhau, số nhóm khác nhau, lần gần nhất) trong [start, end)
    (end None = tới giờ) - nhiều người trước."""
    rows = await (await _pool()).fetch(
        """
        SELECT symbol, action, count(DISTINCT sender_id) AS people,
               count(DISTINCT group_id) AS groups, max(sent_at) AS last_at
        FROM zalo_stock_signals
        WHERE account_id = $1 AND sent_at >= $2
          AND ($3::timestamptz IS NULL OR sent_at < $3)
        GROUP BY symbol, action
        ORDER BY people DESC, groups DESC, last_at DESC
        LIMIT $4
        """,
        account_id,
        start,
        end,
        limit,
    )
    return [
        (row["symbol"], row["action"], int(row["people"]), int(row["groups"]), row["last_at"])
        for row in rows
    ]


async def consensus(account_id: str, signal_date: date, symbols: list[str]):
    """{(mã, hướng): (số người, số nhóm)} trong ngày ``signal_date`` (giờ VN)."""
    if not symbols:
        return {}
    rows = await (await _pool()).fetch(
        """
        SELECT symbol, action, count(DISTINCT sender_id) AS people,
               count(DISTINCT group_id) AS groups
        FROM zalo_stock_signals
        WHERE account_id = $1 AND signal_date = $2 AND symbol = ANY($3::text[])
        GROUP BY symbol, action
        """,
        account_id,
        signal_date,
        sorted(set(symbols)),
    )
    return {
        (row["symbol"], row["action"]): (int(row["people"]), int(row["groups"])) for row in rows
    }


async def cleanup(account_id: str) -> None:
    await (await _pool()).execute(
        "DELETE FROM zalo_stock_signals "
        "WHERE account_id = $1 AND sent_at < now() - ($2::integer * INTERVAL '1 day')",
        account_id,
        retention_days(),
    )
