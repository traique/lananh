"""/tinhieu - tín hiệu mua/bán cổ phiếu từ nhóm Zalo (services/zalo_signals.py).

Dùng chung cho Zalo (admin), Telegram và Zoom qua channels.group_commands - cùng quyền
với /tongket. Không gọi AI: chỉ đọc/ghi cấu hình và tín hiệu đã lưu.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from channels import zalo_repository
from channels import zalo_signal_repository as repo
from core import config
from core import database as db
from core.text_normalize import fold
from services import zalo_signals
from services.channel_result import ChannelResult

USAGE = """📈 /tinhieu — tín hiệu mua/bán cổ phiếu từ nhóm Zalo (gửi Zoom + Zalo)
/tinhieu — tình trạng + tín hiệu hôm nay
/tinhieu on [nhóm] [admin|tatca] — bật quét (mặc định chỉ trưởng/phó nhóm; bỏ trống nhóm = mọi nhóm)
/tinhieu off [nhóm] — tắt quét
/tinhieu nhom — chế độ từng nhóm và người theo dõi thêm
/tinhieu nguoi <nhóm> <tên|id> — theo dõi thêm 1 người không phải admin; /tinhieu bonguoi <nhóm> <tên|id> — bỏ
/tinhieu <mã> [homnay|7d|30d] — ai đã gợi ý mã đó, ở nhóm nào
/tinhieu top [homnay|7d|30d] — mã được nhiều người nói nhất
/tinhieu homnay|homqua|7d — danh sách tín hiệu
/tinhieu nhip <phút> — nhịp quét (1-60 phút, mặc định 2)
Nhóm phải được theo dõi trước bằng /themnhom."""

# "all" KHÔNG phải chế độ: như /tongket on all, nó nghĩa là mọi nhóm.
_MODE_WORDS = {"admin": "admin", "tatca": "all", "tat ca": "all"}
_MODE_LABELS = {"off": "🔕 tắt", "admin": "👑 chỉ admin nhóm", "all": "👥 mọi thành viên"}
_WINDOWS = ("homnay", "homqua", "24h", "7d", "30d")
_SUBCOMMANDS = {"on", "off", "nhom", "nguoi", "bonguoi", "top", "nhip", *_WINDOWS}
_SYMBOL_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,3}")
_ID_RE = re.compile(r"\d{6,25}")
_LIST_LIMIT = 15
_MATCH_LIMIT = 8


def _window(spec: str, now: datetime) -> tuple[datetime, datetime | None, str]:
    """(bắt đầu, kết thúc hoặc None = tới giờ, nhãn)."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if spec == "homnay":
        return today, None, "hôm nay"
    if spec == "homqua":
        return today - timedelta(days=1), today, "hôm qua"
    if spec == "24h":
        return now - timedelta(hours=24), None, "24 giờ qua"
    if spec == "30d":
        return now - timedelta(days=30), None, "30 ngày qua"
    return now - timedelta(days=7), None, "7 ngày qua"


def _now() -> datetime:
    return datetime.now(zalo_signals.VN_TZ)


async def _signals(account_id: str, spec: str, symbol: str | None = None):
    start, end, label = _window(spec, _now())
    rows = await repo.signals_since(account_id, start, end=end, symbol=symbol, limit=60)
    return rows, label


def _render_signals(rows, title: str, empty: str) -> list[str]:
    if not rows:
        return [empty]
    shown = rows[:_LIST_LIMIT]
    body = "\n\n".join(zalo_signals.format_signal(row) for row in shown)
    more = f"\n\n… và {len(rows) - len(shown)} tín hiệu khác." if len(rows) > len(shown) else ""
    return [f"{title}\n\n{body}{more}\n\n{zalo_signals.DISCLAIMER}"]


async def _channel_lines() -> list[str]:
    lines = []
    wanted = zalo_signals.channels()
    if "zoom" in wanted:
        if not config.ZOOM_ENABLED:
            lines.append("• Zoom: đang tắt → gửi về Telegram")
        elif await db.zoom_get_pairing():
            lines.append("• Zoom: ✅ đã ghép đôi")
        else:
            lines.append("• Zoom: ⚠️ chưa ghép đôi (/zoompair) → gửi về Telegram")
    if "zalo" in wanted:
        controller = await zalo_signals.zalo_controller()
        lines.append("• Zalo: ✅ Zalo chủ bot" if controller else "• Zalo: ⚠️ chưa ghép đôi chủ bot")
    return lines or ["• Không có kênh nào (ZALO_SIGNAL_CHANNELS)"]


async def _status(account_id: str) -> ChannelResult:
    groups = await repo.list_groups(account_id)
    active = [(alias, mode) for _, alias, mode in groups if mode != "off"]
    interval = await zalo_signals.interval_minutes()
    lines = ["📈 TÍN HIỆU CỔ PHIẾU TỪ NHÓM ZALO"]
    if not zalo_signals.enabled():
        lines.append("⚠️ Vòng quét đang tắt (cần ZALO_ENABLED, ZALO_BRIDGE_SECRET; ZALO_SIGNALS≠0).")
    if not groups:
        lines.append(
            "Chưa theo dõi nhóm nào: /nhomzalo rồi /themnhom <số>, sau đó /tinhieu on <nhóm>."
        )
    elif not active:
        lines.append("Chưa nhóm nào bật quét: /tinhieu on <nhóm> (chỉ admin nhóm) hoặc thêm tatca.")
    else:
        lines.append(
            "Đang quét: " + ", ".join(f"{alias} ({_MODE_LABELS[mode]})" for alias, mode in active)
        )
    lines.append(
        f"⏱ Nhịp quét {interval} phút · AI {zalo_signals.ai_calls_last_hour()}/"
        f"{zalo_signals.max_ai_per_hour()} lượt trong giờ qua"
    )
    lines.append("Gửi tới:")
    lines.extend(await _channel_lines())
    messages = ["\n".join(lines)]
    rows, _ = await _signals(account_id, "homnay")
    messages.extend(
        _render_signals(rows, "📋 Tín hiệu hôm nay:", "📭 Hôm nay chưa có tín hiệu nào.")
    )
    messages.append("Xem cách dùng: /tinhieu help")
    return ChannelResult(messages)


def _split_mode(args: list[str]) -> tuple[str | None, str]:
    """(nhóm hoặc None = mọi nhóm, chế độ) từ phần sau /tinhieu on."""
    mode = "admin"
    rest = list(args)
    if len(rest) >= 2 and fold(" ".join(rest[-2:])) in _MODE_WORDS:
        mode = _MODE_WORDS[fold(" ".join(rest[-2:]))]
        rest = rest[:-2]
    elif rest and fold(rest[-1]) in _MODE_WORDS:
        mode = _MODE_WORDS[fold(rest[-1])]
        rest = rest[:-1]
    target = " ".join(rest).strip() or None
    if target is not None and target.lower() == "all":
        target = None
    return target, mode


async def _toggle(account_id: str, args: list[str], on: bool) -> ChannelResult:
    if on:
        target, mode = _split_mode(args)
    else:
        target = " ".join(args).strip() or None
        if target is not None and target.lower() == "all":
            target = None
        mode = "off"
    count = await repo.set_mode(account_id, target, mode)
    if count == 0:
        if target is None:
            return ChannelResult(
                ["Chưa theo dõi nhóm nào. Gõ /nhomzalo rồi /themnhom <số> trước nhé."]
            )
        return ChannelResult([f"Không tìm thấy nhóm “{target}” trong danh sách /nhom."])
    where = f"nhóm {target}" if target else f"{count} nhóm"
    if not on:
        return ChannelResult([f"✅ Đã TẮT quét tín hiệu cho {where}."])
    interval = await zalo_signals.interval_minutes()
    who = (
        "chỉ trưởng/phó nhóm (thêm người khác: /tinhieu nguoi <nhóm> <tên>)"
        if mode == "admin"
        else "mọi thành viên"
    )
    return ChannelResult(
        [
            f"✅ Đã BẬT quét tín hiệu cho {where}: {who}.\n"
            f"Tin mới được quét mỗi {interval} phút, có tín hiệu thì em gửi Zoom + Zalo."
        ]
    )


async def _groups(account_id: str) -> ChannelResult:
    groups = await repo.list_groups(account_id)
    if not groups:
        return ChannelResult(["Chưa theo dõi nhóm nào. Gõ /nhomzalo rồi /themnhom <số>."])
    people = await repo.list_people(account_id)
    lines = ["📚 Quét tín hiệu theo nhóm:"]
    for index, (group_id, alias, mode) in enumerate(groups, 1):
        lines.append(f"{index}. {alias} — {_MODE_LABELS[mode]}")
        extra = people.get(group_id) or {}
        if extra:
            names = ", ".join(name or sender_id for sender_id, name in extra.items())
            lines.append(f"   + theo dõi thêm: {names}")
    lines.append("Đổi: /tinhieu on <nhóm> [admin|tatca] · /tinhieu off <nhóm>")
    return ChannelResult(["\n".join(lines)])


def _matches(query: str, candidates: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Khớp tên không dấu: trùng hẳn trước, không có thì chứa chuỗi; id thì khớp id."""
    if _ID_RE.fullmatch(query):
        return [c for c in candidates if c[0] == query]
    wanted = fold(query)
    exact = [c for c in candidates if fold(c[1]) == wanted]
    return exact or [c for c in candidates if wanted and wanted in fold(c[1])]


def _role(role: str) -> str:
    label = zalo_signals.ROLE_LABELS.get(role)
    return f" ({label})" if label else ""


def _no_group(target: str) -> ChannelResult:
    return ChannelResult([f"Không tìm thấy nhóm “{target}” trong danh sách /nhom."])


async def _add_person(account_id: str, args: list[str]) -> ChannelResult:
    if len(args) < 2:
        return ChannelResult(["Cú pháp: /tinhieu nguoi <nhóm> <tên hoặc id người gửi>"])
    group = await zalo_repository.resolve_group(account_id, args[0])
    if group is None:
        return _no_group(args[0])
    group_id, alias = group
    query = " ".join(args[1:]).strip()
    senders = [
        (sender_id, name, role)
        for sender_id, name, role, _ in await repo.recent_senders(account_id, group_id)
    ]
    found = _matches(query, senders)
    if not found and _ID_RE.fullmatch(query):
        found = [(query, "", "")]
    if not found:
        return ChannelResult([f"Không thấy ai tên “{query}” nhắn trong nhóm {alias} 30 ngày qua."])
    if len(found) > 1:
        lines = [f"Có {len(found)} người khớp “{query}” trong nhóm {alias}:"]
        lines.extend(
            f"• {name or sender_id}{_role(role)} — id {sender_id}"
            for sender_id, name, role in found[:_MATCH_LIMIT]
        )
        lines.append(f"Gõ lại theo id: /tinhieu nguoi {alias} <id>")
        return ChannelResult(["\n".join(lines)])
    sender_id, name, role = found[0]
    await repo.add_person(account_id, group_id, sender_id, name)
    note = ""
    if role in zalo_signals.ADMIN_ROLES:
        note = " (người này đã là admin nhóm nên vốn được theo dõi)"
    return ChannelResult([f"✅ Đã theo dõi thêm {name or sender_id} trong nhóm {alias}{note}."])


async def _remove_person(account_id: str, args: list[str]) -> ChannelResult:
    if len(args) < 2:
        return ChannelResult(["Cú pháp: /tinhieu bonguoi <nhóm> <tên hoặc id>"])
    group = await zalo_repository.resolve_group(account_id, args[0])
    if group is None:
        return _no_group(args[0])
    group_id, alias = group
    query = " ".join(args[1:]).strip()
    people = (await repo.list_people(account_id)).get(group_id) or {}
    found = _matches(query, [(sender_id, name, "") for sender_id, name in people.items()])
    if len(found) != 1:
        return ChannelResult(
            [f"Không xác định được 1 người “{query}” trong danh sách theo dõi thêm của {alias}."]
        )
    sender_id, name, _ = found[0]
    await repo.remove_person(account_id, group_id, sender_id)
    return ChannelResult([f"✅ Đã bỏ theo dõi thêm {name or sender_id} trong nhóm {alias}."])


async def _top(account_id: str, spec: str) -> ChannelResult:
    start, end, label = _window(spec, _now())
    rows = await repo.top_symbols(account_id, start, end)
    if not rows:
        return ChannelResult([f"📭 Chưa có tín hiệu nào trong {label}."])
    lines = [f"🏆 Mã được nhiều người nói nhất ({label}):"]
    for index, (symbol, action, people, groups, last_at) in enumerate(rows, 1):
        verb = "🟢 mua" if action == "buy" else "🔴 bán"
        when = last_at.astimezone(zalo_signals.VN_TZ).strftime("%H:%M %d/%m")
        lines.append(f"{index}. {symbol} {verb} — {people} người · {groups} nhóm (gần nhất {when})")
    lines.append(f"Chi tiết: /tinhieu <mã> · {zalo_signals.DISCLAIMER}")
    return ChannelResult(["\n".join(lines)])


async def _interval(args: list[str]) -> ChannelResult:
    if not args:
        current = await zalo_signals.interval_minutes()
        return ChannelResult([f"⏱ Nhịp quét hiện tại: {current} phút. Đổi: /tinhieu nhip <1-60>"])
    if not args[0].isdigit():
        return ChannelResult(["Cú pháp: /tinhieu nhip <số phút từ 1 đến 60>, vd /tinhieu nhip 30"])
    value = await zalo_signals.set_interval_minutes(int(args[0]))
    return ChannelResult([f"✅ Từ giờ em quét tin nhóm mỗi {value} phút."])


async def handle(account_id: str, raw: str) -> ChannelResult:
    parts = raw.split()[1:]
    if not parts:
        return await _status(account_id)
    head = parts[0].lower()
    args = parts[1:]
    if head in {"help", "huongdan", "?"}:
        return ChannelResult([USAGE])
    if head in {"on", "off"}:
        return await _toggle(account_id, args, head == "on")
    if head == "nhom":
        return await _groups(account_id)
    if head == "nguoi":
        return await _add_person(account_id, args)
    if head == "bonguoi":
        return await _remove_person(account_id, args)
    if head == "top":
        spec = args[0].lower() if args and args[0].lower() in _WINDOWS else "7d"
        return await _top(account_id, spec)
    if head == "nhip":
        return await _interval(args)
    if head in _WINDOWS:
        rows, label = await _signals(account_id, head)
        return ChannelResult(
            _render_signals(
                rows, f"📋 Tín hiệu {label}:", f"📭 Chưa có tín hiệu nào trong {label}."
            )
        )
    window_only = not args or (len(args) == 1 and args[0].lower() in _WINDOWS)
    if _SYMBOL_RE.fullmatch(parts[0]) and head not in _SUBCOMMANDS and window_only:
        symbol = parts[0].upper()
        spec = args[0].lower() if args else "7d"
        rows, label = await _signals(account_id, spec, symbol)
        return ChannelResult(
            _render_signals(
                rows,
                f"🔎 Ai đã nói về {symbol} ({label}):",
                f"📭 Chưa ai trong các nhóm đang quét nói mua/bán {symbol} trong {label}.",
            )
        )
    return ChannelResult([USAGE])
