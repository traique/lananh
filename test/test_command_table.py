"""Một bảng lệnh cho ba kênh (services/command_catalog.py): bot_app sinh bảng
CommandHandler Telegram + danh sách lệnh "/", channel_command_service sinh bảng lệnh
Zalo/Zoom từ cùng danh bạ.

Các bảng TELEGRAM, MENU, DESCRIPTIONS, CHAT_ROUTES, CHAT_ADMIN_ONLY dưới đây CHỤP từ code
TRƯỚC khi sinh từ danh bạ (bảng tay 113 handler của bot_app, chuỗi if 25 nhánh của
channel_command_service.maybe_handle_command): đổi danh bạ mà lệch ảnh chụp là đổi hành
vi bot - đổi có chủ đích thì sửa cả ảnh chụp."""

from __future__ import annotations

import re

import pytest

from services import channel_command_service as svc
from services import command_catalog as catalog
from services import divination_commands, memory_commands

# ─── Ảnh chụp trước khi sinh bảng từ danh bạ ────────────────────────────────

# Handler Telegram "<module trong handlers/>.<hàm>" -> các lệnh đăng ký cho handler đó.
# fmt: off
TELEGRAM = {
    "commands.agent_cmd": ("agent",),
    "commands.bangtinsang_cmd": ("bangtinsang", "bantinsang",),
    "commands.battu_chitiet_cmd": ("battu_chitiet",),
    "commands.battu_cmd": ("battu",),
    "commands.cungphi_cmd": ("cungphi",),
    "commands.dich_cmd": ("dich",),
    "commands.help_cmd": ("help",),
    "commands.history_cmd": ("history",),
    "commands.huy_cmd": ("huy",),
    "commands.kymon_chitiet_cmd": ("kymon_chitiet",),
    "commands.kymon_cmd": ("kymon",),
    "commands.lich_cmd": ("lich",),
    "commands.loban_cmd": ("loban",),
    "commands.memory_family_cmd": ("boghim", "forget", "ghim", "hoc", "hoithan", "memory", "nho", "notes", "quen", "xoadulieu", "xoaghichu", "xuatdulieu",),
    "commands.price_cmd": ("gia",),
    "commands.prompt_cmd": ("prompt",),
    "commands.rag_cmd": ("rag",),
    "commands.ragxuly_cmd": ("ragxuly",),
    "commands.reset_chat_cmd": ("reset",),
    "commands.start_cmd": ("start",),
    "commands.thongke_cmd": ("thongke",),
    "commands.tintuc_cmd": ("tintuc",),
    "commands.tsh_chitiet_cmd": ("ths_chitiet", "tsh_chitiet",),
    "commands.tsh_cmd": ("tsh",),
    "commands.tuvi_chitiet_cmd": ("tuvi_chitiet",),
    "commands.tuvi_cmd": ("tuvi",),
    "daily_data_commands.bantin_cmd": ("bantin",),
    "daily_data_commands.chuy_cmd": ("chuy",),
    "daily_data_commands.thoitiet_cmd": ("thoitiet",),
    "daily_data_commands.tygia_cmd": ("tygia",),
    "daily_data_commands.vang_cmd": ("vang",),
    "daily_data_commands.xang_cmd": ("xang",),
    "daily_data_commands.xoso_cmd": ("xoso",),
    "daily_data_commands.yenlang_cmd": ("yenlang",),
    "expense_commands.chitieu_cmd": ("chitieu",),
    "expense_commands.chitieu_xoa_cmd": ("chitieu_xoa",),
    "feedback_commands.danhgia_cmd": ("danhgia",),
    "feedback_commands.sai_cmd": ("sai",),
    "feedback_commands.xuateval_cmd": ("xuateval",),
    "job_commands.lichtacvu_cmd": ("lichtacvu",),
    "job_commands.viec_cmd": ("viec",),
    "pairing_commands.zaloadmin_cmd": ("zaloadmin",),
    "pairing_commands.zalodanhsach_cmd": ("zalodanhsach",),
    "pairing_commands.zalohaquyen_cmd": ("zalohaquyen",),
    "pairing_commands.zalokhoa_cmd": ("zalokhoa",),
    "pairing_commands.zalomokhoa_cmd": ("zalomokhoa",),
    "pairing_commands.zalopair_cmd": ("zalopair",),
    "pairing_commands.zaloxoa_cmd": ("zaloxoa",),
    "pairing_commands.zoompair_cmd": ("zoompair",),
    "pairing_commands.zoomstatus_cmd": ("zoomstatus",),
    "pairing_commands.zoomxoa_cmd": ("zoomxoa",),
    "personal_commands.an_cmd": ("an",),
    "personal_commands.app_cmd": ("app",),
    "personal_commands.noi_cmd": ("noi",),
    "portfolio_commands.portfolio_cmd": ("bancp", "botheodoi", "canhbaogia", "capnhatcp", "danhmuc", "giaban", "laicp", "lichsucp", "themcp", "theodoi", "xoacanhbao", "xoacp",),
    "provider_commands.anh_cmd": ("anh",),
    "provider_commands.loi_cmd": ("loi",),
    "provider_commands.model_cmd": ("model",),
    "provider_commands.phienban_cmd": ("phienban",),
    "provider_commands.router9_toggle_cmd": ("router9",),
    "provider_commands.status_cmd": ("status",),
    "provider_commands.tavily_toggle_cmd": ("tavily",),
    "provider_commands.userouter9_cmd": ("userouter9",),
    "reminder_commands.ngaynho_cmd": ("ngaynho",),
    "reminder_commands.nhac_cmd": ("nhac",),
    "zalo_bridge_commands.dangnoi_cmd": ("dangnoi",),
    "zalo_bridge_commands.fb_boloc_cmd": ("fb_boloc",),
    "zalo_bridge_commands.fb_boqua_cmd": ("fb_boqua",),
    "zalo_bridge_commands.fb_check_cmd": ("fb_check",),
    "zalo_bridge_commands.fb_link_cmd": ("fb_link",),
    "zalo_bridge_commands.fb_loclai_cmd": ("fb_loclai",),
    "zalo_bridge_commands.fb_market_cmd": ("fb_market",),
    "zalo_bridge_commands.fb_nhom_cmd": ("fb_nhom",),
    "zalo_bridge_commands.fb_ok_cmd": ("fb_ok",),
    "zalo_bridge_commands.fb_pages_cmd": ("fb_pages",),
    "zalo_bridge_commands.fb_reconcile_cmd": ("fb_reconcile",),
    "zalo_bridge_commands.fb_reset_cmd": ("fb_reset",),
    "zalo_bridge_commands.fb_sua_cmd": ("fb_sua",),
    "zalo_bridge_commands.fb_themnhom_cmd": ("fb_themnhom",),
    "zalo_bridge_commands.fb_xem_cmd": ("fb_xem",),
    "zalo_bridge_commands.fb_xoanhom_cmd": ("fb_xoanhom",),
    "zalo_bridge_commands.nhom_cmd": ("nhom",),
    "zalo_bridge_commands.nhomzalo_cmd": ("nhomzalo",),
    "zalo_bridge_commands.themnhom_cmd": ("themnhom",),
    "zalo_bridge_commands.tinhieu_cmd": ("tinhieu",),
    "zalo_bridge_commands.tongket_cmd": ("tongket",),
    "zalo_bridge_commands.xoanhom_cmd": ("xoanhom",),
    "zalo_login.zalo_cmd": ("zalo",),
    "zalo_login.zalologout_cmd": ("zalologout",),
}
# Menu "/" của Telegram (setMyCommands) - đúng thứ tự.
MENU = [
    ("help", "Hướng dẫn ngắn; /help all xem đủ lệnh"),
    ("huy", "Dừng câu trả lời đang chạy dở"),
    ("chuy", "Có gì cần anh chú ý hôm nay"),
    ("nhac", "Nhắc việc: xem, đặt, huỷ, hoãn"),
    ("chitieu", "Chi tiêu: tổng theo danh mục, ghi khoản"),
    ("danhmuc", "Xem danh mục cổ phiếu"),
    ("theodoi", "Danh sách theo dõi và ngưỡng giá"),
    ("vang", "Giá vàng SJC, DOJI, PNJ, thế giới"),
    ("xoso", "Kết quả xổ số theo miền/tỉnh, dò vé"),
    ("thoitiet", "Thời tiết; /thoitiet nha <tỉnh> đặt nơi ở"),
    ("lich", "Tra lịch âm, ngày lễ, ngày tốt, tuổi, sao hạn"),
    ("tuvi", "An lá số tử vi"),
    ("dich", "Dịch chat Nhật-Việt"),
    ("notes", "Xem/tìm ghi chú"),
    ("memory", "Xem trí nhớ dài hạn (đánh số), on|off"),
    ("viec", "Giao việc chạy nền; xem, huỷ việc"),
    ("sai", "Báo câu trả lời vừa rồi chưa ổn (kèm lý do)"),
    ("status", "Xem provider"),
]
# Mô tả lệnh trong danh sách lệnh "/" của Telegram (ALL_COMMANDS).
DESCRIPTIONS = {
    "agent": "Hỏi agent tự tra cứu nhiều bước (thử nghiệm)",
    "an": "Ghi bữa ăn, xem kcal ước lượng hôm nay/tuần",
    "anh": "Tạo ảnh thật từ mô tả (Agnes AI)",
    "bancp": "Bán bớt hoặc bán hết",
    "bangtinsang": "Gửi ngay Bản tin sáng tới Zalo + Zoom",
    "bantin": "Bản tin sáng cá nhân (on|off|giờ|xem)",
    "boghim": "Bỏ ghim 1 mục trí nhớ",
    "canhbaogia": "Báo khi giá chạm ngưỡng anh đặt",
    "capnhatcp": "Cập nhật vị thế",
    "chitieu": "Chi tiêu: tổng theo danh mục, ghi khoản",
    "chitieu_xoa": "Xoá 1 khoản chi theo số #",
    "chuy": "Có gì cần anh chú ý hôm nay",
    "dangnoi": "Xem nguyên văn thảo luận hôm nay",
    "danhgia": "Đánh giá 👍/👎 câu trả lời theo provider",
    "danhmuc": "Xem danh mục cổ phiếu",
    "dich": "Dịch chat Nhật-Việt",
    "fb_boloc": "Thống kê bộ lọc Zalo → Facebook",
    "fb_boqua": "Bỏ bài Facebook chờ",
    "fb_check": "Kiểm tra bài Facebook đã công khai",
    "fb_link": "Nhập link Shopee affiliate cho bài chờ",
    "fb_loclai": "Lọc lại các bài đang chờ duyệt",
    "fb_market": "Xem thử/đăng bài Page chứng khoán",
    "fb_nhom": "Xem nhóm nguồn Facebook",
    "fb_ok": "Duyệt và đăng Facebook",
    "fb_pages": "Xem các Facebook Page đã cấu hình",
    "fb_reconcile": "Đối soát lượt đăng Facebook bị gián đoạn",
    "fb_reset": "Xóa bài Facebook đã lưu (cần gõ kèm xacnhan)",
    "fb_sua": "Sửa bài Facebook chờ duyệt",
    "fb_themnhom": "Thêm nhóm nguồn Facebook",
    "fb_xem": "Xem bài Facebook chờ duyệt",
    "fb_xoanhom": "Bỏ nhóm nguồn Facebook",
    "forget": "Xoá hết trí nhớ dài hạn",
    "ghim": "Ghim 1 mục trí nhớ theo số",
    "gia": "Tìm giá sản phẩm",
    "help": "Hướng dẫn ngắn; /help all xem đủ lệnh",
    "history": "Xem lịch sử",
    "hoc": "Duyệt cách trả lời em tự rút ra (ok|bo)",
    "hoithan": "Bật/tắt em chủ động hỏi thăm việc anh kể (on|off)",
    "huy": "Dừng câu trả lời đang chạy dở",
    "kymon": "Lập bàn Kỳ Môn Độn Giáp (mặc định lúc này)",
    "kymon_chitiet": "Bàn Kỳ Môn + AI luận giải việc cần xem",
    "laicp": "Lãi/lỗ đã chốt theo tháng",
    "lich": "Tra lịch âm, ngày lễ, ngày tốt, tuổi, sao hạn",
    "lichsucp": "Sổ giao dịch cổ phiếu",
    "lichtacvu": "Tác vụ định kỳ: em tự chạy yêu cầu theo lịch",
    "loi": "Xem lỗi gom nhóm hôm nay",
    "memory": "Xem trí nhớ dài hạn (đánh số), on|off",
    "model": "Xem/đổi model",
    "ngaynho": "Ngày nhớ: giỗ, sinh nhật, kỷ niệm",
    "nhac": "Nhắc việc: xem, đặt, huỷ, hoãn",
    "nho": "Nhờ em nhớ (ghim) 1 điều",
    "nhom": "Xem danh sách nhóm Zalo",
    "noi": "Trả lời kèm giọng nói (on|off|auto · giong <tên>)",
    "notes": "Xem/tìm ghi chú",
    "phienban": "Phiên bản và lịch sử cập nhật",
    "prompt": "Viết prompt tạo ảnh",
    "quen": "Quên 1 mục trí nhớ (số hoặc từ khoá)",
    "rag": "Tra cứu kiến thức trong thư mục rag/",
    "ragxuly": "Dọn file md OCR trong rag/ (AI thêm heading)",
    "reset": "Xoá ngữ cảnh chat",
    "router9": "Bật/tắt 9Router (on|off)",
    "sai": "Báo câu trả lời vừa rồi chưa ổn (kèm lý do)",
    "start": "Bắt đầu",
    "status": "Xem provider",
    "tavily": "Bật/tắt tra web Tavily (on|off)",
    "themcp": "Thêm giao dịch mua",
    "themnhom": "Thêm nhóm Zalo theo dõi",
    "theodoi": "Danh sách theo dõi và ngưỡng giá",
    "thoitiet": "Thời tiết; /thoitiet nha <tỉnh> đặt nơi ở",
    "thongke": "Thống kê lượt gọi theo user/model",
    "tintuc": "Đọc Điểm tin mới ngay trong Telegram",
    "tongket": "Tổng kết nhóm Zalo (AI)",
    "tsh": "Thần số học theo ngày sinh",
    "tsh_chitiet": "Thần số học + AI diễn giải",
    "tuvi": "An lá số tử vi",
    "tuvi_chitiet": "Lá số tử vi + AI luận giải",
    "tygia": "Tỷ giá Vietcombank, đổi tiền",
    "vang": "Giá vàng SJC, DOJI, PNJ, thế giới",
    "viec": "Giao việc chạy nền; xem, huỷ việc",
    "xang": "Giá xăng dầu Petrolimex/PVOIL",
    "xoacp": "Xóa mã khỏi danh mục",
    "xoadulieu": "Xoá trí nhớ, ghi chú, lịch sử chat",
    "xoaghichu": "Xoá 1 ghi chú theo số",
    "xoanhom": "Ngừng theo dõi 1 nhóm Zalo",
    "xoso": "Kết quả xổ số theo miền/tỉnh, dò vé",
    "xuatdulieu": "Tải file dữ liệu em đang lưu",
    "xuateval": "Xuất câu bị 👎 thành case đánh giá (YAML)",
    "yenlang": "Giờ yên lặng, gom thông báo (on|off|22-7)",
    "zalo": "Đăng nhập hoặc xem trạng thái Zalo B",
    "zaloadmin": "Cấp quyền admin Zalo",
    "zalodanhsach": "Xem danh sách user Zalo",
    "zalohaquyen": "Hạ quyền admin Zalo về thành viên",
    "zalokhoa": "Khóa 1 user Zalo",
    "zalologout": "Đăng xuất Zalo B",
    "zalomokhoa": "Mở khóa 1 user Zalo",
    "zalopair": "Cấp quyền thành viên Zalo",
    "zaloxoa": "Xoá pairing 1 user Zalo",
    "zoompair": "Cấp quyền 1 jid Zoom",
    "zoomstatus": "Xem jid Zoom đã pair",
    "zoomxoa": "Gỡ pairing Zoom",
}
# Zalo/Zoom: lệnh -> nhánh xử lý mà chuỗi if cũ gọi. Lệnh không có ở đây nhận "Lệnh chưa
# được hỗ trợ" (danh mục, nhóm Zalo, Facebook do nhánh trước nhận; lệnh chỉ có trên
# Telegram; lệnh lạ).
CHAT_ROUTES = {
    "agent": "_agent",
    "an": "nutrition",
    "anh": "_generate_image",
    "anuong": "nutrition",
    "bangtinsang": "_morning_news_now",
    "bantin": "daily",
    "bantinsang": "_morning_news_now",
    "battu": "divination",
    "battu_chitiet": "divination",
    "boghim": "memory",
    "chitieu": "expense",
    "chitieu_xoa": "expense",
    "chitieuxoa": "expense",
    "chuy": "chuy",
    "cungphi": "divination",
    "danhgia": "feedback",
    "dich": "_translate",
    "forget": "memory",
    "ghim": "memory",
    "gia": "_price",
    "help": "help",
    "history": "history",
    "hoc": "memory",
    "hoithan": "memory",
    "huy": "huy",
    "kymon": "divination",
    "kymon_chitiet": "divination",
    "lich": "divination",
    "lichtacvu": "lichtacvu",
    "loban": "divination",
    "loi": "loi",
    "memory": "memory",
    "model": "model",
    "ngay_nho": "reminder",
    "ngaynho": "reminder",
    "nhac": "reminder",
    "nhacviec": "reminder",
    "nho": "memory",
    "noi": "noi",
    "notes": "memory",
    "phienban": "phienban",
    "prompt": "_prompt",
    "quen": "memory",
    "rag": "_rag",
    "ragxuly": "_ragxuly",
    "reset": "reset",
    "router9": "router9",
    "sai": "feedback",
    "start": "help",
    "status": "status",
    "tavily": "tavily",
    "thoitiet": "daily",
    "thongke": "thongke",
    "ths_chitiet": "divination",
    "tintuc": "_news_now",
    "tsh": "divination",
    "tsh_chitiet": "divination",
    "tuvi": "divination",
    "tuvi_chitiet": "divination",
    "tygia": "daily",
    "userouter9": "userouter9",
    "vang": "daily",
    "viec": "viec",
    "xang": "daily",
    "xoadulieu": "memory",
    "xoaghichu": "memory",
    "xoso": "daily",
    "xuatdulieu": "memory",
    "yenlang": "yenlang",
}
# fmt: on
# Thành viên thường (không phải admin) nhận "Lệnh này chỉ dành cho admin.".
CHAT_ADMIN_ONLY = frozenset(
    {
        "agent",
        "bangtinsang",
        "bantinsang",
        "model",
        "nhomzalo",
        "phienban",
        "rag",
        "ragxuly",
        "router9",
        "status",
        "tavily",
        "thongke",
        "tintuc",
        "userouter9",
    }
)

OWNER, MEMBER = 1, -5
UNSUPPORTED = "Lệnh chưa được hỗ trợ. Gõ /help để xem danh sách."


# ─── Telegram ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def telegram_app():
    from telegram import User

    import bot_app
    from core import config

    old = config.TELEGRAM_TOKEN
    config.TELEGRAM_TOKEN = "123:ABC"
    try:
        app = bot_app.build_application()
    finally:
        config.TELEGRAM_TOKEN = old
    app.bot._bot_user = User(id=1, is_bot=True, first_name="Lan Anh", username="lananh_bot")
    return app


def test_bang_handler_telegram_khong_doi(telegram_app):
    from telegram.ext import CommandHandler

    registered: dict[str, list[str]] = {}
    for handler in telegram_app.handlers[0]:
        if isinstance(handler, CommandHandler):
            callback = handler.callback
            ref = f"{callback.__module__.removeprefix('handlers.')}.{callback.__name__}"
            registered.setdefault(ref, []).extend(sorted(handler.commands))
    assert {ref: tuple(sorted(names)) for ref, names in registered.items()} == {
        ref: tuple(sorted(names)) for ref, names in TELEGRAM.items()
    }
    names = [name for group in registered.values() for name in group]
    assert len(names) == len(set(names)) == 113


def test_menu_va_mo_ta_lenh_telegram_khong_doi():
    import bot_app

    assert [(c.command, c.description) for c in bot_app.COMMANDS] == MENU
    described = [(c.command, c.description) for c in bot_app.ALL_COMMANDS]
    assert dict(described) == DESCRIPTIONS and len(described) == len(DESCRIPTIONS) <= 100


def test_moi_lenh_trong_danh_ba_co_handler_telegram_hop_le():
    import bot_app

    names = [name for name, _ in catalog.telegram_commands()]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z0-9_]{1,32}", name) for name in names)
    for entry in catalog.ENTRIES:
        module, _, func = entry.tg.partition(".")
        assert callable(getattr(bot_app._HANDLER_MODULES[module], func, None)), entry.name
    assert set(catalog.TELEGRAM_MENU) <= {e.name for e in catalog.ENTRIES if e.menu}


# ─── Zalo/Zoom ───────────────────────────────────────────────────────────────


def test_danh_ba_zalo_zoom_tro_toi_bo_xu_ly_co_that():
    used = {e.chat for e in catalog.ENTRIES if e.chat}
    assert used <= svc.handler_keys() | catalog.CHAT_UPSTREAM
    assert svc.handler_keys() <= used  # không có bộ xử lý thừa
    for entry in catalog.ENTRIES:
        # Lệnh chỉ Telegram không có nhánh Zalo/Zoom; lệnh mọi kênh phải có.
        assert (entry.where == "tg") == (not entry.chat), entry.name
        assert not entry.chat_admin or entry.roles != "all", entry.name
    names = [name for e in catalog.ENTRIES for name in (e.name, *e.aliases, *e.chat_aliases)]
    assert len(names) == len(set(names))


# Lá của chuỗi lệnh cũ: bản giả chỉ nhận đúng các lệnh mà hàm thật nhận.
_OWNS = {
    "feedback": {"/sai", "/danhgia"},
    "viec": {"/viec"},
    "lichtacvu": {"/lichtacvu"},
    "divination": {"/lich", "/tsh", "/tuvi", "/battu", "/kymon", "/loban", "/cungphi"}
    | set(divination_commands.AI_COMMAND_KINDS),
    "daily": {"/vang", "/tygia", "/xang", "/xoso", "/thoitiet", "/bantin"},
    "reminder": {"/nhac", "/nhacviec", "/ngaynho", "/ngay_nho"},
    "expense": {"/chitieu", "/chitieu_xoa", "/chitieuxoa"},
    "nutrition": {"/an", "/anuong"},
    "memory": set(memory_commands.COMMANDS),
}


@pytest.fixture
def leaves(monkeypatch):
    """Thay mọi hàm lá của bảng lệnh Zalo/Zoom bằng bản giả trả tên nhánh."""

    def owner(key):
        async def fake(command, *args, **kwargs):
            return [key] if command in _OWNS[key] else None

        return fake

    def tagged(key, *, pair=True):
        async def fake(*args, **kwargs):
            return ([key], None) if pair else [key]

        return fake

    def returning(value):
        async def fake(*args, **kwargs):
            return value

        return fake

    monkeypatch.setattr(svc, "help_text", lambda user_id, arg, is_admin, channel: f"help[{arg}]")
    monkeypatch.setattr(svc.answer_feedback, "handle_command", owner("feedback"))
    monkeypatch.setattr(svc, "attention_text", returning("chuy"))
    monkeypatch.setattr(svc.notify_hub, "yenlang", returning("yenlang"))
    monkeypatch.setattr(svc.background_jobs, "handle_command", owner("viec"))
    monkeypatch.setattr(svc.routines, "handle_command", owner("lichtacvu"))
    monkeypatch.setattr(svc.turn_control, "nothing_to_cancel_for", returning("huy"))
    monkeypatch.setattr(svc.divination_commands, "handle", owner("divination"))
    monkeypatch.setattr(svc.daily_commands, "handle", owner("daily"))
    monkeypatch.setattr(svc.reminder_commands, "handle", owner("reminder"))
    monkeypatch.setattr(svc.expense_service, "handle_command", owner("expense"))
    monkeypatch.setattr(svc.nutrition_service, "handle_command", owner("nutrition"))
    monkeypatch.setattr(svc.voice_reply, "noi_command", returning("noi"))
    for name in (
        "_prompt",
        "_price",
        "_translate",
        "_generate_image",
        "_agent",
        "_rag",
        "_ragxuly",
    ):
        monkeypatch.setattr(svc, name, tagged(name))
    monkeypatch.setattr(svc, "_morning_news_now", tagged("_morning_news_now", pair=False))
    monkeypatch.setattr(svc, "_news_now", tagged("_news_now", pair=False))
    for target, name in (
        (svc.memory_service, "flush_before_reset"),
        (svc.orchestrator, "reset_chat"),
        (svc.db, "clear_chat"),
    ):
        monkeypatch.setattr(target, name, returning(None))

    async def memory(command, argument, user_id, *, channel, about=""):
        if command in _OWNS["memory"]:
            return memory_commands.CommandReply(["memory"])
        return None

    monkeypatch.setattr(svc.memory_commands, "handle", memory)
    monkeypatch.setattr(svc.db, "get_history", returning([]))
    monkeypatch.setattr(svc.release_notes, "history_text", lambda argument: "phienban")
    monkeypatch.setattr(
        svc.orchestrator,
        "get_provider_state_snapshot",
        lambda: {"active_provider": "x", "router9_enabled": True},
    )
    monkeypatch.setattr(svc.provider_health, "latency_lines", returning([]))
    monkeypatch.setattr(svc.provider_health, "live_lines", returning([]))
    monkeypatch.setattr(svc.search_status, "search_mode_line", returning("mode"))
    monkeypatch.setattr(svc.version, "label", lambda: "v")

    async def is_owner(user_id):
        return user_id == OWNER

    monkeypatch.setattr(svc.reply_audience, "is_owner", is_owner)
    from services import error_digest

    monkeypatch.setattr(error_digest, "build_report", returning("loi"))
    monkeypatch.setattr(svc.usage_stats, "parse_thongke_hours", lambda argument: 1)
    monkeypatch.setattr(svc.usage_stats, "build_thongke_text", returning("thongke"))
    monkeypatch.setattr(svc.orchestrator, "try_router9_now", returning((True, "")))
    monkeypatch.setattr(svc.tavily_client, "get_enabled", returning(True))
    monkeypatch.setattr(svc.router9_client, "get_preferred_model_name", returning("m"))
    monkeypatch.setattr(svc.router9_client, "find_model", returning(None))


def _branch(result) -> str:
    """Tên nhánh đã trả lời (theo câu trả lời của bản giả/câu cố định của nhánh)."""
    messages, _ = result
    first = messages[0] if messages else ""
    if first.startswith("help["):
        return "help" if first == "help[]" else "help:arg"
    fixed = {
        "🔄 Đã xoá ngữ cảnh hội thoại.": "reset",
        "Chưa có lịch sử nào.": "history",
        "Lệnh này chỉ dành cho admin.": "admin_only",
        "Lệnh này chỉ dành cho chủ bot.": "owner_only",
        UNSUPPORTED: "unsupported",
        "✅ 9Router hoạt động, đã chuyển về 9Router.": "userouter9",
    }
    if first in fixed:
        return fixed[first]
    for prefix, name in (
        ("🏷️ Phiên bản", "status"),
        ("9Router đang", "router9"),
        ("Tavily đang", "tavily"),
        ("🧠 Model hiện tại", "model"),
        ("Không tìm thấy model", "model"),
    ):
        if first.startswith(prefix):
            return name
    return first


def _expected(name: str, is_admin: bool, user_id: int, with_argument: bool) -> str:
    if name in CHAT_ADMIN_ONLY and not is_admin:
        return "admin_only"
    branch = CHAT_ROUTES.get(name, "unsupported")
    if branch == "help":
        return "help:arg" if name == "help" and with_argument else "help"
    if branch == "loi":
        return "loi" if user_id == OWNER else "owner_only"
    return branch


@pytest.mark.asyncio
async def test_bang_lenh_zalo_zoom_khong_doi(leaves):
    names = {"khonglenh"}
    for entry in catalog.ENTRIES:
        names.update((entry.name, *entry.aliases, *entry.chat_aliases))
    assert set(CHAT_ROUTES) <= names and CHAT_ADMIN_ONLY <= names
    mismatched = {}
    for name in sorted(names):
        for is_admin in (True, False):
            for user_id in (OWNER, MEMBER):
                for text in (f"/{name}", f"/{name.upper()}@lananh_bot tham so"):
                    result = await svc.maybe_handle_command(user_id, text, is_admin, "zalo")
                    want = _expected(name, is_admin, user_id, " " in text)
                    if _branch(result) != want:
                        mismatched[(text, is_admin, user_id)] = (_branch(result), want)
    assert mismatched == {}


@pytest.mark.asyncio
async def test_khong_phai_lenh_va_lenh_hai_gach_cheo():
    assert await svc.maybe_handle_command(OWNER, "xin chào") is None
    # "//help" không phải /help (chuỗi if cũ cũng không nhận).
    assert await svc.maybe_handle_command(OWNER, "//help") == ([UNSUPPORTED], None)
