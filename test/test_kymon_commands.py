"""Test lệnh /kymon, /kymon_chitiet trên Telegram, Zalo và Zoom."""

import datetime as dt
from types import SimpleNamespace

import pytest

from core import config
from messages import friendly_error
from services import channel_command_service as ccs
from services import divination_commands, kymon

_NOW = dt.datetime(2026, 1, 11, 8, 30, tzinfo=kymon.VN_TZ)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(kymon, "_now", lambda: _NOW)


@pytest.fixture
def telemetry_log(monkeypatch):
    log = []

    async def start(user_id, kind, prompt, channel="telegram"):
        log.append(("start", kind, prompt, channel))
        return 7

    async def success(prompt_id, kind, content):
        log.append(("success", kind, content))

    async def failure(prompt_id, kind, exc):
        log.append(("failure", kind, type(exc).__name__))

    monkeypatch.setattr(divination_commands.telemetry, "start", start)
    monkeypatch.setattr(divination_commands.telemetry, "success", success)
    monkeypatch.setattr(divination_commands.telemetry, "failure", failure)
    return log


@pytest.fixture
def no_ai(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("không được gọi AI")

    monkeypatch.setattr(ccs.orchestrator, "ask", boom)


@pytest.mark.parametrize("channel", ["zalo", "zoom"])
@pytest.mark.parametrize(
    "text,expected",
    [
        ("/kymon", "08:30 Chủ Nhật 11/01/2026"),
        ("/kymon 22/1/2026 12:30", "Dương độn 3 cục"),
        ("/kymon@lananh_bot 14h", "14:00"),
        ("/kymon 31/2/2026", "Ngày không hợp lệ"),
    ],
)
async def test_kymon_khong_ai_tren_zalo_zoom(no_ai, channel, text, expected):
    result = await ccs.maybe_handle_command(1, text, is_admin=False, channel=channel)
    assert result is not None
    messages, image = result
    assert len(messages) == 1 and expected in messages[0] and image is None


async def test_kymon_chitiet_ban_tinh_san_roi_ai(monkeypatch, telemetry_log):
    seen = {}

    async def fake_explain(chart, question):
        seen["args"] = (chart.ju, chart.moment.strftime("%H:%M"), question)
        return "AI luận giải"

    monkeypatch.setattr(kymon, "explain_with_ai", fake_explain)
    messages, _ = await ccs.maybe_handle_command(
        1, "/kymon_chitiet 9h ký hợp đồng có thuận không", channel="zoom"
    )
    assert "🧭 Kỳ Môn Độn Giáp - 09:00" in messages[0] and messages[1] == "AI luận giải"
    assert seen["args"][1:] == ("09:00", "ký hợp đồng có thuận không")
    # Telemetry chỉ ghi tên lệnh và độ dài, KHÔNG ghi việc cần xem hay nội dung luận giải.
    assert telemetry_log == [
        ("start", "kymon_chitiet", "/kymon_chitiet", "zoom"),
        ("success", "kymon_chitiet", "(12 ký tự)"),
    ]


async def test_kymon_chitiet_ai_loi_van_tra_ban(monkeypatch, telemetry_log):
    async def fail(chart, question):
        raise RuntimeError("provider chết")

    monkeypatch.setattr(kymon, "explain_with_ai", fail)
    messages = await divination_commands.kymon_chitiet(1, "đi xa", "telegram")
    assert messages[0].startswith("🧭 Kỳ Môn Độn Giáp") and "AI chưa luận giải được" in messages[1]
    assert telemetry_log[-1] == ("failure", "kymon_chitiet", "RuntimeError")


async def test_kymon_chitiet_het_han_chot_bao_ro(monkeypatch, telemetry_log):
    from ai.timeouts import TurnTimeoutError

    async def slow(chart, question):
        raise TurnTimeoutError("Hết thời gian trả lời")

    monkeypatch.setattr(kymon, "explain_with_ai", slow)
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    # Thành viên Zalo: câu giọng Lan Anh, không lộ thông điệp lỗi kỹ thuật.
    messages = await divination_commands.kymon_chitiet(-1, "đi xa", "zalo")
    assert messages[1] == friendly_error("timeout")
    assert "Hết thời gian trả lời" not in messages[1]
    # Chủ bot: cùng câu đó kèm chi tiết kỹ thuật.
    messages = await divination_commands.kymon_chitiet(42, "đi xa", "telegram")
    assert messages[1].startswith(friendly_error("timeout", owner=True).split("\n")[0])
    assert "Hết thời gian trả lời" in messages[1]


async def test_kymon_chitiet_thieu_viec_khong_goi_ai(no_ai, telemetry_log):
    assert await divination_commands.kymon_chitiet(1, "", "zalo") == [kymon.QUESTION_USAGE]
    assert await divination_commands.kymon_chitiet(1, "25/10/2026 9:00", "zalo") == [
        kymon.QUESTION_USAGE
    ]
    assert telemetry_log == []


def test_help_va_menu_co_kymon():
    import bot_app
    from handlers import commands

    names = {c.command for c in bot_app.COMMANDS}
    assert {"kymon", "kymon_chitiet"} <= names
    assert "/kymon " in commands.HELP_TEXT and "/kymon\\_chitiet" in commands.HELP_TEXT
    assert "/kymon " in ccs.HELP and "/kymon_chitiet" in ccs.HELP
    assert all(len(c) <= 3500 for c in commands.help_chunks())


class _Message:
    def __init__(self):
        self.replies: list[str] = []


async def test_telegram_kymon_va_chitiet(monkeypatch):
    from handlers import commands, common

    monkeypatch.setattr(common, "check_access", lambda update: True)
    sent: list[str] = []

    async def reply_long_text(message, text):
        sent.append(text)

    async def send_chat_action(**kwargs):
        sent.append("typing")

    async def fake_explain(chart, question):
        return f"luận: {question}"

    monkeypatch.setattr(common, "reply_long_text", reply_long_text)
    monkeypatch.setattr(kymon, "explain_with_ai", fake_explain)
    monkeypatch.setattr(divination_commands.telemetry, "start", _async_value(1))
    monkeypatch.setattr(divination_commands.telemetry, "success", _async_value(None))
    update = SimpleNamespace(
        message=_Message(),
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
    )
    context = SimpleNamespace(args=["22/1/2026", "12:30"], bot=SimpleNamespace())
    await commands.kymon_cmd(update, context)
    assert len(sent) == 1 and "Dương độn 3 cục" in sent[0]

    sent.clear()
    context = SimpleNamespace(
        args=["8h", "gặp", "khách"], bot=SimpleNamespace(send_chat_action=send_chat_action)
    )
    await commands.kymon_chitiet_cmd(update, context)
    assert sent[0] == "typing" and sent[1].startswith("🧭 Kỳ Môn Độn Giáp - 08:00")
    assert sent[2] == "luận: gặp khách"


def _async_value(value):
    async def inner(*args, **kwargs):
        return value

    return inner
