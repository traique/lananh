"""Test lệnh /lich, /tsh, /tuvi và *_chitiet trên mọi kênh."""

import pytest

from services import channel_command_service as ccs
from services import divination_commands, numerology, tuvi


@pytest.fixture
def telemetry_log(monkeypatch):
    log = []

    async def start(user_id, kind, prompt, channel="telegram"):
        log.append(("start", kind, prompt))
        return 7

    async def success(prompt_id, kind, content):
        log.append(("success", kind, content))

    async def failure(prompt_id, kind, exc):
        log.append(("failure", kind, type(exc).__name__))

    monkeypatch.setattr(divination_commands.telemetry, "start", start)
    monkeypatch.setattr(divination_commands.telemetry, "success", success)
    monkeypatch.setattr(divination_commands.telemetry, "failure", failure)
    return log


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/lich trung thu", "Tết Trung thu"),
        ("/lich", "Giờ hoàng đạo"),
        ("/tsh 15/3/1990", "Con số chủ đạo: 10"),
        ("/tuvi 15/3/1990 9h30 nam", "Cục: Thổ ngũ cục"),
        ("/tuvi 15/3/1990", "Thiếu giờ sinh"),
    ],
)
async def test_lenh_khong_ai_tren_zalo_zoom(text, expected, monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("không được gọi AI")

    monkeypatch.setattr(ccs.orchestrator, "ask", boom)
    result = await ccs.maybe_handle_command(1, text, is_admin=False, channel="zalo")
    assert result is not None
    messages, _ = result
    assert expected in messages[0]


async def test_chitiet_tra_ket_qua_tinh_san_roi_ai(monkeypatch, telemetry_log):
    async def fake_explain(reading):
        return "AI diễn giải"

    monkeypatch.setattr(numerology, "explain_with_ai", fake_explain)
    result = await ccs.maybe_handle_command(
        1, "/ths_chitiet 15/3/1990 Nguyễn Văn An", channel="zoom"
    )
    messages, _ = result
    assert "Con số chủ đạo: 10" in messages[0]
    assert messages[1] == "AI diễn giải"
    # Telemetry chỉ ghi tên lệnh và độ dài, KHÔNG ghi ngày sinh/họ tên/nội dung.
    assert telemetry_log == [
        ("start", "tsh_chitiet", "/tsh_chitiet"),
        ("success", "tsh_chitiet", "(12 ký tự)"),
    ]


async def test_chitiet_ai_loi_van_tra_la_so(monkeypatch, telemetry_log):
    async def fail(chart):
        raise RuntimeError("provider chết")

    monkeypatch.setattr(tuvi, "explain_with_ai", fail)
    messages = await divination_commands.tuvi_chitiet(1, "15/3/1990 9h30 nam", "telegram")
    assert "Lá số Tử Vi" in messages[0]
    assert "AI chưa luận giải được" in messages[1]
    assert telemetry_log[-1] == ("failure", "tuvi_chitiet", "RuntimeError")


async def test_chitiet_sai_cu_phap_khong_goi_ai(telemetry_log):
    assert await divination_commands.tuvi_chitiet(1, "abc", "zalo") == [tuvi.USAGE]
    assert telemetry_log == []


def test_help_va_menu_telegram_co_lenh_moi():
    import bot_app
    from handlers import commands

    names = {c.command for c in bot_app.COMMANDS}
    assert {"lich", "tsh", "tsh_chitiet", "tuvi", "tuvi_chitiet"} <= names
    for cmd in ("/lich", "/tsh", "/tuvi"):
        assert cmd in commands.HELP_TEXT
        assert cmd in ccs.HELP


def test_help_telegram_chia_tin_duoi_gioi_han():
    from handlers import commands

    chunks = commands.help_chunks()
    assert len(chunks) >= 2
    assert all(len(c) <= 3500 for c in chunks)
    assert "\n\n".join(chunks) == commands.HELP_TEXT
    assert all(c.count("*") % 2 == 0 for c in chunks)  # Markdown cân dấu từng tin
