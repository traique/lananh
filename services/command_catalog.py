"""Danh bạ MỌI lệnh / của bot (Telegram, Zalo, Zoom) ở MỘT chỗ - nguồn DUY NHẤT.

Dùng cho:
- đăng ký lệnh: bot_app.py sinh bảng CommandHandler Telegram + danh sách lệnh BotFather
  (``tg``, ``menu``, ``TELEGRAM_MENU``), services/channel_command_service.py sinh bảng
  lệnh Zalo/Zoom (``chat``, ``chat_admin``, ``chat_aliases``);
- trợ giúp ngắn ``/help``, ``/help <chủ đề>`` (``/help all`` vẫn là bản đầy đủ cũ),
- câu hỏi tự nhiên "em làm được gì", "làm sao để dò vé số", "có lệnh nào xem vàng
  không" (services/nl_commands/ trả lời bằng code, không gọi AI),
- bảng kiểm kê trong docs/nang-cap-jarvis.md (mục "Hỏi tự nhiên").

Mỗi ``Entry``: tên lệnh (không có "/"), ``group`` (tra cứu / cá nhân / ghi dữ liệu /
quản trị / nguy hiểm), ai dùng được (``roles``: all | staff = chủ bot + admin Zalo |
owner), kênh (``where``: both | tg = chỉ Telegram), chủ đề trợ giúp (``topic``),
cú pháp ngắn, mô tả, 1 câu nói tự nhiên mẫu (``ask``) và ``nl`` - đường câu tự
nhiên hiện có:

- ``code:<module>``: đường nhanh bằng code (không AI) đã có từ trước;
- ``tool:<tên>``: tool router chat (services/tools.py, services/history_search.py);
- ``cmd:<tên>``: ToolSpec "chạy lệnh" của services/nl_commands/ (kết quả nguyên văn);
- ``set:<tên>``: ToolSpec bật/tắt cài đặt (câu phải nói rõ bật/tắt; bot nhắc lại lệnh);
- ``suggest``: KHÔNG tự chạy từ câu tự nhiên - bot chỉ ra đúng lệnh cần gõ;
- ``-``: không cần (lệnh điều khiển, nút bấm).

Nhiều đường thì nối bằng " + ". Thêm lệnh mới: thêm 1 dòng ở đây, kèm handler từng
kênh (test_command_table kiểm mọi lệnh có đủ handler và khoá bảng đăng ký).
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field

from core.text_normalize import fold

# Nhóm lệnh (bảng kiểm kê).
LOOKUP = "tra_cuu"
PERSONAL = "ca_nhan"
WRITE = "ghi"
ADMIN = "quan_tri"
DANGER = "nguy_hiem"
GROUP_LABELS = {
    LOOKUP: "tra cứu",
    PERSONAL: "cá nhân (xem)",
    WRITE: "ghi dữ liệu",
    ADMIN: "quản trị",
    DANGER: "nguy hiểm",
}

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
_ROLES = {
    "all": frozenset({ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER}),
    "staff": frozenset({ROLE_OWNER, ROLE_ADMIN}),
    "owner": frozenset({ROLE_OWNER}),
}

# Lệnh Zalo/Zoom xử lý TRƯỚC bảng lệnh của channel_command_service (tới bảng lệnh tức
# là nhánh trước không nhận - trả "Lệnh chưa được hỗ trợ" như cũ):
CHAT_PORTFOLIO = "portfolio"  # danh mục: cổng lệnh / của services/quick_reply.py
CHAT_GROUP = "zalo_group"  # nhóm Zalo: channels/group_commands (admin, có tài khoản Zalo)
CHAT_FACEBOOK = "facebook"  # Zalo -> Facebook: channels/facebook_commands (admin)
CHAT_UPSTREAM = frozenset({CHAT_PORTFOLIO, CHAT_GROUP, CHAT_FACEBOOK})


@dataclass(frozen=True)
class Topic:
    key: str
    icon: str
    title: str
    roles: str = "all"
    # Từ (đã bỏ dấu) gõ sau /help để mở chủ đề này: "/help vàng", "/help nhac viec".
    aliases: tuple[str, ...] = ()
    # Chữ hiển thị trong danh sách chủ đề của /help ("/help đời sống").
    word: str = ""


TOPICS: tuple[Topic, ...] = (
    Topic(
        "doi_song",
        "🌤️",
        "Đời sống",
        word="đời sống",
        aliases=(
            "doi song",
            "vang",
            "gia vang",
            "ty gia",
            "xang",
            "xo so",
            "ve so",
            "do ve",
            "thoi tiet",
            "ban tin",
            "chu y",
        ),
    ),  # fmt: skip
    Topic(
        "huyen_hoc",
        "🔮",
        "Lịch, lễ hội, tử vi, bát tự, thần số học, kỳ môn, phong thủy",
        word="tử vi",
        aliases=(
            "huyen hoc",
            "lich",
            "am lich",
            "tu vi",
            "than so hoc",
            "tsh",
            "ky mon",
            "ngay tot",
            "tuoi",
            "le hoi",
            "bat tu",
            "phong thuy",
            "lo ban",
            "cung phi",
        ),
    ),  # fmt: skip
    Topic(
        "nhac_viec",
        "⏰",
        "Nhắc việc, ngày nhớ, việc nền",
        word="nhắc việc",
        aliases=(
            "nhac viec",
            "nhac",
            "hen gio",
            "ngay nho",
            "gio",
            "sinh nhat",
            "viec nen",
            "tac vu",
            "lich tac vu",
        ),
    ),  # fmt: skip
    Topic(
        "chi_tieu",
        "💰",
        "Chi tiêu, ăn uống",
        word="chi tiêu",
        aliases=("chi tieu", "tien", "an uong", "an", "kcal", "calo"),
    ),  # fmt: skip
    Topic(
        "co_phieu",
        "📈",
        "Cổ phiếu, danh mục",
        word="cổ phiếu",
        aliases=("co phieu", "chung khoan", "danh muc", "ma", "canh bao gia", "theo doi"),
    ),  # fmt: skip
    Topic(
        "tri_nho",
        "🧠",
        "Trí nhớ, ghi chú, dữ liệu",
        word="trí nhớ",
        aliases=("tri nho", "ghi chu", "note", "lich su", "du lieu", "nho"),
    ),  # fmt: skip
    Topic(
        "tien_ich",
        "🛠️",
        "Tiện ích (dịch, giá, ảnh, giọng nói)",
        word="tiện ích",
        aliases=(
            "tien ich",
            "dich",
            "tim gia",
            "anh",
            "tao anh",
            "prompt",
            "giong noi",
            "tai lieu",
            "file",
        ),
    ),  # fmt: skip
    Topic(
        "cai_dat",
        "⚙️",
        "Cài đặt",
        word="cài đặt",
        aliases=("cai dat", "yen lang", "noi", "hoi than"),
    ),
    Topic(
        "admin",
        "🔧",
        "Quản trị bot",
        word="admin",
        roles="staff",
        aliases=("admin", "quan tri", "provider", "model", "thong ke", "loi", "rag", "tin tuc"),
    ),  # fmt: skip
    Topic(
        "nhom",
        "👥",
        "Nhóm Zalo, Facebook Page",
        word="nhóm",
        roles="staff",
        aliases=("nhom", "nhom zalo", "facebook", "fb", "tong ket"),
    ),  # fmt: skip
    Topic(
        "quyen",
        "🔑",
        "Quyền Zalo/Zoom",
        word="quyền",
        roles="owner",
        aliases=("quyen", "zalo", "zoom", "pair", "cap quyen"),
    ),  # fmt: skip
)
_TOPIC_BY_KEY = {t.key: t for t in TOPICS}


@dataclass(frozen=True)
class Entry:
    name: str
    group: str
    roles: str
    where: str
    topic: str
    usage: str
    summary: str
    ask: str = ""
    nl: str = "-"
    # Cụm từ (bỏ dấu) cho câu "làm sao để ..."/"có lệnh nào ..." tìm ra lệnh này.
    keywords: tuple[str, ...] = field(default_factory=tuple)
    # Tên phụ dùng được trên MỌI kênh (Telegram đăng ký thêm handler, Zalo/Zoom nhận).
    aliases: tuple[str, ...] = ()
    # ── Đăng ký theo kênh (bot_app.py, channel_command_service sinh bảng lệnh từ đây) ──
    # Handler Telegram "<module trong handlers/>.<hàm>" - bot_app.py nạp theo tên
    # (services/ không import handlers/).
    tg: str = ""
    # Mô tả trong danh sách lệnh "/" của Telegram (setMyCommands, trần 100); "" = không có.
    menu: str = ""
    # Zalo/Zoom: tên bộ xử lý trong channel_command_service, hoặc CHAT_UPSTREAM (nhánh xử
    # lý trước đó); "" = lệnh chỉ có trên Telegram.
    chat: str = ""
    # Zalo/Zoom: thành viên thường nhận "Lệnh này chỉ dành cho admin." (đổi cấu hình chung
    # của bot, kho kiến thức riêng của chủ bot...).
    chat_admin: bool = False
    # Tên phụ CHỈ Zalo/Zoom nhận (cách gõ cũ; Telegram không đăng ký).
    chat_aliases: tuple[str, ...] = ()

    @property
    def allowed_roles(self) -> frozenset[str]:
        return _ROLES[self.roles]

    @property
    def suggest_only(self) -> bool:
        return "suggest" in self.nl

    @property
    def has_nl(self) -> bool:
        return self.nl not in ("-", "suggest")


def _e(name, group, roles, where, topic, usage, summary, ask="", nl="-", kw=(), aliases=(), **kw2):
    # kw2: khai báo theo kênh (tg, menu, chat, chat_admin, chat_aliases).
    return Entry(
        name, group, roles, where, topic, usage, summary, ask, nl, tuple(kw), aliases, **kw2
    )


A, S, O = "all", "staff", "owner"
B, TG = "both", "tg"
# fmt: off
ENTRIES: tuple[Entry, ...] = (
    # ── Chung ──
    _e("start", LOOKUP, A, B, "chung", "/start", "Lời chào", nl="code:nl_commands",
       tg="commands.start_cmd", chat="help", menu="Bắt đầu"),
    _e("help", LOOKUP, A, B, "chung", "/help [chủ đề|all]", "Hướng dẫn ngắn, theo chủ đề, đầy đủ",
       'em làm được gì?', "code:nl_commands", ("huong dan", "tro giup", "lenh"),
       tg="commands.help_cmd", chat="help", menu="Hướng dẫn ngắn; /help all xem đủ lệnh"),
    _e("huy", PERSONAL, A, B, "chung", "/huy", "Dừng câu trả lời đang chạy dở", nl="-",
       tg="commands.huy_cmd", chat="huy", menu="Dừng câu trả lời đang chạy dở"),
    _e("sai", WRITE, A, B, "chung", "/sai [lý do]", "Báo câu trả lời vừa rồi chưa ổn",
       nl="-", kw=("bao sai", "tra loi sai"),
       tg="feedback_commands.sai_cmd", chat="feedback",
       menu="Báo câu trả lời vừa rồi chưa ổn (kèm lý do)"),
    # ── Đời sống ──
    _e("vang", LOOKUP, A, B, "doi_song", "/vang [sjc|nhẫn|doji|pnj|thế giới]",
       "Giá vàng SJC, DOJI, PNJ, Bảo Tín, thế giới", "giá vàng hôm nay", "code:vn_daily_chat",
       ("gia vang", "vang sjc", "vang nhan", "xem vang"),
       tg="daily_data_commands.vang_cmd", chat="daily", menu="Giá vàng SJC, DOJI, PNJ, thế giới"),
    _e("tygia", LOOKUP, A, B, "doi_song", "/tygia [mã|100 usd]", "Tỷ giá Vietcombank, đổi tiền",
       "100 đô bằng bao nhiêu tiền Việt", "code:vn_daily_chat",
       ("ty gia", "doi tien", "usd", "do la", "yen nhat"),
       tg="daily_data_commands.tygia_cmd", chat="daily", menu="Tỷ giá Vietcombank, đổi tiền"),
    _e("xang", LOOKUP, A, B, "doi_song", "/xang [xăng|diesel|dầu hỏa]",
       "Giá xăng dầu Petrolimex, PVOIL", "giá xăng hôm nay", "code:vn_daily_chat",
       ("gia xang", "xang dau", "dau diesel"),
       tg="daily_data_commands.xang_cmd", chat="daily", menu="Giá xăng dầu Petrolimex/PVOIL"),
    _e("xoso", LOOKUP, A, B, "doi_song", "/xoso [miền|tỉnh] [ngày] [số vé]",
       "Kết quả xổ số theo miền/tỉnh, dò vé", "dò vé 123456 đài Tây Ninh hôm nay",
       "code:vn_daily_chat", ("xo so", "do ve", "ve so", "ket qua xo so", "lich quay"),
       tg="daily_data_commands.xoso_cmd", chat="daily", menu="Kết quả xổ số theo miền/tỉnh, dò vé"),
    _e("thoitiet", LOOKUP, A, B, "doi_song", "/thoitiet [địa điểm] · /thoitiet nha <tỉnh>",
       "Thời tiết; đặt nơi ở của mình", "mai Đà Lạt có mưa không",
       "code:weather_chat + tool:get_weather + code:weather_home",
       ("thoi tiet", "mua khong", "nhiet do", "noi o", "thanh pho mac dinh"),
       tg="daily_data_commands.thoitiet_cmd", chat="daily",
       menu="Thời tiết; /thoitiet nha <tỉnh> đặt nơi ở"),
    _e("bantin", PERSONAL, A, B, "doi_song", "/bantin on|off|<giờ>|xem",
       "Bản tin sáng cá nhân (xem thử, bật/tắt, đổi giờ)", "xem thử bản tin sáng",
       "cmd:cmd_bantin + set:set_bantin", ("ban tin sang", "ban tin"),
       tg="daily_data_commands.bantin_cmd", chat="daily",
       menu="Bản tin sáng cá nhân (on|off|giờ|xem)"),
    _e("chuy", LOOKUP, A, B, "doi_song", "/chuy", "Có gì cần mình chú ý hôm nay",
       "hôm nay có gì cần anh chú ý", "code:attention", ("can chu y", "can lo", "co gi can"),
       tg="daily_data_commands.chuy_cmd", chat="chuy", menu="Có gì cần anh chú ý hôm nay"),
    _e("yenlang", WRITE, A, B, "cai_dat", "/yenlang on|off|22-7",
       "Giờ yên lặng: ban đêm gom thông báo", "bật giờ yên lặng từ 22h tới 7h",
       "set:set_yenlang", ("yen lang", "gio yen lang", "dung lam phien", "gom thong bao"),
       tg="daily_data_commands.yenlang_cmd", chat="yenlang",
       menu="Giờ yên lặng, gom thông báo (on|off|22-7)"),
    # ── Huyền học ──
    _e("lich", LOOKUP, A, B, "huyen_hoc", "/lich <câu hỏi>",
       "Âm lịch, ngày tốt, ngày lễ, tuổi, sao hạn (không AI)", "ngày tốt tháng này",
       "code:lunar_chat + tool:lunar_calendar + cmd:cmd_lich",
       ("am lich", "ngay tot", "ngay le", "sao han", "xem ngay", "xem tuoi", "hoang dao"),
       tg="commands.lich_cmd", chat="divination",
       menu="Tra lịch âm, ngày lễ, ngày tốt, tuổi, sao hạn"),
    _e("tsh", LOOKUP, A, B, "huyen_hoc", "/tsh <ngày sinh> [họ tên]",
       "Thần số học (không AI)", "thần số học ngày sinh 15/3/1990", "cmd:cmd_tsh",
       ("than so hoc", "so chu dao"),
       tg="commands.tsh_cmd", chat="divination", menu="Thần số học theo ngày sinh"),
    _e("tsh_chitiet", LOOKUP, A, B, "huyen_hoc", "/tsh_chitiet <như /tsh>",
       "Thần số học + AI diễn giải", "luận giải thần số học 15/3/1990 chi tiết", "cmd:cmd_tsh",
       ("than so hoc chi tiet",), aliases=("ths_chitiet",),
       tg="commands.tsh_chitiet_cmd", chat="divination", menu="Thần số học + AI diễn giải"),
    _e("tuvi", LOOKUP, A, B, "huyen_hoc", "/tuvi <ngày sinh> <giờ> <nam|nữ>",
       "An lá số tử vi (không AI)", "xem tử vi 15/3/1990 9h30 nam", "cmd:cmd_tuvi",
       ("tu vi", "la so"),
       tg="commands.tuvi_cmd", chat="divination", menu="An lá số tử vi"),
    _e("tuvi_chitiet", LOOKUP, A, B, "huyen_hoc", "/tuvi_chitiet <như /tuvi>",
       "Lá số tử vi + AI luận giải", "luận giải tử vi chi tiết 15/3/1990 9h30 nam",
       "cmd:cmd_tuvi", ("luan giai tu vi", "tu vi chi tiet"),
       tg="commands.tuvi_chitiet_cmd", chat="divination", menu="Lá số tử vi + AI luận giải"),
    _e("battu", LOOKUP, A, B, "huyen_hoc", "/battu <ngày sinh> [giờ] <nam|nữ>",
       "Bát Tự tứ trụ, thập thần, đại vận (không AI)", "lập bát tự 15/3/1990 9h30 nam",
       "cmd:cmd_battu", ("bat tu", "tu tru", "dai van"),
       tg="commands.battu_cmd", chat="divination"),
    _e("battu_chitiet", LOOKUP, A, B, "huyen_hoc", "/battu_chitiet <như /battu>",
       "Bát Tự + AI luận giải", "luận giải bát tự chi tiết 15/3/1990 9h30 nam", "cmd:cmd_battu",
       ("luan giai bat tu", "bat tu chi tiet"),
       tg="commands.battu_chitiet_cmd", chat="divination"),
    _e("loban", LOOKUP, A, B, "huyen_hoc", "/loban <số đo> [cửa|giường|bàn thờ...]",
       "Thước Lỗ Ban: cung tốt xấu, số đo tốt gần nhất (không AI)",
       "cửa rộng 80cm có hợp lỗ ban không", "cmd:cmd_loban", ("lo ban", "thuoc lo ban"),
       tg="commands.loban_cmd", chat="divination"),
    _e("cungphi", LOOKUP, A, B, "huyen_hoc", "/cungphi <năm sinh> <nam|nữ>",
       "Cung phi Bát Trạch, hướng nhà tốt xấu (không AI)", "nữ 1990 hợp hướng nhà nào",
       "cmd:cmd_cungphi", ("cung phi", "huong nha", "bat trach", "hop huong"),
       tg="commands.cungphi_cmd", chat="divination"),
    _e("kymon", LOOKUP, A, B, "huyen_hoc", "/kymon [ngày] [giờ]", "Bàn Kỳ Môn Độn Giáp (không AI)",
       "lập bàn kỳ môn lúc này", "cmd:cmd_kymon", ("ky mon",),
       tg="commands.kymon_cmd", chat="divination",
       menu="Lập bàn Kỳ Môn Độn Giáp (mặc định lúc này)"),
    _e("kymon_chitiet", LOOKUP, A, B, "huyen_hoc", "/kymon_chitiet [thời điểm] <việc cần xem>",
       "Bàn Kỳ Môn + AI luận giải 1 việc", "kỳ môn xem 9h sáng mai ký hợp đồng có thuận không",
       "cmd:cmd_kymon", ("ky mon chi tiet",),
       tg="commands.kymon_chitiet_cmd", chat="divination",
       menu="Bàn Kỳ Môn + AI luận giải việc cần xem"),
    # ── Nhắc việc, việc nền ──
    _e("nhac", WRITE, A, B, "nhac_viec", "/nhac [giờ việc] · huy|hoan <số>",
       "Nhắc việc: xem, đặt, huỷ, hoãn", "nhắc anh 8h tối mai gọi mẹ",
       "tool:set_reminder + tool:list_reminders + tool:cancel_reminder + code:reminder_service",
       ("nhac viec", "dat nhac", "hen gio", "nhac lap", "huy nhac", "hoan nhac"),
       tg="reminder_commands.nhac_cmd", chat="reminder", chat_aliases=("nhacviec",),
       menu="Nhắc việc: xem, đặt, huỷ, hoãn"),
    _e("ngaynho", WRITE, A, B, "nhac_viec", "/ngaynho [mô tả] · xoa <số>",
       "Ngày nhớ: giỗ, sinh nhật, kỷ niệm", "nhớ giỗ ông nội 12/3 âm, nhắc trước 3 ngày",
       "tool:save_special_date + tool:list_special_dates", ("ngay nho", "ngay gio", "sinh nhat",
       "ky niem"),
       tg="reminder_commands.ngaynho_cmd", chat="reminder", chat_aliases=("ngay_nho",),
       menu="Ngày nhớ: giỗ, sinh nhật, kỷ niệm"),
    _e("viec", WRITE, A, B, "nhac_viec", "/viec <yêu cầu> · /viec [xem|huy <số>]",
       "Giao việc chạy nền, xem/huỷ việc", "việc nền của anh tới đâu rồi",
       "cmd:cmd_viec + suggest", ("viec nen", "chay nen", "giao viec"),
       tg="job_commands.viec_cmd", chat="viec", menu="Giao việc chạy nền; xem, huỷ việc"),
    _e("lichtacvu", WRITE, S, B, "nhac_viec", "/lichtacvu [câu có mốc lặp] · huy|tam dung|tiep <số>",
       "Tác vụ định kỳ: em tự chạy yêu cầu theo lịch",
       "mỗi sáng thứ 2 8h tóm tắt tin về VNM cho anh", "code:routines + cmd:cmd_lichtacvu",
       ("tac vu dinh ky", "lich tac vu", "dinh ky"),
       tg="job_commands.lichtacvu_cmd", chat="lichtacvu",
       menu="Tác vụ định kỳ: em tự chạy yêu cầu theo lịch"),
    # ── Chi tiêu, ăn uống ──
    _e("chitieu", WRITE, A, B, "chi_tieu", "/chitieu [kỳ|ds|<khoản>]",
       "Chi tiêu: tổng theo danh mục, khoản gần nhất, ghi khoản", "tháng này anh tiêu bao nhiêu",
       "code:expense_service + tool:add_expense + tool:expense_summary + cmd:cmd_chitieu_ds",
       ("chi tieu", "tieu bao nhieu", "ghi chi tieu", "khoan chi"),
       tg="expense_commands.chitieu_cmd", chat="expense",
       menu="Chi tiêu: tổng theo danh mục, ghi khoản"),
    _e("chitieu_xoa", WRITE, A, B, "chi_tieu", "/chitieu_xoa <số>", "Xoá 1 khoản chi",
       nl="suggest", kw=("xoa khoan chi", "xoa chi tieu"),
       tg="expense_commands.chitieu_xoa_cmd", chat="expense", chat_aliases=("chitieuxoa",),
       menu="Xoá 1 khoản chi theo số #"),
    _e("an", WRITE, A, B, "chi_tieu", "/an [món] · /an tuan · /an xoa <số>",
       "Ghi bữa ăn, xem kcal ước lượng", "hôm nay anh ăn bao nhiêu calo",
       "code:nutrition_service + cmd:cmd_an", ("an uong", "calo", "kcal", "ghi bua an"),
       tg="personal_commands.an_cmd", chat="nutrition", chat_aliases=("anuong",),
       menu="Ghi bữa ăn, xem kcal ước lượng hôm nay/tuần"),
    # ── Cổ phiếu ──
    _e("danhmuc", PERSONAL, A, B, "co_phieu", "/danhmuc", "Xem danh mục, lãi/lỗ, cảnh báo",
       "xem danh mục của anh", "code:portfolio_service + tool:get_portfolio",
       ("danh muc", "lai lo", "co phieu dang giu"),
       tg="portfolio_commands.portfolio_cmd", chat=CHAT_PORTFOLIO, menu="Xem danh mục cổ phiếu"),
    _e("themcp", WRITE, A, B, "co_phieu", "/themcp <MÃ> <SL> <GIÁ> [STOP] [TARGET]",
       "Thêm giao dịch mua", "đã mua FPT 100 giá 120", "code:portfolio_service",
       ("them co phieu", "ghi mua"),
       tg="portfolio_commands.portfolio_cmd", chat=CHAT_PORTFOLIO, menu="Thêm giao dịch mua"),
    _e("capnhatcp", WRITE, A, B, "co_phieu", "/capnhatcp <MÃ> <SL> <GIÁ> [STOP] [TARGET]",
       "Ghi đè vị thế", nl="suggest", kw=("cap nhat vi the", "sua gia von"),
       tg="portfolio_commands.portfolio_cmd", chat=CHAT_PORTFOLIO, menu="Cập nhật vị thế"),
    _e("bancp", WRITE, A, B, "co_phieu", "/bancp <MÃ> [SL|hết] [GIÁ]", "Bán bớt hoặc bán hết",
       "đã bán hết HPG giá 27", "code:portfolio_service", ("ban co phieu", "ghi ban"),
       tg="portfolio_commands.portfolio_cmd", chat=CHAT_PORTFOLIO, menu="Bán bớt hoặc bán hết"),
    _e("xoacp", WRITE, A, B, "co_phieu", "/xoacp <MÃ>", "Xoá mã khỏi danh mục", nl="suggest",
       kw=("xoa ma", "xoa khoi danh muc"),
       tg="portfolio_commands.portfolio_cmd", chat=CHAT_PORTFOLIO, menu="Xóa mã khỏi danh mục"),
    _e("lichsucp", PERSONAL, A, B, "co_phieu", "/lichsucp [MÃ]", "Sổ giao dịch cổ phiếu",
       "sổ giao dịch FPT", "cmd:cmd_lichsucp", ("so giao dich", "lich su giao dich"),
       tg="portfolio_commands.portfolio_cmd", chat=CHAT_PORTFOLIO, menu="Sổ giao dịch cổ phiếu"),
    _e("laicp", PERSONAL, A, B, "co_phieu", "/laicp [THÁNG]", "Lãi/lỗ đã chốt theo tháng",
       "tháng 10 anh chốt lãi bao nhiêu", "cmd:cmd_laicp", ("lai da chot", "lai lo da chot"),
       tg="portfolio_commands.portfolio_cmd", chat=CHAT_PORTFOLIO,
       menu="Lãi/lỗ đã chốt theo tháng"),
    _e("giaban", WRITE, A, B, "co_phieu", "/giaban <MÃ> <GIÁ>", "Bổ sung giá cho lệnh bán",
       nl="suggest", kw=("bo sung gia ban",),
       tg="portfolio_commands.portfolio_cmd", chat=CHAT_PORTFOLIO),
    _e("theodoi", PERSONAL, A, B, "co_phieu", "/theodoi [MÃ...]",
       "Danh sách theo dõi và ngưỡng giá", "danh sách mã anh đang theo dõi",
       "cmd:cmd_theodoi + suggest", ("theo doi", "watchlist"),
       tg="portfolio_commands.portfolio_cmd", chat=CHAT_PORTFOLIO,
       menu="Danh sách theo dõi và ngưỡng giá"),
    _e("botheodoi", WRITE, A, B, "co_phieu", "/botheodoi <MÃ>", "Bỏ theo dõi 1 mã", nl="suggest",
       kw=("bo theo doi",),
       tg="portfolio_commands.portfolio_cmd", chat=CHAT_PORTFOLIO),
    _e("canhbaogia", WRITE, A, B, "co_phieu", "/canhbaogia <MÃ> [về|lên] <GIÁ>",
       "Báo khi giá chạm ngưỡng", "báo anh khi HPG về 25", "code:portfolio_service",
       ("canh bao gia", "bao khi gia", "nguong gia"),
       tg="portfolio_commands.portfolio_cmd", chat=CHAT_PORTFOLIO,
       menu="Báo khi giá chạm ngưỡng anh đặt"),
    _e("xoacanhbao", WRITE, A, B, "co_phieu", "/xoacanhbao <MÃ> [GIÁ]", "Xoá ngưỡng giá",
       nl="suggest", kw=("xoa canh bao",),
       tg="portfolio_commands.portfolio_cmd", chat=CHAT_PORTFOLIO),
    # ── Trí nhớ, dữ liệu ──
    _e("memory", PERSONAL, A, B, "tri_nho", "/memory [on|off]",
       "Xem trí nhớ dài hạn (đánh số), bật/tắt", "em đang nhớ gì về anh",
       "cmd:cmd_memory + set:set_memory", ("tri nho", "em nho gi"),
       tg="commands.memory_family_cmd", chat="memory",
       menu="Xem trí nhớ dài hạn (đánh số), on|off"),
    _e("nho", WRITE, A, B, "tri_nho", "/nho <điều cần nhớ>", "Nhờ em nhớ (ghim) 1 điều",
       "nhớ giúp anh là anh dị ứng tôm", "tool:remember_fact", ("nho giup", "ghi nho"),
       tg="commands.memory_family_cmd", chat="memory", menu="Nhờ em nhớ (ghim) 1 điều"),
    _e("quen", WRITE, A, B, "tri_nho", "/quen <số|từ khoá>", "Quên 1 mục trí nhớ", nl="suggest",
       kw=("quen muc", "quen di"),
       tg="commands.memory_family_cmd", chat="memory", menu="Quên 1 mục trí nhớ (số hoặc từ khoá)"),
    _e("ghim", WRITE, A, B, "tri_nho", "/ghim <số>", "Ghim 1 mục trí nhớ", nl="suggest",
       kw=("ghim muc",),
       tg="commands.memory_family_cmd", chat="memory", menu="Ghim 1 mục trí nhớ theo số"),
    _e("boghim", WRITE, A, B, "tri_nho", "/boghim <số>", "Bỏ ghim 1 mục trí nhớ", nl="suggest",
       kw=("bo ghim",),
       tg="commands.memory_family_cmd", chat="memory", menu="Bỏ ghim 1 mục trí nhớ"),
    _e("forget", DANGER, A, B, "tri_nho", "/forget", "Xoá hết trí nhớ dài hạn", nl="suggest",
       kw=("xoa het tri nho", "quen het"),
       tg="commands.memory_family_cmd", chat="memory", menu="Xoá hết trí nhớ dài hạn"),
    _e("notes", PERSONAL, A, B, "tri_nho", "/notes [từ khoá]", "Xem/tìm ghi chú",
       "em ghi chú gì cho anh rồi", "tool:list_notes + tool:save_note", ("ghi chu", "note"),
       tg="commands.memory_family_cmd", chat="memory", menu="Xem/tìm ghi chú"),
    _e("xoaghichu", WRITE, A, B, "tri_nho", "/xoaghichu <số>", "Xoá 1 ghi chú", nl="suggest",
       kw=("xoa ghi chu",),
       tg="commands.memory_family_cmd", chat="memory", menu="Xoá 1 ghi chú theo số"),
    _e("history", PERSONAL, A, B, "tri_nho", "/history", "Xem 10 lượt chat gần nhất",
       "xem lịch sử chat gần đây", "cmd:cmd_history + tool:search_history",
       ("lich su chat", "lich su tro chuyen", "hom truoc em noi gi"),
       tg="commands.history_cmd", chat="history", menu="Xem lịch sử"),
    _e("xuatdulieu", PERSONAL, A, B, "tri_nho", "/xuatdulieu", "Tải/xem dữ liệu em đang lưu",
       nl="suggest", kw=("xuat du lieu", "tai du lieu"),
       tg="commands.memory_family_cmd", chat="memory", menu="Tải file dữ liệu em đang lưu"),
    _e("xoadulieu", DANGER, A, B, "tri_nho", "/xoadulieu", "Xoá trí nhớ, ghi chú, lịch sử chat",
       nl="suggest", kw=("xoa du lieu", "xoa het du lieu"),
       tg="commands.memory_family_cmd", chat="memory", menu="Xoá trí nhớ, ghi chú, lịch sử chat"),
    _e("reset", DANGER, A, B, "tri_nho", "/reset", "Xoá ngữ cảnh chat", nl="suggest",
       kw=("xoa ngu canh", "lam moi cuoc tro chuyen"),
       tg="commands.reset_chat_cmd", chat="reset", menu="Xoá ngữ cảnh chat"),
    _e("hoithan", WRITE, A, B, "cai_dat", "/hoithan on|off",
       "Em chủ động hỏi thăm việc mình kể", "tắt hỏi thăm chủ động đi em",
       "set:set_hoithan + code:proactive_service", ("hoi than", "hoi tham"),
       tg="commands.memory_family_cmd", chat="memory",
       menu="Bật/tắt em chủ động hỏi thăm việc anh kể (on|off)"),
    _e("hoc", WRITE, A, B, "tri_nho", "/hoc [ok|bo]", "Duyệt cách trả lời em tự rút ra",
       '"ghi nhớ"/"bỏ" ngay sau câu em hỏi', "code:style_approval", ("cach tra loi",),
       tg="commands.memory_family_cmd", chat="memory",
       menu="Duyệt cách trả lời em tự rút ra (ok|bo)"),
    # ── Tiện ích ──
    _e("gia", LOOKUP, A, B, "tien_ich", "/gia <sản phẩm>", "Tìm và so sánh giá sản phẩm",
       "tìm giá iPhone 16 Pro", "cmd:cmd_gia", ("tim gia", "so sanh gia", "gia san pham"),
       tg="commands.price_cmd", chat="gia", menu="Tìm giá sản phẩm"),
    _e("dich", LOOKUP, A, B, "tien_ich", "/dich [ja>vi|vi>ja] <nội dung>", "Dịch chat Nhật-Việt",
       "dịch giúp anh: お世話になります", "cmd:cmd_dich", ("dich", "tieng nhat"),
       tg="commands.dich_cmd", chat="dich", menu="Dịch chat Nhật-Việt"),
    _e("prompt", LOOKUP, A, B, "tien_ich", "/prompt <mô tả>", "Viết prompt tạo ảnh",
       "viết prompt tạo ảnh cô gái áo dài", "cmd:cmd_prompt", ("viet prompt", "prompt"),
       tg="commands.prompt_cmd", chat="prompt", menu="Viết prompt tạo ảnh"),
    _e("anh", LOOKUP, A, B, "tien_ich", "/anh <mô tả> · /anh on|off (admin)",
       "Tạo ảnh thật (Agnes AI)", "tạo ảnh con mèo đội nón lá", "cmd:cmd_anh + suggest",
       ("tao anh", "ve anh", "ve hinh"),
       tg="provider_commands.anh_cmd", chat="anh", menu="Tạo ảnh thật từ mô tả (Agnes AI)"),
    _e("noi", WRITE, A, B, "cai_dat", "/noi on|off|auto · /noi giong <tên>", "Trả lời kèm giọng nói",
       "bật trả lời bằng giọng nói", "set:set_noi", ("giong noi", "tin thoai", "doc cho nghe"),
       tg="personal_commands.noi_cmd", chat="noi",
       menu="Trả lời kèm giọng nói (on|off|auto · giong <tên>)"),
    # ── Quản trị bot ──
    _e("status", LOOKUP, S, B, "admin", "/status", "Trạng thái provider",
       "provider đang chạy cái nào", "cmd:cmd_status", ("provider", "trang thai bot"),
       tg="provider_commands.status_cmd", chat="status", chat_admin=True, menu="Xem provider"),
    _e("loi", LOOKUP, O, B, "admin", "/loi", "Lỗi gom nhóm hôm nay",
       "hôm nay bot có lỗi gì không", "cmd:cmd_loi", ("loi hom nay", "xem loi", "bao loi"),
       tg="provider_commands.loi_cmd", chat="loi", menu="Xem lỗi gom nhóm hôm nay"),
    _e("phienban", LOOKUP, S, B, "admin", "/phienban [số bản|all]",
       "Phiên bản đang chạy và lịch sử cập nhật", "bot có gì mới", "cmd:cmd_phienban",
       ("phien ban", "co gi moi", "ban moi", "lich su cap nhat"),
       tg="provider_commands.phienban_cmd", chat="phienban", chat_admin=True,
       menu="Phiên bản và lịch sử cập nhật"),
    _e("thongke", LOOKUP, S, B, "admin", "/thongke [Nd|Ngiờ]", "Thống kê lượt gọi theo user/model",
       "thống kê lượt gọi 7 ngày", "cmd:cmd_thongke", ("thong ke", "luot goi"),
       tg="commands.thongke_cmd", chat="thongke", chat_admin=True,
       menu="Thống kê lượt gọi theo user/model"),
    _e("danhgia", LOOKUP, O, B, "admin", "/danhgia [Nd]", "Số 👍/👎 theo provider",
       "thống kê đánh giá câu trả lời", "cmd:cmd_danhgia", ("danh gia", "like dislike"),
       tg="feedback_commands.danhgia_cmd", chat="feedback",
       menu="Đánh giá 👍/👎 câu trả lời theo provider"),
    _e("xuateval", ADMIN, O, TG, "admin", "/xuateval [Nd]", "Xuất câu bị 👎 thành case đánh giá",
       nl="suggest", kw=("xuat eval",),
       tg="feedback_commands.xuateval_cmd", menu="Xuất câu bị 👎 thành case đánh giá (YAML)"),
    _e("app", ADMIN, O, TG, "admin", "/app [moi <tên>|xoa <số>]",
       "Ghép/thu hồi app Android màn hình thông tin", nl="suggest",
       kw=("app android", "man hinh thong tin", "ghep app"),
       tg="personal_commands.app_cmd"),
    _e("model", ADMIN, S, B, "admin", "/model [tên|auto]", "Xem/đổi model chat", nl="suggest",
       kw=("doi model", "model nao"),
       tg="provider_commands.model_cmd", chat="model", chat_admin=True, menu="Xem/đổi model"),
    _e("router9", DANGER, S, B, "admin", "/router9 on|off", "Bật/tắt 9Router", nl="suggest",
       kw=("9router", "router9"),
       tg="provider_commands.router9_toggle_cmd", chat="router9", chat_admin=True,
       menu="Bật/tắt 9Router (on|off)"),
    _e("userouter9", ADMIN, S, B, "admin", "/userouter9", "Ép thử lại 9Router", nl="suggest",
       kw=("thu lai 9router",),
       tg="provider_commands.userouter9_cmd", chat="userouter9", chat_admin=True),
    _e("tavily", WRITE, S, B, "admin", "/tavily on|off", "Bật/tắt tra web Tavily",
       "tắt Tavily đi em", "set:set_tavily", ("tavily", "tra web"),
       tg="provider_commands.tavily_toggle_cmd", chat="tavily", chat_admin=True,
       menu="Bật/tắt tra web Tavily (on|off)"),
    _e("agent", LOOKUP, S, B, "admin", "/agent <câu hỏi>", "Agent tự tra cứu nhiều bước",
       nl="-", kw=("agent",),
       tg="commands.agent_cmd", chat="agent", chat_admin=True,
       menu="Hỏi agent tự tra cứu nhiều bước (thử nghiệm)"),
    _e("rag", LOOKUP, S, B, "admin", "/rag <câu hỏi>", "Tra kho kiến thức rag/",
       "trong tài liệu của anh nói gì về ...", "tool:rag_search", ("kho kien thuc", "tai lieu"),
       tg="commands.rag_cmd", chat="rag", chat_admin=True,
       menu="Tra cứu kiến thức trong thư mục rag/"),
    _e("ragxuly", ADMIN, S, B, "admin", "/ragxuly <file>", "Dọn file md OCR trong rag/",
       nl="suggest", kw=("don file", "ragxuly"),
       tg="commands.ragxuly_cmd", chat="ragxuly", chat_admin=True,
       menu="Dọn file md OCR trong rag/ (AI thêm heading)"),
    _e("bangtinsang", ADMIN, S, B, "admin", "/bangtinsang", "Gửi ngay Bản tin sáng tới Zalo + Zoom",
       nl="suggest", kw=("gui ban tin", "phat ban tin"), aliases=("bantinsang",),
       tg="commands.bangtinsang_cmd", chat="bangtinsang", chat_admin=True,
       menu="Gửi ngay Bản tin sáng tới Zalo + Zoom"),
    _e("tintuc", LOOKUP, S, B, "admin", "/tintuc", "Đọc Điểm tin mới ngay tại kênh đang gọi",
       "điểm tin sáng nay có gì", "cmd:cmd_tintuc", ("diem tin", "tin tuc"),
       tg="commands.tintuc_cmd", chat="tintuc", chat_admin=True,
       menu="Đọc Điểm tin mới ngay trong Telegram"),
    # ── Nhóm Zalo, Facebook ──
    _e("nhomzalo", LOOKUP, S, B, "nhom", "/nhomzalo [từ khoá]",
       "Mọi nhóm Zalo bot đang ở: tên + group ID", nl="suggest",
       kw=("group id", "id nhom", "lay id nhom", "nhom bot dang o"),
       tg="zalo_bridge_commands.nhomzalo_cmd", chat=CHAT_GROUP, chat_admin=True),
    _e("nhom", LOOKUP, S, B, "nhom", "/nhom", "Danh sách nhóm Zalo đang theo dõi",
       "đang theo dõi những nhóm Zalo nào", "cmd:cmd_nhom", ("nhom zalo", "danh sach nhom"),
       tg="zalo_bridge_commands.nhom_cmd", chat=CHAT_GROUP, menu="Xem danh sách nhóm Zalo"),
    _e("themnhom", ADMIN, S, B, "nhom", "/themnhom <số|group_id> [tên]", "Thêm nhóm Zalo theo dõi",
       nl="suggest", kw=("them nhom",),
       tg="zalo_bridge_commands.themnhom_cmd", chat=CHAT_GROUP, menu="Thêm nhóm Zalo theo dõi"),
    _e("xoanhom", ADMIN, S, B, "nhom", "/xoanhom <group_id|tên>", "Ngừng theo dõi 1 nhóm",
       nl="suggest", kw=("xoa nhom", "bo nhom"),
       tg="zalo_bridge_commands.xoanhom_cmd", chat=CHAT_GROUP, menu="Ngừng theo dõi 1 nhóm Zalo"),
    _e("tongket", LOOKUP, S, B, "nhom", "/tongket <tên> [24h|7d|homnay|homqua] · on|off",
       "Tổng kết nhóm Zalo (AI)", "tóm tắt nhóm Gia đình hôm nay", "cmd:cmd_tongket + suggest",
       ("tong ket nhom", "tom tat nhom"),
       tg="zalo_bridge_commands.tongket_cmd", chat=CHAT_GROUP, menu="Tổng kết nhóm Zalo (AI)"),
    _e("dangnoi", LOOKUP, S, B, "nhom", "/dangnoi <tên>", "Nguyên văn thảo luận hôm nay",
       "nhóm Gia đình đang nói gì", "cmd:cmd_dangnoi", ("dang noi gi", "thao luan"),
       tg="zalo_bridge_commands.dangnoi_cmd", chat=CHAT_GROUP,
       menu="Xem nguyên văn thảo luận hôm nay"),
    _e("tinhieu", ADMIN, S, B, "nhom", "/tinhieu [mã|on|off|top|nguoi|nhip]",
       "Tín hiệu mua/bán cổ phiếu từ nhóm Zalo gửi Zoom + Zalo", nl="suggest",
       kw=("tin hieu", "goi y mua", "nhom goi y", "ai goi y", "room khuyen nghi"),
       tg="zalo_bridge_commands.tinhieu_cmd", chat=CHAT_GROUP),
    *(
        _e(f"fb_{n}", DANGER if n == "reset" else ADMIN, S, B, "nhom", u, d, nl="suggest",
           kw=("facebook", "bai fb", "fanpage", *k), tg=f"zalo_bridge_commands.fb_{n}_cmd",
           chat=CHAT_FACEBOOK, menu=m)
        # (tên, cú pháp, mô tả /help, từ khoá, mô tả menu "/" Telegram)
        for n, u, d, k, m in (
            ("nhom", "/fb_nhom", "Xem nhóm nguồn Facebook", ("nhom nguon",),
             "Xem nhóm nguồn Facebook"),
            ("pages", "/fb_pages", "Xem các Facebook Page đã cấu hình", ("facebook page",),
             "Xem các Facebook Page đã cấu hình"),
            ("market", "/fb_market <stock|news> [dang]", "Xem thử/đăng bài Page chứng khoán",
             ("bai chung khoan", "dang bai page"), "Xem thử/đăng bài Page chứng khoán"),
            ("themnhom", "/fb_themnhom <số|group_id> [tên]", "Thêm nhóm nguồn Facebook",
             ("them nhom nguon",), "Thêm nhóm nguồn Facebook"),
            ("xoanhom", "/fb_xoanhom <group_id|tên>", "Bỏ nhóm nguồn Facebook",
             ("bo nhom nguon", "xoa nhom nguon"), "Bỏ nhóm nguồn Facebook"),
            ("xem", "/fb_xem <post_id>", "Xem bài chờ duyệt", ("bai cho duyet", "xem bai"),
             "Xem bài Facebook chờ duyệt"),
            ("sua", "/fb_sua <post_id> <nội dung>", "Sửa bài chờ duyệt", ("sua bai",),
             "Sửa bài Facebook chờ duyệt"),
            ("link", "/fb_link <post_id> <url>", "Nhập link affiliate cho bài chờ",
             ("link affiliate", "nhap link"), "Nhập link Shopee affiliate cho bài chờ"),
            ("ok", "/fb_ok <post_id>", "Duyệt và đăng Facebook",
             ("duyet bai", "duyet va dang", "dang facebook", "dang bai"),
             "Duyệt và đăng Facebook"),
            ("check", "/fb_check <post_id>", "Kiểm tra bài đã công khai", ("kiem tra bai",),
             "Kiểm tra bài Facebook đã công khai"),
            ("reconcile", "/fb_reconcile", "Đối soát lượt đăng bị gián đoạn", ("doi soat",),
             "Đối soát lượt đăng Facebook bị gián đoạn"),
            ("boqua", "/fb_boqua <post_id>", "Bỏ bài chờ", ("bo qua bai", "bo bai"),
             "Bỏ bài Facebook chờ"),
            ("reset", "/fb_reset xacnhan", "Xoá bài Facebook đã lưu", ("xoa bai facebook",),
             "Xóa bài Facebook đã lưu (cần gõ kèm xacnhan)"),
            ("boloc", "/fb_boloc [reset]", "Thống kê bộ lọc Zalo → Facebook", ("bo loc",),
             "Thống kê bộ lọc Zalo → Facebook"),
            ("loclai", "/fb_loclai", "Lọc lại các bài đang chờ", ("loc lai",),
             "Lọc lại các bài đang chờ duyệt"),
        )
    ),
    # ── Quyền Zalo/Zoom (chỉ Telegram của chủ bot) ──
    _e("zalo", ADMIN, O, TG, "quyen", "/zalo", "Đăng nhập (QR) / xem trạng thái Zalo B",
       nl="suggest", kw=("dang nhap zalo",),
       tg="zalo_login.zalo_cmd", menu="Đăng nhập hoặc xem trạng thái Zalo B"),
    _e("zalologout", ADMIN, O, TG, "quyen", "/zalologout", "Đăng xuất Zalo B", nl="suggest",
       kw=("dang xuat zalo",),
       tg="zalo_login.zalologout_cmd", menu="Đăng xuất Zalo B"),
    _e("zalopair", ADMIN, O, TG, "quyen", "/zalopair <id> [tên] [xưng hô]",
       "Cấp quyền thành viên Zalo", nl="suggest", kw=("cap quyen", "them thanh vien"),
       tg="pairing_commands.zalopair_cmd", menu="Cấp quyền thành viên Zalo"),
    _e("zaloadmin", ADMIN, O, TG, "quyen", "/zaloadmin <id> [tên] [xưng hô]",
       "Cấp quyền admin Zalo", nl="suggest", kw=("cap admin", "len admin"),
       tg="pairing_commands.zaloadmin_cmd", menu="Cấp quyền admin Zalo"),
    _e("zalohaquyen", ADMIN, O, TG, "quyen", "/zalohaquyen <id>", "Hạ admin về thành viên",
       nl="suggest", kw=("ha quyen",),
       tg="pairing_commands.zalohaquyen_cmd", menu="Hạ quyền admin Zalo về thành viên"),
    _e("zalokhoa", ADMIN, O, TG, "quyen", "/zalokhoa <id>", "Khoá 1 user Zalo", nl="suggest",
       kw=("khoa zalo", "khoa user"),
       tg="pairing_commands.zalokhoa_cmd", menu="Khóa 1 user Zalo"),
    _e("zalomokhoa", ADMIN, O, TG, "quyen", "/zalomokhoa <id>", "Mở khoá 1 user Zalo",
       nl="suggest", kw=("mo khoa",),
       tg="pairing_commands.zalomokhoa_cmd", menu="Mở khóa 1 user Zalo"),
    _e("zaloxoa", DANGER, O, TG, "quyen", "/zaloxoa <id>", "Xoá pairing kèm dữ liệu 1 user Zalo",
       nl="suggest", kw=("xoa user zalo", "xoa nguoi dung"),
       tg="pairing_commands.zaloxoa_cmd", menu="Xoá pairing 1 user Zalo"),
    _e("zalodanhsach", LOOKUP, O, TG, "quyen", "/zalodanhsach", "Danh sách user Zalo",
       nl="suggest", kw=("danh sach zalo", "user zalo"),
       tg="pairing_commands.zalodanhsach_cmd", menu="Xem danh sách user Zalo"),
    _e("zoompair", ADMIN, O, TG, "quyen", "/zoompair <jid> [tên]", "Cấp quyền 1 jid Zoom",
       nl="suggest", kw=("pair zoom",),
       tg="pairing_commands.zoompair_cmd", menu="Cấp quyền 1 jid Zoom"),
    _e("zoomxoa", DANGER, O, TG, "quyen", "/zoomxoa", "Gỡ pairing Zoom", nl="suggest",
       kw=("go zoom",),
       tg="pairing_commands.zoomxoa_cmd", menu="Gỡ pairing Zoom"),
    _e("zoomstatus", LOOKUP, O, TG, "quyen", "/zoomstatus", "Xem jid Zoom đã pair", nl="suggest",
       kw=("jid zoom",),
       tg="pairing_commands.zoomstatus_cmd", menu="Xem jid Zoom đã pair"),
)
# fmt: on

# Menu "/" của Telegram: lệnh hay dùng nhất, đúng thứ tự hiện trong menu (bot chỉ phục vụ
# chủ bot nên 1 menu chung, không cần BotCommandScope). Mô tả lấy từ ``menu`` của lệnh.
TELEGRAM_MENU: tuple[str, ...] = (
    "help",
    "huy",
    "chuy",
    "nhac",
    "chitieu",
    "danhmuc",
    "theodoi",
    "vang",
    "xoso",
    "thoitiet",
    "lich",
    "tuvi",
    "dich",
    "notes",
    "memory",
    "viec",
    "sai",
    "status",
)

_BY_NAME: dict[str, Entry] = {}
# "/<tên>" -> Entry của mọi lệnh Zalo/Zoom (tên, tên phụ, tên phụ riêng Zalo/Zoom).
_CHAT_ROUTES: dict[str, Entry] = {}
for _entry in ENTRIES:
    _BY_NAME[_entry.name] = _entry
    for _alias in (*_entry.aliases, *_entry.chat_aliases):
        _BY_NAME.setdefault(_alias, _entry)
    if _entry.chat:
        for _name in (_entry.name, *_entry.aliases, *_entry.chat_aliases):
            _CHAT_ROUTES.setdefault(f"/{_name}", _entry)


def get(name: str) -> Entry | None:
    """Entry theo tên lệnh ("/tuvi", "tuvi", "/ths_chitiet" đều được)."""
    return _BY_NAME.get((name or "").strip().lstrip("/").lower().split("@", 1)[0])


def telegram_commands() -> list[tuple[str, str]]:
    """(tên lệnh, handler "<module>.<hàm>") của MỌI lệnh Telegram theo thứ tự danh bạ -
    bot_app.build_application() đăng ký đúng danh sách này (tên + tên phụ)."""
    return [(name, e.tg) for e in ENTRIES if e.tg for name in (e.name, *e.aliases)]


def telegram_descriptions() -> list[tuple[str, str]]:
    """(tên lệnh, mô tả) cho danh sách lệnh "/" của Telegram (lệnh có ``menu``)."""
    return [(e.name, e.menu) for e in ENTRIES if e.menu]


def chat_entry(command: str) -> Entry | None:
    """Entry Zalo/Zoom của chữ lệnh đã chuẩn hoá ("/tuvi", chữ thường, bỏ "@bot");
    None = lệnh lạ hoặc chỉ có trên Telegram."""
    return _CHAT_ROUTES.get(command)


def channel_kind(channel: str) -> str:
    return "tg" if channel == "telegram" else "chat"


def visible(entry: Entry, role: str, channel: str) -> bool:
    """Người có vai trò ``role`` trên ``channel`` có thấy/dùng lệnh này không."""
    if role not in entry.allowed_roles:
        return False
    return entry.where == "both" or channel_kind(channel) == "tg"


def entries_for(role: str, channel: str, topic: str | None = None) -> list[Entry]:
    return [e for e in ENTRIES if visible(e, role, channel) and (topic is None or e.topic == topic)]


def topics_for(role: str, channel: str = "telegram") -> list[Topic]:
    """Chủ đề người này thấy (bỏ chủ đề không còn lệnh nào trên kênh này)."""
    return [t for t in TOPICS if role in _ROLES[t.roles] and entries_for(role, channel, t.key)]


def find_topic(query: str, role: str, channel: str = "telegram") -> Topic | None:
    q = fold(query).strip(" ?.!")
    if not q:
        return None
    for topic in topics_for(role, channel):
        names = {topic.key.replace("_", " "), fold(topic.title), fold(topic.word), *topic.aliases}
        if q in names:
            return topic
    return None


# ─── Tìm lệnh theo câu "làm sao để ..." ────────────────────────────────────

_WORD_RE = re.compile(r"[a-z0-9]+")


@functools.cache
def _mention_re() -> re.Pattern[str]:
    """ "/<tên lệnh>" + ranh giới từ, 1 regex cho mọi lệnh (trước đây 1 lần re.search mỗi
    lệnh). Mỗi dấu "/" khớp tối đa 1 tên (tên ngắn là tiền tố tên dài thì ký tự kế tiếp
    là chữ, không có ranh giới) nên findall ra đúng tập tên khớp. Dựng lúc dùng lần đầu."""
    return re.compile(r"/(" + "|".join(entry.name for entry in ENTRIES) + r")\b")


def search(query: str, role: str, channel: str, limit: int = 3) -> list[Entry]:
    """Lệnh khớp câu hỏi nhất: ưu tiên cụm từ khoá trùng nguyên cụm (dài hơn điểm
    cao hơn), rồi tới từ trong tên chủ đề/mô tả. Không khớp cụm nào -> []."""
    q = " " + " ".join(_WORD_RE.findall(fold(query))) + " "
    raw = (query or "").lower()
    # Tên lệnh chỉ tính khi gõ kèm "/" ("/xoso là gì"): tên trùng chữ thường
    # ("anh", "an", "gia", "nho", "lich") không được kéo theo lệnh. Câu không có "/"
    # (gần như mọi tin chat - hàm này chạy mỗi lượt qua nl_commands.router_groups) bỏ qua.
    mentioned = set(_mention_re().findall(raw)) if "/" in raw else ()
    scored: list[tuple[int, int, Entry]] = []
    for index, entry in enumerate(entries_for(role, channel)):
        score = 0
        for phrase in entry.keywords:
            if f" {phrase} " in q:
                score = max(score, 10 * len(phrase.split()))
        if entry.name in mentioned:
            score = max(score, 30)
        if score:
            scored.append((-score, index, entry))
    scored.sort()
    return [entry for _, _, entry in scored[:limit]]


def topic_title(key: str) -> str:
    topic = _TOPIC_BY_KEY.get(key)
    return f"{topic.icon} {topic.title}" if topic else "Chung"


def _routes(entry: Entry) -> list[str]:
    return [r.strip() for r in entry.nl.split("+")]


def had_nl_before(entry: Entry) -> bool:
    """Lệnh đã có đường câu tự nhiên TRƯỚC gói "hỏi tự nhiên" (đường code cũ, tool router)."""
    return any(
        (r.startswith("code:") and r != "code:nl_commands") or r.startswith("tool:")
        for r in _routes(entry)
    )


def inventory_counts() -> dict[str, int]:
    """Số lệnh (không tính alias) theo tình trạng đường câu tự nhiên - cho báo cáo:
    before = đã có trước gói này; nl = có sau gói này (chạy được từ câu tự nhiên);
    suggest = chỉ được chỉ ra lệnh; none = không cần (nút bấm, lệnh điều khiển)."""
    counts = {"total": len(ENTRIES), "before": 0, "nl": 0, "suggest": 0, "none": 0}
    for entry in ENTRIES:
        counts["before"] += had_nl_before(entry)
        if entry.has_nl:
            counts["nl"] += 1
        elif entry.suggest_only:
            counts["suggest"] += 1
        else:
            counts["none"] += 1
    return counts
