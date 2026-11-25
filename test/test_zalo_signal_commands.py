"""Lệnh /tinhieu (channels/zalo_signal_commands.py) qua dispatcher nhóm dùng chung."""

from datetime import datetime, timezone

import pytest

from channels import group_commands
from channels import zalo_signal_commands as cmd
from channels.zalo_signal_repository import Signal
from services import zalo_signals


def _signal(**kw):
    values = dict(
        id=1,
        group_id="g1",
        alias="room-vip",
        sender_id="boss",
        sender_name="Tuấn Phạm",
        sender_role="owner",
        symbol="HPG",
        action="buy",
        kind="recommend",
        entry="",
        target="",
        stop="",
        quote="HPG gom dần",
        sent_at=datetime(2026, 11, 25, 3, 0, tzinfo=timezone.utc),
    )
    values.update(kw)
    return Signal(**values)


class FakeRepo:
    def __init__(self):
        self.modes = []
        self.people = {"g1": {"expert": "Chuyên Gia"}}
        self.added = []
        self.removed = []
        self.signals = [_signal()]
        self.senders = [
            ("u1", "Minh Anh", "member", 5),
            ("u2", "Minh Tuấn", "admin", 3),
            ("u3", "Hoa", "member", 1),
        ]

    async def set_mode(self, account_id, target, mode):
        self.modes.append((target, mode))
        return 0 if target == "khong-co" else (3 if target is None else 1)

    async def list_groups(self, account_id):
        return [("g1", "room-vip", "admin"), ("g2", "gia-dinh", "off")]

    async def list_people(self, account_id):
        return self.people

    async def recent_senders(self, account_id, group_id):
        return self.senders

    async def add_person(self, account_id, group_id, sender_id, name):
        self.added.append((group_id, sender_id, name))

    async def remove_person(self, account_id, group_id, sender_id):
        self.removed.append((group_id, sender_id))
        return True

    async def signals_since(self, account_id, start, *, symbol=None, limit=30):
        return [s for s in self.signals if symbol in (None, s.symbol)]

    async def top_symbols(self, account_id, start, limit=10):
        return [("HPG", "buy", 3, 2, datetime(2026, 11, 25, 3, 0, tzinfo=timezone.utc))]


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    monkeypatch.setattr(cmd, "repo", fake)

    async def resolve_group(account_id, target):
        return ("g1", "room-vip") if target in ("room-vip", "g1") else None

    monkeypatch.setattr(cmd.zalo_repository, "resolve_group", resolve_group)
    return fake


async def _run(text):
    result = await group_commands.maybe_handle_group_command("B", text)
    assert result is not None
    return "\n".join(result.messages)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/tinhieu on room-vip", ("room-vip", "admin")),
        ("/tinhieu on room-vip tatca", ("room-vip", "all")),
        ("/tinhieu on room-vip tất cả", ("room-vip", "all")),
        ("/tinhieu on", (None, "admin")),
        ("/tinhieu on all", (None, "admin")),
        ("/tinhieu on tatca", (None, "all")),
        ("/tinhieu off room-vip", ("room-vip", "off")),
        ("/tinhieu off", (None, "off")),
    ],
)
async def test_bat_tat_theo_nhom_va_che_do(repo, text, expected):
    reply = await _run(text)
    assert repo.modes == [expected]
    assert reply.startswith("✅")


async def test_bat_nhom_khong_co(repo):
    assert "Không tìm thấy nhóm “khong-co”" in await _run("/tinhieu on khong-co")


async def test_them_nguoi_theo_ten_khong_dau_hoac_id(repo):
    reply = await _run("/tinhieu nguoi room-vip minh")
    assert "Có 2 người khớp" in reply and "id u1" in reply and repo.added == []
    reply = await _run("/tinhieu nguoi room-vip hoa")
    assert repo.added == [("g1", "u3", "Hoa")] and "Đã theo dõi thêm Hoa" in reply
    reply = await _run("/tinhieu nguoi room-vip Minh Tuấn")
    assert "đã là admin nhóm" in reply
    assert "Không thấy ai" in await _run("/tinhieu nguoi room-vip Lan")
    await _run("/tinhieu nguoi room-vip 1234567890")
    assert repo.added[-1] == ("g1", "1234567890", "")
    assert "Không tìm thấy nhóm" in await _run("/tinhieu nguoi lạ hoa")


async def test_bo_nguoi(repo):
    reply = await _run("/tinhieu bonguoi room-vip chuyen gia")
    assert repo.removed == [("g1", "expert")] and "Chuyên Gia" in reply


async def test_tra_ma_va_top(repo):
    reply = await _run("/tinhieu hpg")
    assert "🔎 Ai đã nói về HPG (7 ngày qua)" in reply and "Tuấn Phạm (trưởng nhóm)" in reply
    assert zalo_signals.DISCLAIMER in reply
    assert "Chưa ai" in await _run("/tinhieu SSI 30d")
    assert "1. HPG 🟢 mua — 3 người · 2 nhóm" in await _run("/tinhieu top")


async def test_nhom_va_help(repo):
    reply = await _run("/tinhieu nhom")
    assert "room-vip — 👑 chỉ admin nhóm" in reply and "theo dõi thêm: Chuyên Gia" in reply
    assert "gia-dinh — 🔕 tắt" in reply
    assert "/tinhieu nguoi" in await _run("/tinhieu help")
    assert "/tinhieu on" in await _run("/tinhieu linh tinh qua")


async def test_nhip_quet(repo, monkeypatch):
    seen = []

    async def set_interval(value):
        seen.append(value)
        return min(value, 60)

    async def interval():
        return 2

    monkeypatch.setattr(cmd.zalo_signals, "set_interval_minutes", set_interval)
    monkeypatch.setattr(cmd.zalo_signals, "interval_minutes", interval)
    assert "mỗi 30 phút" in await _run("/tinhieu nhip 30")
    assert seen == [30]
    assert "2 phút" in await _run("/tinhieu nhip")
    assert "Cú pháp" in await _run("/tinhieu nhip nhanh")


async def test_tinh_trang(repo, monkeypatch):
    async def interval():
        return 2

    async def no_pairing():
        return None

    async def controller():
        return "ctrl"

    monkeypatch.setattr(cmd.zalo_signals, "interval_minutes", interval)
    monkeypatch.setattr(cmd.zalo_signals, "zalo_controller", controller)
    monkeypatch.setattr(cmd.db, "zoom_get_pairing", no_pairing)
    monkeypatch.setattr(cmd.config, "ZOOM_ENABLED", True)
    reply = await _run("/tinhieu")
    assert "Đang quét: room-vip (👑 chỉ admin nhóm)" in reply
    assert "Zoom: ⚠️ chưa ghép đôi" in reply and "Zalo: ✅" in reply
    assert "📋 Tín hiệu hôm nay" in reply
