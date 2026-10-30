"""Hỏi tự nhiên thay cho lệnh (services/nl_commands.py, services/command_catalog.py):
đường code (trợ giúp, mẫu câu rõ), ToolSpec "chạy lệnh" qua router chat (tham số ->
đúng dòng lệnh), câu chat thường không bị bắt, quyền theo vai trò, lệnh nguy hiểm
không bao giờ tự chạy, hỏi lại khi thiếu tham số, /help theo chủ đề, menu Telegram."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ai import official_client, orchestrator
from core import config
from services import command_catalog as catalog
from services import nl_commands as nl
from services import tool_registry as reg
from services import tools, usage_quota, web_search

OWNER = 42
MEMBER = -5
ADMIN = -6
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", OWNER)
    monkeypatch.delenv("NL_COMMANDS_ENABLED", raising=False)
    monkeypatch.delenv("NL_COMMANDS_LLM", raising=False)

    async def no_history(user_id, turns=3):
        return ""

    async def chain_down(prompt, **kwargs):
        raise RuntimeError("provider-chain lỗi mô phỏng")

    async def heavy_ok(user_id, *, channel, is_admin):
        return True

    monkeypatch.setattr(web_search, "recent_history_text", no_history)
    monkeypatch.setattr(orchestrator, "ask", chain_down)
    monkeypatch.setattr(usage_quota, "heavy_available", heavy_ok)
    nl._pending.clear()
    yield
    nl._pending.clear()


@pytest.fixture
def lines(monkeypatch):
    """Ghi lại dòng lệnh mà câu tự nhiên được dịch thành (không chạy lệnh thật)."""
    seen: list[str] = []

    async def fake_run_line(user_id, line, *, channel, is_admin):
        seen.append(line)
        return nl.CommandRun((f"KẾT QUẢ {line}",))

    monkeypatch.setattr(nl, "run_line", fake_run_line)
    return seen


def _router(monkeypatch, data):
    async def fake(prompt):
        fake.prompts.append(prompt)
        return data

    fake.prompts = []
    monkeypatch.setattr(official_client, "generate_utility_json", fake)
    return fake


# ─── Danh bạ lệnh: đủ mọi lệnh, đường tự nhiên trỏ đúng ToolSpec ──────────


def _telegram_handler_names() -> set[str]:
    source = (ROOT / "bot_app.py").read_text(encoding="utf-8")
    block = source.split("command_handlers = [", 1)[1].split("]\n", 1)[0]
    return set(re.findall(r'\("([a-z_0-9]+)",', block))


def test_danh_ba_co_moi_lenh_telegram_va_zalo():
    import bot_app

    names = _telegram_handler_names()
    assert len(names) >= 100
    missing = sorted(n for n in names if catalog.get(n) is None)
    assert missing == []
    assert {c.command for c in bot_app.ALL_COMMANDS} <= {
        n for e in catalog.ENTRIES for n in (e.name, *e.aliases)
    }
    for entry in catalog.ENTRIES:
        assert entry.group in catalog.GROUP_LABELS, entry.name
        assert entry.topic == "chung" or entry.topic in {t.key for t in catalog.TOPICS}
        for route in entry.nl.split(" + "):
            kind, _, name = route.partition(":")
            assert kind in {"code", "tool", "cmd", "set", "suggest", "-"}, (entry.name, route)
            if kind in {"cmd", "set"}:
                assert name in nl.COMMANDS, (entry.name, name)
            if kind == "tool":
                assert reg.get(name) is not None, (entry.name, name)


def test_lenh_quan_tri_nguy_hiem_khong_co_toolspec_chay():
    """fb_*, zalo*, /forget, /xoadulieu, /reset, /router9, /model... chỉ được CHỈ RA."""
    wrapped = {COMMAND.entry for COMMAND in nl.COMMANDS.values()}
    for entry in catalog.ENTRIES:
        if entry.group in (catalog.DANGER, catalog.ADMIN):
            assert entry.name not in wrapped, entry.name
            assert entry.suggest_only or entry.nl == "-", entry.name
    for name in ("forget", "xoadulieu", "reset", "router9", "model", "fb_ok", "fb_reset",
                 "zalokhoa", "zaloxoa", "themnhom", "xoanhom", "bangtinsang"):  # fmt: skip
        assert catalog.get(name).suggest_only, name
    # Mọi ToolSpec "chạy lệnh" ghi dữ liệu đều là bật/tắt cài đặt (set_*).
    for spec in reg.specs(reg.COMMAND):
        assert spec.verbatim
        assert spec.side_effect == spec.name.startswith("set_"), spec.name


def test_kiem_ke_truoc_sau():
    counts = catalog.inventory_counts()
    assert counts["total"] == len(catalog.ENTRIES) >= 100
    assert counts["before"] < counts["nl"] and counts["none"] <= 5
    assert counts["nl"] + counts["suggest"] + counts["none"] == counts["total"]


# ─── Câu chat thường KHÔNG bị bắt ───────────────────────────────────────────

CHAT_THUONG = (
    "vâng hôm nay em bận",  # vn-1
    "em nói về số liệu kinh tế đi",  # vn-2: "về số"
    "nước lũ đổ về số đông hộ dân",
    "nhắc anh trưa nay ăn thuốc",  # exp-1: câu nhắc việc, không phải lệnh
    "anh nhớ em quá",
    "hôm nay em làm gì đấy",
    "trưa nay ăn gì ngon nhỉ",
    "anh đang dùng giọng nói để nhắn cho em",
    "dịch bệnh năm nay thế nào",
    "giao dịch hôm nay thế nào em",
    "anh bao nhiêu tuổi rồi nhỉ",
    "tối nay đi xem phim không",
    "cảm ơn em nhé",
    "ok em",
    "đúng rồi",
    "thôi bỏ đi",
    "cach nau pho bo",
    "lam sao de giam can",
    "hom nay troi dep qua",
    "chuyển về 500000 cho anh",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", CHAT_THUONG)
async def test_cau_chat_thuong_khong_bi_bat(text, lines):
    for user, admin in ((OWNER, True), (MEMBER, False)):
        assert await nl.maybe_handle(user, text, channel="zalo", is_admin=admin) is None
        role = nl.role_for(user, admin)
        groups = [g for g in nl.router_groups(user, text, role) if g != "suggest"]
        assert groups == [], (text, groups)
    assert lines == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "anh thích xem tử vi lắm",
        "tử vi là gì vậy em",
        "kỳ môn độn giáp có đúng không",
        "thần số học có khoa học không",
    ],  # fmt: skip
)
async def test_cau_hoi_kien_thuc_khong_chay_lenh_bang_code(text, lines):
    """Có từ khoá nhưng là hỏi kiến thức/kể chuyện: code không chạy lệnh (router có
    quy tắc riêng: hỏi kiến thức -> "none")."""
    assert await nl.maybe_handle(OWNER, text, channel="telegram", is_admin=True) is None
    assert lines == [] and nl.pending(OWNER) is None


def test_tat_bang_env(monkeypatch):
    monkeypatch.setenv("NL_COMMANDS_LLM", "0")
    assert nl.router_groups(OWNER, "luận giải tử vi cho anh", "owner") == []
    monkeypatch.setenv("NL_COMMANDS_ENABLED", "0")
    assert not nl.enabled()


# ─── Trợ giúp bằng câu tự nhiên (không AI) ──────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "em làm được gì?",
        "em lam duoc gi",
        "bot có những tính năng gì",
        "em giúp gì được cho anh",
        "Lan Anh ơi em biết làm gì",
        "có những lệnh nào",
    ],  # fmt: skip
)
async def test_hoi_em_lam_duoc_gi(text):
    reply = await nl.maybe_handle(OWNER, text, channel="telegram", is_admin=True)
    assert reply is not None and "nhắn tự nhiên" in reply.text and "/help all" in reply.text


@pytest.mark.asyncio
async def test_tro_giup_thanh_vien_khong_lo_admin():
    reply = await nl.maybe_handle(MEMBER, "em làm được gì", channel="zalo", is_admin=False)
    assert reply is not None
    for hidden in ("admin", "provider", "nhóm", "quyền"):
        assert hidden not in reply.text, hidden
    admin = await nl.maybe_handle(ADMIN, "em làm được gì", channel="zalo", is_admin=True)
    assert "admin" in admin.text and "quyền" not in admin.text  # quyền Zalo: chỉ chủ bot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("làm sao để dò vé số", "/xoso"),
        ("lam sao de do ve so", "/xoso"),
        ("có lệnh nào xem vàng không", "/vang"),
        ("làm sao để đặt nhắc lặp", "/nhac"),
        ("làm thế nào để xem tỷ giá", "/tygia"),
        ("muốn xoá ghi chú thì làm sao", "/xoaghichu"),
        ("có lệnh nào xem thời tiết không", "/thoitiet"),
    ],
)
async def test_lam_sao_de_tim_trong_danh_ba(text, command):
    reply = await nl.maybe_handle(OWNER, text, channel="telegram", is_admin=True)
    assert reply is not None and command in reply.text


@pytest.mark.asyncio
async def test_lam_sao_khong_lo_lenh_admin_cho_thanh_vien():
    reply = await nl.maybe_handle(MEMBER, "làm sao để khoá zalo", channel="zalo")
    assert reply is None or "/zalokhoa" not in reply.text
    owner = await nl.maybe_handle(OWNER, "làm sao để khoá zalo", channel="telegram")
    assert owner is not None and "/zalokhoa" in owner.text


# ─── Đường code: mẫu câu rõ ràng chạy thẳng ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "xem tử vi 15/3/1990 9h30 nam",
        "tu vi 15/3/1990 9h30 nam",
        "lá số tử vi của em 2/9/1995 7h tối nữ",
        "xem tử vi cho anh sinh ngày 15/3/1990 lúc 9h30 sáng, nam",
    ],  # fmt: skip
)
async def test_tu_vi_bang_code(text):
    reply = await nl.maybe_handle(OWNER, text, channel="telegram", is_admin=True)
    assert reply is not None and reply.text.startswith("🔮 Lá số Tử Vi")


@pytest.mark.asyncio
async def test_tu_vi_thieu_tham_so_hoi_lai_roi_chay_tiep(lines):
    ask = await nl.maybe_handle(MEMBER, "xem tử vi giúp em", channel="zalo")
    assert ask is not None and "ngày sinh, giờ sinh và giới tính" in ask.text
    assert nl.pending(MEMBER).spec == "cmd_tuvi" and lines == []
    # Thiếu giới tính: hỏi đúng chỗ thiếu, vẫn chờ.
    again = await nl.maybe_handle(MEMBER, "15/3/1990 9h30", channel="zalo")
    assert again is not None and "Thiếu giới tính" in again.text
    done = await nl.maybe_handle(MEMBER, "15/3/1990 9h30 nam", channel="zalo")
    assert done is not None and done.text.startswith("KẾT QUẢ /tuvi ")
    assert nl.pending(MEMBER) is None


@pytest.mark.asyncio
async def test_dang_cho_ma_noi_chuyen_khac_thi_thoi_cho(lines):
    await nl.maybe_handle(MEMBER, "xem tử vi", channel="zalo")
    assert nl.pending(MEMBER) is not None
    assert await nl.maybe_handle(MEMBER, "thôi để sau", channel="zalo") is None
    assert nl.pending(MEMBER) is None and lines == []


@pytest.mark.asyncio
async def test_tu_vi_luan_giai_la_lenh_nang_het_luot_thi_bao(lines, monkeypatch):
    from services import usage_quota

    async def no_heavy(user_id, *, channel, is_admin):
        return False

    async def caps():
        return usage_quota.Caps(turns=30, heavy=5)

    monkeypatch.setattr(usage_quota, "heavy_available", no_heavy)
    monkeypatch.setattr(usage_quota, "caps", caps)
    reply = await nl.maybe_handle(
        MEMBER, "luận giải tử vi chi tiết 15/3/1990 9h30 nam", channel="zalo"
    )
    assert reply is not None and lines == []
    plain = await nl.maybe_handle(MEMBER, "xem tử vi 15/3/1990 9h30 nam", channel="zalo")
    assert plain is not None and lines == ["/tuvi xem tử vi 15/3/1990 9h30 nam"]


@pytest.mark.asyncio
async def test_than_so_hoc_ky_mon_lich_bang_code():
    tsh = await nl.maybe_handle(OWNER, "thần số học 15/3/1990 tên Nguyễn Văn An", channel="zoom")
    assert tsh is not None and "Nguyễn Văn An" in tsh.text and "Con số chủ đạo" in tsh.text
    plain = await nl.maybe_handle(OWNER, "than so hoc 15/3/1990", channel="zoom")
    assert plain is not None and "Con số chủ đạo" in plain.text
    # câu thật từ chat (từng bị bắt nhầm thành khoản chi 1.988đ ở bước ghi chi tiêu)
    asked = await nl.maybe_handle(OWNER, "thần số học ngày 18/09/1988", channel="zoom")
    assert asked is not None and "Con số chủ đạo" in asked.text
    kymon = await nl.maybe_handle(OWNER, "lập bàn kỳ môn lúc này", channel="zoom")
    assert kymon is not None and kymon.text.startswith("🧭 Kỳ Môn")
    for text in ("ngày tốt tháng này", "ngay tot thang nay", "xem sao hạn 1990 nam năm 2027"):
        reply = await nl.maybe_handle(OWNER, text, channel="zoom")
        assert reply is not None and "Em chưa hiểu" not in reply.text, text


@pytest.mark.asyncio
async def test_mau_cau_doc_du_lieu_rieng_theo_dung_user(lines):
    reply = await nl.maybe_handle(MEMBER, "em đang nhớ gì về chị", channel="zalo")
    assert reply is not None and lines == ["/memory"]
    await nl.maybe_handle(MEMBER, "xem lịch sử chat gần đây", channel="zalo")
    await nl.maybe_handle(MEMBER, "các khoản chi gần đây", channel="zalo")
    assert lines == ["/memory", "/history", "/chitieu ds"]
    # Lệnh hệ thống: thành viên nói y hệt cũng không chạy.
    assert await nl.maybe_handle(MEMBER, "xem trạng thái provider", channel="zalo") is None
    assert await nl.maybe_handle(MEMBER, "hôm nay bot có lỗi gì không", channel="zalo") is None
    assert lines == ["/memory", "/history", "/chitieu ds"]
    await nl.maybe_handle(OWNER, "xem trạng thái provider", channel="telegram", is_admin=True)
    assert lines[-1] == "/status"


@pytest.mark.asyncio
async def test_xem_nhac_viec_va_tong_chi_tieu_bang_code_khong_qua_router(monkeypatch):
    from services import expense_service, reminder_service

    calls = []

    async def fake_items(user_id):
        calls.append(("nhac", user_id))
        return []

    async def fake_summary(user_id, period=""):
        calls.append(("chi", user_id, period))
        return f"💸 Chi tiêu {period}"

    monkeypatch.setattr(reminder_service, "pending_items", fake_items)
    monkeypatch.setattr(expense_service, "summary_text", fake_summary)
    for text in ("xem nhắc việc", "các việc cần nhắc", "em đặt những nhắc gì rồi"):
        reply = await nl.maybe_handle(MEMBER, text, channel="zalo")
        assert reply is not None and "chưa có nhắc việc nào" in reply.text, text
    for text in ("tháng này anh tiêu bao nhiêu", "chi tiêu tuần này", "tổng chi tháng trước"):
        reply = await nl.maybe_handle(MEMBER, text, channel="zalo")
        assert reply is not None and reply.text.startswith("💸 Chi tiêu"), text
    assert [c[1] for c in calls] == [MEMBER] * 6  # dữ liệu đúng người hỏi
    assert calls[3][2] == "thang nay anh tieu bao nhieu"
    for text in (
        "nhắc anh 8h tối mai gọi mẹ",
        "chi tiêu hợp lý thế nào",
        "tháng này nên tiêu bao nhiêu",
    ):
        assert await nl.maybe_handle(MEMBER, text, channel="zalo") is None, text
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_lenh_nguy_hiem_chi_chi_ra_lenh(monkeypatch):
    from services import memory_commands

    async def boom(*args, **kwargs):
        raise AssertionError("không được chạy lệnh xoá")

    monkeypatch.setattr(memory_commands, "handle", boom)
    for text, command in (
        ("xoá hết trí nhớ đi em", "/forget"),
        ("xoa het du lieu cua anh", "/xoadulieu"),
        ("xoá ngữ cảnh", "/reset"),
    ):
        reply = await nl.maybe_handle(OWNER, text, channel="telegram", is_admin=True)
        assert reply is not None and f"\n{command}\n" in reply.text and "🔐" in reply.text


@pytest.mark.asyncio
async def test_bat_tat_cai_dat_bang_code_nhac_lai_lenh(lines):
    reply = await nl.maybe_handle(MEMBER, "bật trả lời bằng giọng nói", channel="zalo")
    assert reply is not None and lines == ["/noi on"]
    assert reply.text.startswith("↪️ Em làm theo lệnh /noi on")
    await nl.maybe_handle(MEMBER, "bật giờ yên lặng từ 22h đến 7h", channel="zalo")
    await nl.maybe_handle(MEMBER, "tắt hỏi thăm chủ động", channel="zalo")
    assert lines == ["/noi on", "/yenlang 22-7", "/hoithan off"]
    # Lượt chỉ đọc (tin thoại): không đổi cài đặt.
    assert (
        await nl.maybe_handle(
            MEMBER, "tắt trả lời bằng giọng nói", channel="zalo", allow_side_effects=False
        )
        is None
    )
    assert lines == ["/noi on", "/yenlang 22-7", "/hoithan off"]


# ─── Router LLM: tool -> đúng dòng lệnh ─────────────────────────────────────


async def _route(user_id, text, *, is_admin=False, channel="zalo", **kwargs):
    with nl.turn_scope(channel, is_admin):
        return await tools.maybe_run_tool(user_id, text, is_admin=is_admin, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "data", "line"),
    [
        (
            "cho anh xem lá số chi tiết của vợ, sinh 1995",
            {
                "tool": "cmd_tuvi",
                "args": {"date": "1995-09-02", "time": "19h", "gender": "nữ", "detail": True},
            },  # fmt: skip
            "/tuvi_chitiet 2/9/1995 19h nữ",
        ),
        (
            "kỳ môn 9h sáng mai ký hợp đồng có thuận không",
            {"tool": "cmd_kymon", "args": {"when": "9h sáng mai", "question": "ký hợp đồng"}},
            "/kymon_chitiet 9h sáng mai ký hợp đồng",
        ),
        (
            "dịch giúp anh sang tiếng nhật: cảm ơn anh",
            {"tool": "cmd_dich", "args": {"text": "cảm ơn anh", "direction": "vi>ja"}},
            "/dich vi>ja cảm ơn anh",
        ),
        (
            "tim gia iphone 16 pro giup anh",
            {"tool": "cmd_gia", "args": {"product": "iPhone 16 Pro"}},
            "/gia iPhone 16 Pro",
        ),
        (
            "tháng 10 anh chốt lãi lỗ đã chốt bao nhiêu",
            {"tool": "cmd_laicp", "args": {"month": "10"}},
            "/laicp 10",
        ),
        (
            "xem hợp tuổi 1990 và 1992",
            {"tool": "cmd_lich", "args": {"query": "tuổi 1990 và 1992"}},
            "/lich tuổi 1990 và 1992",
        ),
    ],
)
async def test_router_chon_lenh_ra_dung_dong_lenh(monkeypatch, lines, text, data, line):
    fake = _router(monkeypatch, data)
    outcome = await _route(MEMBER, text)
    assert outcome.verbatim and outcome.status == reg.STATUS_OK
    assert lines == [line]
    assert nl.outcome_messages(outcome) == [f"KẾT QUẢ {line}"]
    assert data["tool"] in fake.prompts[0] and nl.ROUTER_RULE in fake.prompts[0]


@pytest.mark.asyncio
async def test_router_thieu_tham_so_hoi_lai_khong_doan(monkeypatch, lines):
    _router(monkeypatch, {"tool": "cmd_tuvi", "args": {"date": "15/3/1990", "time": "9h30"}})
    outcome = await _route(MEMBER, "lập lá số giúp em với, sinh 15/3/1990 9h30")
    assert outcome.status == reg.STATUS_INVALID and lines == []
    assert "giới tính" in nl.outcome_messages(outcome)[0]
    assert nl.pending(MEMBER).spec == "cmd_tuvi"
    # Lượt sau (câu không có từ khoá nào) vẫn được đưa vào router nhờ "đang chờ".
    assert nl.router_groups(MEMBER, "nam", "member") == ["huyen_hoc"]


@pytest.mark.asyncio
async def test_router_khong_chac_thi_hoi_lai(monkeypatch, lines):
    _router(monkeypatch, {"tool": "clarify_command", "args": {"tools": ["cmd_tuvi", "cmd_tsh"]}})
    outcome = await _route(MEMBER, "xem lá số hay thần số học gì đó cho chị")
    assert outcome.status == reg.STATUS_INVALID and lines == []
    assert "tử vi hay thần số học" in nl.outcome_messages(outcome)[0]


@pytest.mark.asyncio
async def test_router_bat_tat_phai_noi_ro(monkeypatch, lines):
    _router(monkeypatch, {"tool": "set_noi", "args": {"mode": "off"}})
    # Router đoán "off" nhưng câu không nói tắt ("dùng" bỏ dấu trùng "dừng").
    vague = await _route(MEMBER, "anh muốn dùng giọng nói của em")
    assert lines == [] and vague.status in (reg.STATUS_NONE, reg.STATUS_INVALID)
    both = await _route(MEMBER, "giọng nói bật hay tắt thì tuỳ em")
    assert lines == [] and both.status == reg.STATUS_INVALID
    assert "BẬT hay TẮT" in nl.outcome_messages(both)[0]
    ok = await _route(MEMBER, "tắt cái giọng nói đi em")
    assert ok.saved and lines == ["/noi off"]
    assert nl.outcome_messages(ok)[0].startswith("↪️ Em làm theo lệnh /noi off")
    blocked = await _route(MEMBER, "tắt cái giọng nói đi em", allow_side_effects=False)
    assert blocked.status == reg.STATUS_BLOCKED and lines == ["/noi off"]
    assert "/noi" in nl.outcome_messages(blocked)[0]


@pytest.mark.asyncio
async def test_router_khong_bat_tat_qua_lenh_tra_cuu(monkeypatch, lines):
    _router(monkeypatch, {"tool": "cmd_anh", "args": {"description": "off"}})
    outcome = await _route(ADMIN, "tắt tạo ảnh đi em", is_admin=True)
    assert outcome.status == reg.STATUS_INVALID and lines == []
    _router(monkeypatch, {"tool": "cmd_tongket", "args": {"group": "on"}})
    outcome = await _route(ADMIN, "bật tổng kết nhóm tự động", is_admin=True)
    assert outcome.status == reg.STATUS_INVALID and lines == []


@pytest.mark.asyncio
async def test_router_thanh_vien_khong_goi_duoc_lenh_admin(monkeypatch, lines):
    fake = _router(monkeypatch, {"tool": "cmd_status", "args": {}})
    outcome = await _route(MEMBER, "provider đang chạy cái nào em")
    assert outcome.status == reg.STATUS_NONE and lines == []
    assert not fake.prompts or "cmd_status" not in fake.prompts[0]
    _router(monkeypatch, {"tool": "suggest_command", "args": {"command": "/zalokhoa", "args": "9"}})
    hidden = await _route(MEMBER, "khoá zalo user 9 giúp chị")
    assert nl.outcome_messages(hidden) == [] and lines == []
    owner = await _route(OWNER, "khoá zalo user 9 giúp anh", is_admin=True, channel="zoom")
    text = nl.outcome_messages(owner)[0]
    assert "/zalokhoa 9" in text and "trên Telegram" in text and lines == []


@pytest.mark.asyncio
async def test_router_quan_tri_chi_chi_ra_lenh(monkeypatch, lines):
    fake = _router(
        monkeypatch, {"tool": "suggest_command", "args": {"command": "fb_ok", "args": "12"}}
    )
    outcome = await _route(ADMIN, "duyệt và đăng facebook bài 12 đi", is_admin=True)
    assert outcome.verbatim and lines == []
    assert "\n/fb_ok 12\n" in nl.outcome_messages(outcome)[0]
    assert "/fb_ok" in fake.prompts[0]


# ─── Lượt chat: kết quả lệnh gửi nguyên văn, không gọi AI ───────────────────


@pytest.mark.asyncio
async def test_run_chat_turn_tra_nguyen_van_khong_goi_ai(monkeypatch, lines):
    from services import channel_chat_service as ccs

    _router(monkeypatch, {"tool": "cmd_lich", "args": {"query": "ngày tốt tháng 11"}})
    saved, calls = [], []

    async def fake_chat(*args, **kwargs):
        calls.append("chat")
        raise AssertionError("không được gọi AI")

    async def start(*args, **kwargs):
        return 7

    async def success(*args, **kwargs):
        return None

    async def record(user_id, text, reply, kind="chat", **kwargs):
        saved.append((user_id, kind))
        return True

    async def plan(*args, **kwargs):
        raise AssertionError("không tra web")

    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.telemetry, "start", start)
    monkeypatch.setattr(ccs.telemetry, "success", success)
    monkeypatch.setattr(ccs.conversation_log, "record_exchange", record)
    monkeypatch.setattr(ccs.web_search, "plan_search", plan)
    turn = await ccs.run_chat_turn(MEMBER, "xem ngày xuất hành tốt tháng 11", channel="zalo")
    assert turn.verbatim and turn.messages == ("KẾT QUẢ /lich ngày tốt tháng 11",)
    assert calls == [] and saved == [(MEMBER, "command")]


# ─── /help theo chủ đề, menu Telegram, trần token ───────────────────────────


def test_help_ngan_chu_de_va_day_du():
    short = nl.help_reply("", "member", "zalo", "chị")
    assert "Chị cứ nhắn tự nhiên" in short and "/help all" in short and "admin" not in short
    gold = nl.help_reply("vàng", "member", "zalo", "chị")
    assert "/vang" in gold and "/xoso" in gold and "/fb_" not in gold
    remind = nl.help_reply("nhắc việc", "owner", "telegram")
    assert "/nhac" in remind and "/ngaynho" in remind
    # Thành viên gõ /help admin: không lộ lệnh admin, nhận bản ngắn.
    assert "/status" not in nl.help_reply("admin", "member", "zalo")
    assert "/status" in nl.help_reply("admin", "admin", "zalo")
    assert "/zalokhoa" in nl.help_reply("quyền", "owner", "telegram")
    assert nl.help_reply("all", "owner", "telegram") is None


@pytest.mark.asyncio
async def test_help_zalo_theo_chu_de():
    from services import channel_command_service as ccs

    member, _ = await ccs.maybe_handle_command(MEMBER, "/help admin", is_admin=False)
    assert "/status" not in member[0]
    admin, _ = await ccs.maybe_handle_command(ADMIN, "/help nhóm", is_admin=True)
    assert "/tongket" in admin[0] and "/fb_ok" in admin[0]
    full, _ = await ccs.maybe_handle_command(ADMIN, "/help all", is_admin=True)
    assert full == [ccs.HELP_ADMIN]


def test_menu_telegram_gon():
    import bot_app

    menu = [c.command for c in bot_app.COMMANDS]
    every = {c.command for c in bot_app.ALL_COMMANDS}
    assert 15 <= len(menu) <= 20 and len(set(menu)) == len(menu)
    assert set(menu) <= every and {"help", "huy", "nhac", "vang"} <= set(menu)
    # Bỏ khỏi menu không có nghĩa là bỏ lệnh: handler vẫn đăng ký đủ.
    assert every <= _telegram_handler_names()


def test_tran_token_prompt_router_theo_nhom():
    """Chỉ nhóm khớp câu mới vào prompt: câu thường giữ nguyên trần cũ, câu có lệnh
    thêm tối đa MAX_GROUPS nhóm."""
    assert len(tools._build_router_prompt("nhắc anh 8h tối mai gọi mẹ")) <= 3800
    for text in (
        "luận giải tử vi cho anh",
        "dịch giúp anh câu này",
        "provider đang chạy cái nào",
        "tóm tắt nhóm Gia đình hôm nay",
        "bật giờ yên lặng 22h-7h",
        "xem sổ giao dịch FPT",
        "xem trí nhớ của em",
        "ngày tốt tháng này để khai trương",
        "tắt giọng nói và dịch giúp anh, xem tử vi luôn",
    ):
        groups = nl.router_groups(OWNER, text, "owner")
        assert 1 <= len(groups) <= nl.MAX_GROUPS, text
        assert len(tools._build_router_prompt(text, role="owner")) <= 5200, text
    for spec in reg.specs(reg.COMMAND):
        assert len(spec.description) <= 80 and len(spec.router_hint) <= 360, spec.name


def test_case_danh_gia_router_lenh_co_tool_trong_prompt(monkeypatch):
    """Eval tier A (test/evals/router.yaml): case chờ 1 tool "chạy lệnh" thì prompt
    router dựng cho câu đó PHẢI liệt kê tool ấy (không thì model không thể chọn)."""
    from evals import harness

    monkeypatch.setattr(config, "ALLOWED_USER_ID", harness.OWNER_ID)

    cases = [
        c
        for c in harness.load_cases()
        if c.kind == "router"
        and str(c.expect["tool"]) in nl.COMMANDS
        or (c.kind == "router" and c.expect["tool"] in ("suggest_command", "clarify_command"))
    ]
    assert len(cases) >= 3
    for case in cases:
        role = tools._role(case.user_id, case.is_admin)
        prompt = tools._build_router_prompt(case.prompt, "", role)
        assert f'"{case.expect["tool"]}"' in prompt, case.id


# ─── Telegram ───────────────────────────────────────────────────────────────


class _TgMessage:
    def __init__(self):
        self.texts: list[tuple[str, dict]] = []
        self.photos: list = []

    async def reply_text(self, text, **kwargs):
        self.texts.append((text, kwargs))

    async def reply_photo(self, photo=None, **kwargs):
        self.photos.append(photo)


def _tg_update(message):
    from types import SimpleNamespace

    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=OWNER, username="u"),
        effective_chat=SimpleNamespace(id=OWNER),
    )


@pytest.mark.asyncio
async def test_telegram_cau_tu_nhien_tra_loi_bang_code(monkeypatch):
    from handlers import chat_router

    sent, recorded = [], []

    async def reply_long(message, text, **kwargs):
        sent.append(text)

    async def record(user_id, user_text, reply, kind="chat", **kwargs):
        recorded.append(kind)
        return True

    monkeypatch.setattr(chat_router.common, "reply_long_text", reply_long)
    monkeypatch.setattr(chat_router.conversation_log, "record_exchange", record)
    message = _TgMessage()
    reply = await chat_router._quick_reply(
        _tg_update(message), OWNER, "em làm được gì?", allow_side_effects=False
    )
    assert reply == "" and recorded == ["command"]
    assert "nhắn tự nhiên" in sent[0] and "🔧 admin" in sent[0] and "🔑 quyền" in sent[0]


@pytest.mark.asyncio
async def test_telegram_gui_ket_qua_lenh_kem_anh(monkeypatch):
    import base64

    from handlers import chat_router

    sent = []

    async def reply_long(message, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(chat_router.common, "reply_long_text", reply_long)
    message = _TgMessage()
    run = nl.CommandRun(("Ảnh nè",), image_b64=base64.b64encode(b"PNG").decode())
    await chat_router.send_command_output(message, run)
    assert sent == ["Ảnh nè"] and message.photos == [b"PNG"]


@pytest.mark.asyncio
async def test_telegram_help_ngan_chu_de_va_all(monkeypatch):
    from types import SimpleNamespace

    from handlers import commands

    sent = []

    async def reply_long(message, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(commands.common, "reply_long_text", reply_long)
    message = _TgMessage()
    await commands.help_cmd(_tg_update(message), SimpleNamespace(args=[]))
    await commands.help_cmd(_tg_update(message), SimpleNamespace(args=["vàng"]))
    assert "/help all" in sent[0] and "/vang" in sent[1]
    await commands.help_cmd(_tg_update(message), SimpleNamespace(args=["all"]))
    assert [t for t, _ in message.texts] == commands.help_chunks()
    assert all(kw.get("parse_mode") == "Markdown" for _, kw in message.texts)


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("xem phiên bản", True),
        ("bot có gì mới không", True),
        ("em mới cập nhật gì thế", True),
        ("phiên bản mới có gì", True),
        ("lịch sử cập nhật", True),
        ("bạn mới có gì", False),  # trùng chữ không dấu "ban moi co gi"
        ("có gì mới không", False),  # có thể là hỏi tin tức
    ],
)
def test_cau_tu_nhien_hoi_phien_ban(text, hit):
    core = nl._core(text)
    matched = [
        c.spec.name for c in nl.COMMANDS.values() if any(p.fullmatch(core) for p in c.patterns)
    ]
    assert (matched == ["cmd_phienban"]) is hit, (core, matched)
