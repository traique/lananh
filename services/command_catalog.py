"""Danh bạ MỌI lệnh / của bot (Telegram, Zalo, Zoom) ở MỘT chỗ.

Dùng cho:
- trợ giúp ngắn ``/help``, ``/help <chủ đề>`` (``/help all`` vẫn là bản đầy đủ cũ),
- câu hỏi tự nhiên "em làm được gì", "làm sao để dò vé số", "có lệnh nào xem vàng
  không" (services/nl_commands.py trả lời bằng code, không gọi AI),
- bảng kiểm kê trong docs/nang-cap-jarvis.md (mục "Hỏi tự nhiên").

Mỗi ``Entry``: tên lệnh (không có "/"), ``group`` (tra cứu / cá nhân / ghi dữ liệu /
quản trị / nguy hiểm), ai dùng được (``roles``: all | staff = chủ bot + admin Zalo |
owner), kênh (``where``: both | tg = chỉ Telegram), chủ đề trợ giúp (``topic``),
cú pháp ngắn, mô tả, 1 câu nói tự nhiên mẫu (``ask``) và ``nl`` - đường câu tự
nhiên hiện có:

- ``code:<module>``: đường nhanh bằng code (không AI) đã có từ trước;
- ``tool:<tên>``: tool router chat (services/tools.py, services/history_search.py);
- ``cmd:<tên>``: ToolSpec "chạy lệnh" của services/nl_commands.py (kết quả nguyên văn);
- ``set:<tên>``: ToolSpec bật/tắt cài đặt (câu phải nói rõ bật/tắt; bot nhắc lại lệnh);
- ``suggest``: KHÔNG tự chạy từ câu tự nhiên - bot chỉ ra đúng lệnh cần gõ;
- ``-``: không cần (lệnh điều khiển, nút bấm).

Nhiều đường thì nối bằng " + ". Thêm lệnh mới: thêm 1 dòng ở đây (test
test_nl_commands kiểm mọi lệnh đăng ký trong bot_app đều có mặt).
"""

from __future__ import annotations

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
        "Lịch, tử vi, thần số học, kỳ môn",
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
    aliases: tuple[str, ...] = ()

    @property
    def allowed_roles(self) -> frozenset[str]:
        return _ROLES[self.roles]

    @property
    def suggest_only(self) -> bool:
        return "suggest" in self.nl

    @property
    def has_nl(self) -> bool:
        return self.nl not in ("-", "suggest")


def _e(name, group, roles, where, topic, usage, summary, ask="", nl="-", kw=(), aliases=()):
    return Entry(name, group, roles, where, topic, usage, summary, ask, nl, tuple(kw), aliases)


A, S, O = "all", "staff", "owner"
B, TG = "both", "tg"
# fmt: off
ENTRIES: tuple[Entry, ...] = (
    # ── Chung ──
    _e("start", LOOKUP, A, B, "chung", "/start", "Lời chào", nl="code:nl_commands"),
    _e("help", LOOKUP, A, B, "chung", "/help [chủ đề|all]", "Hướng dẫn ngắn, theo chủ đề, đầy đủ",
       'em làm được gì?', "code:nl_commands", ("huong dan", "tro giup", "lenh")),
    _e("huy", PERSONAL, A, B, "chung", "/huy", "Dừng câu trả lời đang chạy dở", nl="-"),
    _e("sai", WRITE, A, B, "chung", "/sai [lý do]", "Báo câu trả lời vừa rồi chưa ổn",
       nl="-", kw=("bao sai", "tra loi sai")),
    # ── Đời sống ──
    _e("vang", LOOKUP, A, B, "doi_song", "/vang [sjc|nhẫn|doji|pnj|thế giới]",
       "Giá vàng SJC, DOJI, PNJ, Bảo Tín, thế giới", "giá vàng hôm nay", "code:vn_daily_chat",
       ("gia vang", "vang sjc", "vang nhan", "xem vang")),
    _e("tygia", LOOKUP, A, B, "doi_song", "/tygia [mã|100 usd]", "Tỷ giá Vietcombank, đổi tiền",
       "100 đô bằng bao nhiêu tiền Việt", "code:vn_daily_chat",
       ("ty gia", "doi tien", "usd", "do la", "yen nhat")),
    _e("xang", LOOKUP, A, B, "doi_song", "/xang [xăng|diesel|dầu hỏa]",
       "Giá xăng dầu Petrolimex, PVOIL", "giá xăng hôm nay", "code:vn_daily_chat",
       ("gia xang", "xang dau", "dau diesel")),
    _e("xoso", LOOKUP, A, B, "doi_song", "/xoso [miền|tỉnh] [ngày] [số vé]",
       "Kết quả xổ số theo miền/tỉnh, dò vé", "dò vé 123456 đài Tây Ninh hôm nay",
       "code:vn_daily_chat", ("xo so", "do ve", "ve so", "ket qua xo so", "lich quay")),
    _e("thoitiet", LOOKUP, A, B, "doi_song", "/thoitiet [địa điểm] · /thoitiet nha <tỉnh>",
       "Thời tiết; đặt nơi ở của mình", "mai Đà Lạt có mưa không",
       "code:weather_chat + tool:get_weather + code:weather_home",
       ("thoi tiet", "mua khong", "nhiet do", "noi o", "thanh pho mac dinh")),
    _e("bantin", PERSONAL, A, B, "doi_song", "/bantin on|off|<giờ>|xem",
       "Bản tin sáng cá nhân (xem thử, bật/tắt, đổi giờ)", "xem thử bản tin sáng",
       "cmd:cmd_bantin + set:set_bantin", ("ban tin sang", "ban tin")),
    _e("chuy", LOOKUP, A, B, "doi_song", "/chuy", "Có gì cần mình chú ý hôm nay",
       "hôm nay có gì cần anh chú ý", "code:attention", ("can chu y", "can lo", "co gi can")),
    _e("yenlang", WRITE, A, B, "cai_dat", "/yenlang on|off|22-7",
       "Giờ yên lặng: ban đêm gom thông báo", "bật giờ yên lặng từ 22h tới 7h",
       "set:set_yenlang", ("yen lang", "gio yen lang", "dung lam phien", "gom thong bao")),
    # ── Huyền học ──
    _e("lich", LOOKUP, A, B, "huyen_hoc", "/lich <câu hỏi>",
       "Âm lịch, ngày tốt, ngày lễ, tuổi, sao hạn (không AI)", "ngày tốt tháng này",
       "code:lunar_chat + tool:lunar_calendar + cmd:cmd_lich",
       ("am lich", "ngay tot", "ngay le", "sao han", "xem ngay", "xem tuoi", "hoang dao")),
    _e("tsh", LOOKUP, A, B, "huyen_hoc", "/tsh <ngày sinh> [họ tên]",
       "Thần số học (không AI)", "thần số học ngày sinh 15/3/1990", "cmd:cmd_tsh",
       ("than so hoc", "so chu dao")),
    _e("tsh_chitiet", LOOKUP, A, B, "huyen_hoc", "/tsh_chitiet <như /tsh>",
       "Thần số học + AI diễn giải", "luận giải thần số học 15/3/1990 chi tiết", "cmd:cmd_tsh",
       ("than so hoc chi tiet",), aliases=("ths_chitiet",)),
    _e("tuvi", LOOKUP, A, B, "huyen_hoc", "/tuvi <ngày sinh> <giờ> <nam|nữ>",
       "An lá số tử vi (không AI)", "xem tử vi 15/3/1990 9h30 nam", "cmd:cmd_tuvi",
       ("tu vi", "la so")),
    _e("tuvi_chitiet", LOOKUP, A, B, "huyen_hoc", "/tuvi_chitiet <như /tuvi>",
       "Lá số tử vi + AI luận giải", "luận giải tử vi chi tiết 15/3/1990 9h30 nam",
       "cmd:cmd_tuvi", ("luan giai tu vi", "tu vi chi tiet")),
    _e("kymon", LOOKUP, A, B, "huyen_hoc", "/kymon [ngày] [giờ]", "Bàn Kỳ Môn Độn Giáp (không AI)",
       "lập bàn kỳ môn lúc này", "cmd:cmd_kymon", ("ky mon",)),
    _e("kymon_chitiet", LOOKUP, A, B, "huyen_hoc", "/kymon_chitiet [thời điểm] <việc cần xem>",
       "Bàn Kỳ Môn + AI luận giải 1 việc", "kỳ môn xem 9h sáng mai ký hợp đồng có thuận không",
       "cmd:cmd_kymon", ("ky mon chi tiet",)),
    # ── Nhắc việc, việc nền ──
    _e("nhac", WRITE, A, B, "nhac_viec", "/nhac [giờ việc] · huy|hoan <số>",
       "Nhắc việc: xem, đặt, huỷ, hoãn", "nhắc anh 8h tối mai gọi mẹ",
       "tool:set_reminder + tool:list_reminders + tool:cancel_reminder + code:reminder_service",
       ("nhac viec", "dat nhac", "hen gio", "nhac lap", "huy nhac", "hoan nhac")),
    _e("ngaynho", WRITE, A, B, "nhac_viec", "/ngaynho [mô tả] · xoa <số>",
       "Ngày nhớ: giỗ, sinh nhật, kỷ niệm", "nhớ giỗ ông nội 12/3 âm, nhắc trước 3 ngày",
       "tool:save_special_date + tool:list_special_dates", ("ngay nho", "ngay gio", "sinh nhat",
       "ky niem"), aliases=("ngay_nho",)),
    _e("viec", WRITE, A, B, "nhac_viec", "/viec <yêu cầu> · /viec [xem|huy <số>]",
       "Giao việc chạy nền, xem/huỷ việc", "việc nền của anh tới đâu rồi",
       "cmd:cmd_viec + suggest", ("viec nen", "chay nen", "giao viec")),
    _e("lichtacvu", WRITE, S, B, "nhac_viec", "/lichtacvu [câu có mốc lặp] · huy|tam dung|tiep <số>",
       "Tác vụ định kỳ: em tự chạy yêu cầu theo lịch",
       "mỗi sáng thứ 2 8h tóm tắt tin về VNM cho anh", "code:routines + cmd:cmd_lichtacvu",
       ("tac vu dinh ky", "lich tac vu", "dinh ky")),
    # ── Chi tiêu, ăn uống ──
    _e("chitieu", WRITE, A, B, "chi_tieu", "/chitieu [kỳ|ds|<khoản>]",
       "Chi tiêu: tổng theo danh mục, khoản gần nhất, ghi khoản", "tháng này anh tiêu bao nhiêu",
       "code:expense_service + tool:add_expense + tool:expense_summary + cmd:cmd_chitieu_ds",
       ("chi tieu", "tieu bao nhieu", "ghi chi tieu", "khoan chi")),
    _e("chitieu_xoa", WRITE, A, B, "chi_tieu", "/chitieu_xoa <số>", "Xoá 1 khoản chi",
       nl="suggest", kw=("xoa khoan chi", "xoa chi tieu")),
    _e("an", WRITE, A, B, "chi_tieu", "/an [món] · /an tuan · /an xoa <số>",
       "Ghi bữa ăn, xem kcal ước lượng", "hôm nay anh ăn bao nhiêu calo",
       "code:nutrition_service + cmd:cmd_an", ("an uong", "calo", "kcal", "ghi bua an")),
    # ── Cổ phiếu ──
    _e("danhmuc", PERSONAL, A, B, "co_phieu", "/danhmuc", "Xem danh mục, lãi/lỗ, cảnh báo",
       "xem danh mục của anh", "code:portfolio_service + tool:get_portfolio",
       ("danh muc", "lai lo", "co phieu dang giu")),
    _e("themcp", WRITE, A, B, "co_phieu", "/themcp <MÃ> <SL> <GIÁ> [STOP] [TARGET]",
       "Thêm giao dịch mua", "đã mua FPT 100 giá 120", "code:portfolio_service",
       ("them co phieu", "ghi mua")),
    _e("capnhatcp", WRITE, A, B, "co_phieu", "/capnhatcp <MÃ> <SL> <GIÁ> [STOP] [TARGET]",
       "Ghi đè vị thế", nl="suggest", kw=("cap nhat vi the", "sua gia von")),
    _e("bancp", WRITE, A, B, "co_phieu", "/bancp <MÃ> [SL|hết] [GIÁ]", "Bán bớt hoặc bán hết",
       "đã bán hết HPG giá 27", "code:portfolio_service", ("ban co phieu", "ghi ban")),
    _e("xoacp", WRITE, A, B, "co_phieu", "/xoacp <MÃ>", "Xoá mã khỏi danh mục", nl="suggest",
       kw=("xoa ma", "xoa khoi danh muc")),
    _e("lichsucp", PERSONAL, A, B, "co_phieu", "/lichsucp [MÃ]", "Sổ giao dịch cổ phiếu",
       "sổ giao dịch FPT", "cmd:cmd_lichsucp", ("so giao dich", "lich su giao dich")),
    _e("laicp", PERSONAL, A, B, "co_phieu", "/laicp [THÁNG]", "Lãi/lỗ đã chốt theo tháng",
       "tháng 10 anh chốt lãi bao nhiêu", "cmd:cmd_laicp", ("lai da chot", "lai lo da chot")),
    _e("giaban", WRITE, A, B, "co_phieu", "/giaban <MÃ> <GIÁ>", "Bổ sung giá cho lệnh bán",
       nl="suggest", kw=("bo sung gia ban",)),
    _e("theodoi", PERSONAL, A, B, "co_phieu", "/theodoi [MÃ...]",
       "Danh sách theo dõi và ngưỡng giá", "danh sách mã anh đang theo dõi",
       "cmd:cmd_theodoi + suggest", ("theo doi", "watchlist")),
    _e("botheodoi", WRITE, A, B, "co_phieu", "/botheodoi <MÃ>", "Bỏ theo dõi 1 mã", nl="suggest",
       kw=("bo theo doi",)),
    _e("canhbaogia", WRITE, A, B, "co_phieu", "/canhbaogia <MÃ> [về|lên] <GIÁ>",
       "Báo khi giá chạm ngưỡng", "báo anh khi HPG về 25", "code:portfolio_service",
       ("canh bao gia", "bao khi gia", "nguong gia")),
    _e("xoacanhbao", WRITE, A, B, "co_phieu", "/xoacanhbao <MÃ> [GIÁ]", "Xoá ngưỡng giá",
       nl="suggest", kw=("xoa canh bao",)),
    # ── Trí nhớ, dữ liệu ──
    _e("memory", PERSONAL, A, B, "tri_nho", "/memory [on|off]",
       "Xem trí nhớ dài hạn (đánh số), bật/tắt", "em đang nhớ gì về anh",
       "cmd:cmd_memory + set:set_memory", ("tri nho", "em nho gi")),
    _e("nho", WRITE, A, B, "tri_nho", "/nho <điều cần nhớ>", "Nhờ em nhớ (ghim) 1 điều",
       "nhớ giúp anh là anh dị ứng tôm", "tool:remember_fact", ("nho giup", "ghi nho")),
    _e("quen", WRITE, A, B, "tri_nho", "/quen <số|từ khoá>", "Quên 1 mục trí nhớ", nl="suggest",
       kw=("quen muc", "quen di")),
    _e("ghim", WRITE, A, B, "tri_nho", "/ghim <số>", "Ghim 1 mục trí nhớ", nl="suggest",
       kw=("ghim muc",)),
    _e("boghim", WRITE, A, B, "tri_nho", "/boghim <số>", "Bỏ ghim 1 mục trí nhớ", nl="suggest",
       kw=("bo ghim",)),
    _e("forget", DANGER, A, B, "tri_nho", "/forget", "Xoá hết trí nhớ dài hạn", nl="suggest",
       kw=("xoa het tri nho", "quen het")),
    _e("notes", PERSONAL, A, B, "tri_nho", "/notes [từ khoá]", "Xem/tìm ghi chú",
       "em ghi chú gì cho anh rồi", "tool:list_notes + tool:save_note", ("ghi chu", "note")),
    _e("xoaghichu", WRITE, A, B, "tri_nho", "/xoaghichu <số>", "Xoá 1 ghi chú", nl="suggest",
       kw=("xoa ghi chu",)),
    _e("history", PERSONAL, A, B, "tri_nho", "/history", "Xem 10 lượt chat gần nhất",
       "xem lịch sử chat gần đây", "cmd:cmd_history + tool:search_history",
       ("lich su chat", "lich su tro chuyen", "hom truoc em noi gi")),
    _e("xuatdulieu", PERSONAL, A, B, "tri_nho", "/xuatdulieu", "Tải/xem dữ liệu em đang lưu",
       nl="suggest", kw=("xuat du lieu", "tai du lieu")),
    _e("xoadulieu", DANGER, A, B, "tri_nho", "/xoadulieu", "Xoá trí nhớ, ghi chú, lịch sử chat",
       nl="suggest", kw=("xoa du lieu", "xoa het du lieu")),
    _e("reset", DANGER, A, B, "tri_nho", "/reset", "Xoá ngữ cảnh chat", nl="suggest",
       kw=("xoa ngu canh", "lam moi cuoc tro chuyen")),
    _e("hoithan", WRITE, A, B, "cai_dat", "/hoithan on|off",
       "Em chủ động hỏi thăm việc mình kể", "tắt hỏi thăm chủ động đi em",
       "set:set_hoithan + code:proactive_service", ("hoi than", "hoi tham")),
    _e("hoc", WRITE, A, B, "tri_nho", "/hoc [ok|bo]", "Duyệt cách trả lời em tự rút ra",
       '"ghi nhớ"/"bỏ" ngay sau câu em hỏi', "code:style_approval", ("cach tra loi",)),
    # ── Tiện ích ──
    _e("gia", LOOKUP, A, B, "tien_ich", "/gia <sản phẩm>", "Tìm và so sánh giá sản phẩm",
       "tìm giá iPhone 16 Pro", "cmd:cmd_gia", ("tim gia", "so sanh gia", "gia san pham")),
    _e("dich", LOOKUP, A, B, "tien_ich", "/dich [ja>vi|vi>ja] <nội dung>", "Dịch chat Nhật-Việt",
       "dịch giúp anh: お世話になります", "cmd:cmd_dich", ("dich", "tieng nhat")),
    _e("prompt", LOOKUP, A, B, "tien_ich", "/prompt <mô tả>", "Viết prompt tạo ảnh",
       "viết prompt tạo ảnh cô gái áo dài", "cmd:cmd_prompt", ("viet prompt", "prompt")),
    _e("anh", LOOKUP, A, B, "tien_ich", "/anh <mô tả> · /anh on|off (admin)",
       "Tạo ảnh thật (Agnes AI)", "tạo ảnh con mèo đội nón lá", "cmd:cmd_anh + suggest",
       ("tao anh", "ve anh", "ve hinh")),
    _e("noi", WRITE, A, B, "cai_dat", "/noi on|off|auto · /noi giong <tên>", "Trả lời kèm giọng nói",
       "bật trả lời bằng giọng nói", "set:set_noi", ("giong noi", "tin thoai", "doc cho nghe")),
    # ── Quản trị bot ──
    _e("status", LOOKUP, S, B, "admin", "/status", "Trạng thái provider",
       "provider đang chạy cái nào", "cmd:cmd_status", ("provider", "trang thai bot")),
    _e("loi", LOOKUP, O, B, "admin", "/loi", "Lỗi gom nhóm hôm nay",
       "hôm nay bot có lỗi gì không", "cmd:cmd_loi", ("loi hom nay", "xem loi", "bao loi")),
    _e("thongke", LOOKUP, S, B, "admin", "/thongke [Nd|Ngiờ]", "Thống kê lượt gọi theo user/model",
       "thống kê lượt gọi 7 ngày", "cmd:cmd_thongke", ("thong ke", "luot goi")),
    _e("danhgia", LOOKUP, O, B, "admin", "/danhgia [Nd]", "Số 👍/👎 theo provider",
       "thống kê đánh giá câu trả lời", "cmd:cmd_danhgia", ("danh gia", "like dislike")),
    _e("xuateval", ADMIN, O, TG, "admin", "/xuateval [Nd]", "Xuất câu bị 👎 thành case đánh giá",
       nl="suggest", kw=("xuat eval",)),
    _e("model", ADMIN, S, B, "admin", "/model [tên|auto]", "Xem/đổi model chat", nl="suggest",
       kw=("doi model", "model nao")),
    _e("router9", DANGER, S, B, "admin", "/router9 on|off", "Bật/tắt 9Router", nl="suggest",
       kw=("9router", "router9")),
    _e("userouter9", ADMIN, S, B, "admin", "/userouter9", "Ép thử lại 9Router", nl="suggest",
       kw=("thu lai 9router",)),
    _e("tavily", WRITE, S, B, "admin", "/tavily on|off", "Bật/tắt tra web Tavily",
       "tắt Tavily đi em", "set:set_tavily", ("tavily", "tra web")),
    _e("agent", LOOKUP, S, B, "admin", "/agent <câu hỏi>", "Agent tự tra cứu nhiều bước",
       nl="-", kw=("agent",)),
    _e("rag", LOOKUP, S, B, "admin", "/rag <câu hỏi>", "Tra kho kiến thức rag/",
       "trong tài liệu của anh nói gì về ...", "tool:rag_search", ("kho kien thuc", "tai lieu")),
    _e("ragxuly", ADMIN, S, B, "admin", "/ragxuly <file>", "Dọn file md OCR trong rag/",
       nl="suggest", kw=("don file", "ragxuly")),
    _e("bangtinsang", ADMIN, S, B, "admin", "/bangtinsang", "Gửi ngay Bản tin sáng tới Zalo + Zoom",
       nl="suggest", kw=("gui ban tin", "phat ban tin"), aliases=("bantinsang",)),
    _e("tintuc", LOOKUP, S, B, "admin", "/tintuc", "Đọc Điểm tin mới ngay tại kênh đang gọi",
       "điểm tin sáng nay có gì", "cmd:cmd_tintuc", ("diem tin", "tin tuc")),
    # ── Nhóm Zalo, Facebook ──
    _e("nhom", LOOKUP, S, B, "nhom", "/nhom", "Danh sách nhóm Zalo đang theo dõi",
       "đang theo dõi những nhóm Zalo nào", "cmd:cmd_nhom", ("nhom zalo", "danh sach nhom")),
    _e("themnhom", ADMIN, S, B, "nhom", "/themnhom <group_id> <tên>", "Thêm nhóm Zalo theo dõi",
       nl="suggest", kw=("them nhom",)),
    _e("xoanhom", ADMIN, S, B, "nhom", "/xoanhom <group_id|tên>", "Ngừng theo dõi 1 nhóm",
       nl="suggest", kw=("xoa nhom", "bo nhom")),
    _e("tongket", LOOKUP, S, B, "nhom", "/tongket <tên> [24h|7d|homnay|homqua] · on|off",
       "Tổng kết nhóm Zalo (AI)", "tóm tắt nhóm Gia đình hôm nay", "cmd:cmd_tongket + suggest",
       ("tong ket nhom", "tom tat nhom")),
    _e("dangnoi", LOOKUP, S, B, "nhom", "/dangnoi <tên>", "Nguyên văn thảo luận hôm nay",
       "nhóm Gia đình đang nói gì", "cmd:cmd_dangnoi", ("dang noi gi", "thao luan")),
    *(
        _e(f"fb_{n}", DANGER if n == "reset" else ADMIN, S, B, "nhom", u, d, nl="suggest",
           kw=("facebook", "bai fb", "fanpage", *k))
        for n, u, d, k in (
            ("nhom", "/fb_nhom", "Xem nhóm nguồn Facebook", ("nhom nguon",)),
            ("pages", "/fb_pages", "Xem các Facebook Page đã cấu hình", ("facebook page",)),
            ("market", "/fb_market <stock|news> [dang]", "Xem thử/đăng bài Page chứng khoán",
             ("bai chung khoan", "dang bai page")),
            ("themnhom", "/fb_themnhom <group_id> <tên>", "Thêm nhóm nguồn Facebook",
             ("them nhom nguon",)),
            ("xoanhom", "/fb_xoanhom <group_id|tên>", "Bỏ nhóm nguồn Facebook",
             ("bo nhom nguon", "xoa nhom nguon")),
            ("xem", "/fb_xem <post_id>", "Xem bài chờ duyệt", ("bai cho duyet", "xem bai")),
            ("sua", "/fb_sua <post_id> <nội dung>", "Sửa bài chờ duyệt", ("sua bai",)),
            ("link", "/fb_link <post_id> <url>", "Nhập link affiliate cho bài chờ",
             ("link affiliate", "nhap link")),
            ("ok", "/fb_ok <post_id>", "Duyệt và đăng Facebook",
             ("duyet bai", "duyet va dang", "dang facebook", "dang bai")),
            ("check", "/fb_check <post_id>", "Kiểm tra bài đã công khai", ("kiem tra bai",)),
            ("reconcile", "/fb_reconcile", "Đối soát lượt đăng bị gián đoạn", ("doi soat",)),
            ("boqua", "/fb_boqua <post_id>", "Bỏ bài chờ", ("bo qua bai", "bo bai")),
            ("reset", "/fb_reset xacnhan", "Xoá bài Facebook đã lưu", ("xoa bai facebook",)),
            ("boloc", "/fb_boloc [reset]", "Thống kê bộ lọc Zalo → Facebook", ("bo loc",)),
            ("loclai", "/fb_loclai", "Lọc lại các bài đang chờ", ("loc lai",)),
        )
    ),
    # ── Quyền Zalo/Zoom (chỉ Telegram của chủ bot) ──
    _e("zalo", ADMIN, O, TG, "quyen", "/zalo", "Đăng nhập (QR) / xem trạng thái Zalo B",
       nl="suggest", kw=("dang nhap zalo",)),
    _e("zalologout", ADMIN, O, TG, "quyen", "/zalologout", "Đăng xuất Zalo B", nl="suggest",
       kw=("dang xuat zalo",)),
    _e("zalopair", ADMIN, O, TG, "quyen", "/zalopair <id> [tên] [xưng hô]",
       "Cấp quyền thành viên Zalo", nl="suggest", kw=("cap quyen", "them thanh vien")),
    _e("zaloadmin", ADMIN, O, TG, "quyen", "/zaloadmin <id> [tên] [xưng hô]",
       "Cấp quyền admin Zalo", nl="suggest", kw=("cap admin", "len admin")),
    _e("zalohaquyen", ADMIN, O, TG, "quyen", "/zalohaquyen <id>", "Hạ admin về thành viên",
       nl="suggest", kw=("ha quyen",)),
    _e("zalokhoa", ADMIN, O, TG, "quyen", "/zalokhoa <id>", "Khoá 1 user Zalo", nl="suggest",
       kw=("khoa zalo", "khoa user")),
    _e("zalomokhoa", ADMIN, O, TG, "quyen", "/zalomokhoa <id>", "Mở khoá 1 user Zalo",
       nl="suggest", kw=("mo khoa",)),
    _e("zaloxoa", DANGER, O, TG, "quyen", "/zaloxoa <id>", "Xoá pairing kèm dữ liệu 1 user Zalo",
       nl="suggest", kw=("xoa user zalo", "xoa nguoi dung")),
    _e("zalodanhsach", LOOKUP, O, TG, "quyen", "/zalodanhsach", "Danh sách user Zalo",
       nl="suggest", kw=("danh sach zalo", "user zalo")),
    _e("zoompair", ADMIN, O, TG, "quyen", "/zoompair <jid> [tên]", "Cấp quyền 1 jid Zoom",
       nl="suggest", kw=("pair zoom",)),
    _e("zoomxoa", DANGER, O, TG, "quyen", "/zoomxoa", "Gỡ pairing Zoom", nl="suggest",
       kw=("go zoom",)),
    _e("zoomstatus", LOOKUP, O, TG, "quyen", "/zoomstatus", "Xem jid Zoom đã pair", nl="suggest",
       kw=("jid zoom",)),
)
# fmt: on

_BY_NAME: dict[str, Entry] = {}
for _entry in ENTRIES:
    _BY_NAME[_entry.name] = _entry
    for _alias in _entry.aliases:
        _BY_NAME.setdefault(_alias, _entry)


def get(name: str) -> Entry | None:
    """Entry theo tên lệnh ("/tuvi", "tuvi", "/ths_chitiet" đều được)."""
    return _BY_NAME.get((name or "").strip().lstrip("/").lower().split("@", 1)[0])


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


def search(query: str, role: str, channel: str, limit: int = 3) -> list[Entry]:
    """Lệnh khớp câu hỏi nhất: ưu tiên cụm từ khoá trùng nguyên cụm (dài hơn điểm
    cao hơn), rồi tới từ trong tên chủ đề/mô tả. Không khớp cụm nào -> []."""
    q = " " + " ".join(re.findall(r"[a-z0-9]+", fold(query))) + " "
    raw = (query or "").lower()
    scored: list[tuple[int, int, Entry]] = []
    for index, entry in enumerate(entries_for(role, channel)):
        score = 0
        for phrase in entry.keywords:
            if f" {phrase} " in q:
                score = max(score, 10 * len(phrase.split()))
        # Tên lệnh chỉ tính khi gõ kèm "/" ("/xoso là gì"): tên trùng chữ thường
        # ("anh", "an", "gia", "nho", "lich") không được kéo theo lệnh.
        if re.search(rf"/{entry.name}\b", raw):
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
