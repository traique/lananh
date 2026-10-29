"""Web LCTA: trang chủ, /lich (+ /hoi, /doi-ngay, /ngay-le, /ngay-le/<lễ>, /ngay-tot),
/tuoi (+ /tuoi/<năm sinh>), /tu-vi, /than-so-hoc (form -> trang kết quả riêng), /ky-mon,
/thoi-tiet, /xo-so, /gia-vang, /gia-xang và JSON API /api/v1.

Xem nội dung KHÔNG cần đăng nhập: lịch, tử vi, thần số học tính bằng code
(services/site_data.py), không gọi AI, không đụng DB. /thoi-tiet tra Open-Meteo
qua services/weather.py (cache có trần), giới hạn số lượt tra song song và số lượt tra
địa điểm mới theo từng IP. /xo-so đọc
feed xskt.com.vn qua services/lottery_lookup.py (cache có trần theo đài, dò vé bằng
code). /gia-vang, /gia-xang đọc bảng giá qua services/price_lookup.py (dùng lại cache
1 bản/nguồn của vn_market_data + fuel_prices). Trang render sẵn ở server nên chạy được
cả khi tắt JavaScript.

Cần đăng nhập (tài khoản admin, chung phiên với /admin) cho việc tốn quota hoặc
đổi dữ liệu chung: nút "AI luận giải" ở kết quả tử vi/thần số học/kỳ môn, thêm/xoá địa
điểm thời tiết. Khách chỉ thấy link nhỏ "Chủ trang đăng nhập" ở chân trang; đăng nhập
rồi thanh bên mới hiện nút "Quản trị".

SEO/chia sẻ link: mọi trang có canonical + thẻ og:/twitter:, /robots.txt, /sitemap.xml,
/llms.txt; trình duyệt gặp đường dẫn không có nhận trang 404 HTML (web.py gọi
``not_found``). Nhập sai (URL gõ bừa) -> noindex; nguồn ngoài lỗi -> 503 + Retry-After.

JSON API (chỉ thêm trường mới, không đổi/bỏ trường cũ):
  GET  /api/v1/calendar/days/{YYYY-MM-DD}
  GET  /api/v1/calendar/months/{YYYY-MM}
  GET  /api/v1/calendar/conversions?lunarDay=&lunarMonth=&lunarYear=&isLeapMonth=true|false
  GET  /api/v1/calendar/answers?q=
  GET  /api/v1/lottery?province=mn|mt|mb|<khoá đài, vd tay-ninh>&date=YYYY-MM-DD&ticket=
                                    (bỏ trống province = miền Nam, bỏ trống date = kỳ mới nhất;
                                     nguồn lỗi -> 503 SOURCE_UNAVAILABLE)
  GET  /api/v1/gold                 (vang.today, lỗi thì bảng SJC với fallback=true; kèm
                                     quy đổi XAU/USD theo tỷ giá Vietcombank)
  GET  /api/v1/fuel                 (Petrolimex vùng 1/vùng 2 + PVOIL, giaxanghomnay.com)
  GET  /api/v1/fx                   (tỷ giá Vietcombank các ngoại tệ chính)
                                    (3 API giá: nguồn lỗi -> 503 SOURCE_UNAVAILABLE)
  POST /api/v1/numerology/readings  {"birthDate": "YYYY-MM-DD"|"DD/MM/YYYY", "name"?: str}
  POST /api/v1/tuvi/charts          {"birthDate": "YYYY-MM-DD"|"DD/MM/YYYY",
                                     "calendar": "solar"|"lunar", "isLeapMonth"?: bool (chỉ lunar),
                                     "hourBranch": 0-11, "gender": "male"|"female",
                                     "isEarlyTy"?: bool (sinh 23h-24h, an theo ngày hôm sau)}
  POST /api/v1/qimen/charts         {"dateTime"?: "YYYY-MM-DDTHH:MM[+07:00]" (bỏ trống = lúc này;
                                     không múi giờ = giờ Việt Nam)}
Ngày sinh/họ tên đi bằng POST để không nằm trong URL và log truy cập.
Lỗi nhập liệu: HTTP 422 + {"error": {"code": "...", "message": "..."}}; 404/405 dưới
/api/v1 cũng cùng dạng {"error"} (web.py); body quá 1 MiB bị middleware chặn 413.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import datetime as dt
import functools
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode
from zoneinfo import ZoneInfo

import yaml
from fastapi import APIRouter, Request
from fastapi.routing import APIRoute
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

import web_admin
from core import config
from services import (
    calendar_query,
    can_chi,
    concurrency,
    divination_commands,
    kymon,
    lottery_lookup,
    lottery_provinces,
    lunar,
    numerology,
    price_lookup,
    site_data,
    tuvi,
    vn_market_data,
    weather,
    weather_places,
)
from services.site_data import InputError

_ROOT = Path(__file__).resolve().parent
_STATIC = _ROOT / "static"
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_STATIC_FILES = {
    "site.css": "text/css; charset=utf-8",
    "logo-light.webp": "image/webp",
    "logo-dark.webp": "image/webp",
    "favicon.png": "image/png",
    "og-image.png": "image/png",
    "icon.png": "image/png",
}
# Trang công khai cho robots.txt / sitemap.xml (trang có form POST vẫn có bản GET).
_SITEMAP_PATHS = (
    "/",
    "/lich",
    "/lich/ngay-tot",
    "/lich/hoi",
    "/lich/doi-ngay",
    "/lich/ngay-le",
    "/thoi-tiet",
    "/xo-so",
    "/gia-vang",
    "/gia-xang",
    "/tuoi",
    "/tu-vi",
    "/ky-mon",
    "/than-so-hoc",
)
# Tham số làm nên nội dung trang - link canonical/og:url chỉ giữ các tham số này (bỏ
# fbclid/utm_* do Facebook/Zalo gắn, mã thông báo tạm ?tb=, số vé dò).
_LOTTERY_SHORT = {"mn": "XSMN", "mt": "XSMT", "mb": "XSMB"}
# Trang đổi số liệu mỗi ngày (lastmod hôm nay trong sitemap).
_DAILY_PATHS = frozenset({"/", "/lich", "/thoi-tiet", "/xo-so", "/gia-vang", "/gia-xang"})
_CANONICAL_PARAMS = frozenset({"thang", "ngay", "q", "am", "nhuan", "noi", "dai", "gio", "viec"})
# Health check (Render, UptimeRobot...) chỉ cần mã 200: GET / trả JSON nhẹ cho các client
# này; mọi client khác (trình duyệt, trình đọc link Facebook/Zalo gửi Accept: */*)
# nhận trang chủ HTML có thẻ og:.
_HEALTH_AGENTS = re.compile(r"\bRender/|UptimeRobot|kube-probe|ELB-HealthChecker|GoogleHC")
# Thanh bên: (nhóm, [(đường dẫn, nhãn, biểu tượng)]); biểu tượng là lớp .i-* trong site.css.
_NAV = [
    ("", [("/", "Hôm nay", "home")]),
    (
        "Lịch",
        [
            ("/lich", "Lịch tháng", "calendar"),
            ("/lich/ngay-tot", "Ngày tốt", "star"),
            ("/lich/hoi", "Hỏi lịch", "ask"),
            ("/lich/doi-ngay", "Đổi ngày âm", "swap"),
            ("/lich/ngay-le", "Ngày lễ", "flag"),
        ],
    ),
    (
        "Tra cứu",
        [
            ("/thoi-tiet", "Thời tiết", "cloud"),
            ("/xo-so", "Xổ số", "ticket"),
            ("/gia-vang", "Giá vàng", "gold"),
            ("/gia-xang", "Giá xăng", "fuel"),
            ("/tuoi", "Tuổi, sao hạn", "user"),
            ("/tu-vi", "Lá số tử vi", "chart"),
            ("/ky-mon", "Kỳ Môn Độn Giáp", "compass"),
            ("/than-so-hoc", "Thần số học", "number"),
        ],
    ),
]
# 12 giờ địa chi theo thứ tự trong ngày (Tý vắt qua nửa đêm).
_HOURS = [
    ("Tý", "23-1h"),
    ("Sửu", "1-3h"),
    ("Dần", "3-5h"),
    ("Mão", "5-7h"),
    ("Thìn", "7-9h"),
    ("Tỵ", "9-11h"),
    ("Ngọ", "11-13h"),
    ("Mùi", "13-15h"),
    ("Thân", "15-17h"),
    ("Dậu", "17-19h"),
    ("Tuất", "19-21h"),
    ("Hợi", "21-23h"),
]
_ASK_EXAMPLES = [
    "rằm tháng 8",
    "còn mấy ngày nữa Tết",
    "ngày tốt tháng này",
    "xem ngày động thổ",
    "tuổi 15/3/1990",
    "sao hạn 1990 nam năm 2027",
    "1990 và 1995 có hợp không",
]
# Thông báo sau khi thêm/xoá địa điểm: chỉ nhận mã cố định, không phản chiếu chữ từ URL.
_PLACE_NOTICES = {
    "da-them": ("ok", "Đã thêm địa điểm."),
    "da-xoa": ("ok", "Đã xoá địa điểm."),
    "ten": (
        "error",
        f"Tên địa điểm 1-{weather_places.MAX_NAME_LEN} ký tự, không chứa ký tự đặc biệt.",
    ),
    "day": ("error", f"Tối đa {weather_places.MAX_PLACES} địa điểm, xoá bớt trước khi thêm."),
    "luu": ("error", "Không lưu được, thử lại sau."),
    "da-luu-vi-tri": ("ok", "Đã lưu vị trí hiện tại. Lần sau mở trang Thời tiết sẽ xem ở đây."),
    "da-xoa-vi-tri": ("ok", "Đã xoá vị trí đã lưu."),
    "vi-tri-loi": ("error", "Không đọc được vị trí, thử lại."),
}
# Vị trí hiện tại của khách (nút "Lấy vị trí hiện tại", navigator.geolocation): lưu TRONG
# TRÌNH DUYỆT bằng cookie chỉ gửi kèm trang /thoi-tiet, toạ độ làm tròn 2 số lẻ (~1 km).
# Máy chủ không lưu (không DB, không RAM ngoài cache dự báo có trần), không cần đăng nhập.
# Tên nơi tra 1 lần lúc lưu (Nominatim, weather.reverse_geocode) rồi cất luôn trong cookie
# - xem lại không gọi Nominatim nữa.
_MY_PLACE_COOKIE = "lcta_vitri"
_MY_PLACE_PATH = "/thoi-tiet"
_MY_PLACE_MAX_AGE = 365 * 86400
_MY_PLACE_NAME_MAX = 80
# Tên nơi là phần phụ: quá hạn chót (kể cả chờ lượt 1 giây của Nominatim) thì lưu toạ độ.
_REVERSE_TIMEOUT_SEC = 6
# "vĩ độ_kinh độ" ("_" không phải bọc ngoặc kép như ","); form gửi ",".
_COORDS_RE = re.compile(r"\s*(-?\d{1,3}(?:\.\d{1,8})?)\s*[,_]\s*(-?\d{1,3}(?:\.\d{1,8})?)\s*")
# Cookie: "vĩ_kinh" hoặc "vĩ_kinh_<tên nơi đã percent-encode>" (80 ký tự tiếng Việt, mỗi
# ký tự tối đa 3 byte UTF-8 = 9 ký tự "%XX" -> tên dài nhất 720 ký tự).
_MY_PLACE_RE = re.compile(r"([^_]{1,20}_[^_]{1,20})(?:_(.{1,900}))?")
_WEATHER_DAYS = 7
_WEATHER_TIMEOUT_SEC = 15
# Trang công khai: chặn số lượt gọi Open-Meteo song song để người lạ spam
# địa danh không chiếm hết kết nối/RAM của 1 worker (webhook Telegram/Zalo).
_weather_slots = asyncio.Semaphore(3)
# Địa điểm LẠ (không có trong danh sách hay xem, chưa có trong cache toạ độ) tốn 3-4
# request Open-Meteo mỗi lượt (geocoding + dự báo) và dùng chung hạn mức miễn phí với
# tool thời tiết của chat. Giới hạn theo từng IP (một người gõ liên tục không khoá được
# người khác) cộng một trần chung toàn site trong 10 phút. Bảng IP có trần.
_NEW_PLACE_PER_IP, _NEW_PLACE_LIMIT, _NEW_PLACE_WINDOW_SEC = 10, 60, 600
_NEW_PLACE_MAX_IPS = 512
_new_place_times: collections.deque[float] = collections.deque()
_new_place_by_ip: collections.OrderedDict[str, collections.deque[float]] = collections.OrderedDict()
_MAX_JSON_BYTES = 4096
# Xổ số: mỗi đài/miền 1 bản cache 5 phút (vn_market_data), thêm trần lượt tải song song
# để người lạ đổi đài liên tục không chiếm hết kết nối của worker.
_lottery_slots = asyncio.Semaphore(3)
_LOTTERY_TIMEOUT_SEC = 12
# Giá vàng/xăng/tỷ giá: cache sẵn có (vàng, tỷ giá 10 phút; xăng 1 giờ), cùng trần lượt
# tải song song. Hạn chót 15 giây vì vàng có thể phải thử vang.today rồi mới tới SJC.
_price_slots = asyncio.Semaphore(3)
_PRICE_TIMEOUT_SEC = 15


# (đã đăng nhập, đường dẫn đang xem, gốc URL công khai) của request hiện tại - để thanh
# bên hiện nút Quản trị/Đăng xuất và trang có link canonical/og:url mà không phải truyền
# request qua mọi hàm.
_viewer: contextvars.ContextVar[tuple[bool, str, str]] = contextvars.ContextVar(
    "site_viewer", default=(False, "/", "")
)


def _current_path(request: Request) -> str:
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


def _base_url(request: Request) -> str:
    return config.SITE_BASE_URL or str(request.base_url).rstrip("/")


async def _set_viewer(request: Request) -> bool:
    logged_in = await web_admin.is_logged_in(request)
    # POST (kết quả form) không mở lại được bằng link: đăng nhập xong về trang form.
    path = _current_path(request) if request.method == "GET" else request.url.path
    _viewer.set((logged_in, path, _base_url(request)))
    return logged_in


def _canonical_path(path: str) -> str:
    """Đường dẫn đang xem chỉ giữ tham số nội dung (_CANONICAL_PARAMS) - cho canonical/og:url."""
    base, _, query = path.partition("?")
    pairs = [(k, v) for k, v in parse_qsl(query) if k in _CANONICAL_PARAMS]
    # %20 cho dấu cách (không "+"): trùng link nội bộ (bộ lọc urlencode) và sitemap
    return base + (f"?{urlencode(pairs, quote_via=quote)}" if pairs else "")


class _ViewerRoute(APIRoute):
    """Ghi nhận trạng thái đăng nhập trước khi render trang."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def with_viewer(request: Request) -> Response:
            await _set_viewer(request)
            return await handler(request)

        return with_viewer


router = APIRouter(include_in_schema=False, route_class=_ViewerRoute)
api_router = APIRouter(prefix="/api/v1")
static_router = APIRouter(include_in_schema=False)


# Câu trả lời Hỏi lịch là chữ của lệnh /lich (dùng chung với chat). Trên web: bỏ emoji đầu
# dòng, đổi dấu đánh dấu emoji/chấm giữa sang ký hiệu chữ, " - " thành dấu phẩy, dòng "• "
# thành danh sách thật; đoạn cách nhau bằng dòng trống.
_ANSWER_LEAD = re.compile(r"^(?:📅|🎉|✅|⚠️|ℹ️|❌)\s*")
_ANSWER_MARKS = (("⭐", "★"), ("🌕", "●"), ("·hđ", "(hđ)"), (" - ", ", "))


def _answer_html(text: str) -> Markup:
    html = []
    for chunk in re.split(r"\n\s*\n", str(text).strip()):
        lines: list[str] = []
        items: list[str] = []
        for raw in chunk.splitlines():
            line = _ANSWER_LEAD.sub("", raw.strip())
            for old, new in _ANSWER_MARKS:
                line = line.replace(old, new)
            if line.startswith("• "):
                if lines:
                    html.append(f"<p>{'<br>'.join(lines)}</p>")
                    lines = []
                items.append(f"<li>{escape(line[2:])}</li>")
                continue
            if items:
                html.append(f"<ul>{''.join(items)}</ul>")
                items = []
            if line:
                lines.append(str(escape(line)))
        if lines:
            html.append(f"<p>{'<br>'.join(lines)}</p>")
        if items:
            html.append(f"<ul>{''.join(items)}</ul>")
    return Markup("".join(html))


_FAQ_PATH = _ROOT / "assets" / "site_faq.yaml"
_FAQ_LINK = re.compile(r"\[([^\]]+)\]\((/[^)\s]*)\)")


@functools.lru_cache(maxsize=1)
def _faq() -> dict[str, list[dict]]:
    """Hỏi đáp cuối trang (assets/site_faq.yaml) theo đường dẫn trang gốc."""
    with _FAQ_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _faq_html(text: str) -> Markup:
    """Câu trả lời hỏi đáp: escape rồi đổi [chữ](/đường-dẫn) thành link nội bộ."""
    return Markup(_FAQ_LINK.sub(r'<a href="\2">\1</a>', str(escape(text))))


@functools.lru_cache(maxsize=1)
def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_ROOT / "templates" / "site")),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["vn_date"] = lambda iso: dt.date.fromisoformat(iso).strftime("%d/%m/%Y")
    # Số kiểu Việt Nam: 25.060 / 4.015,1; đồng -> triệu: 141700000 -> "141,7".
    env.filters["vnd"] = vn_market_data.vnd
    env.filters["trieu"] = vn_market_data.trieu
    env.filters["fx_rate"] = lambda v: vn_market_data.vnd(v, 0 if v is None or v >= 1000 else 2)
    env.filters["usd"] = lambda v: vn_market_data.vnd(v, 1 if v is not None and v % 1 else 0)
    # Ý nghĩa trong assets/*.yaml (dùng chung với chat) viết "nhãn - giải thích"; trên web
    # gạch nối có cách thành dấu hai chấm (hoặc phẩy), không để gạch làm dấu câu.
    env.filters["clause"] = lambda text, sep=": ": str(text).replace(" - ", sep)
    env.filters["answer_html"] = _answer_html
    env.filters["faq_html"] = _faq_html
    return env


@functools.lru_cache(maxsize=1)
def _assets() -> tuple[str, str]:
    """(phiên bản CSS, phiên bản ảnh) = hash nội dung, gắn vào URL dạng ?v=<hash>.
    CSS là file riêng cache 1 năm (immutable): chỉ lượt xem đầu tiên tải thêm 1
    request, các trang sau trình duyệt dùng bản đã lưu, không hỏi lại server - nên
    không đè lên --limit-concurrency của uvicorn (webhook Telegram/Zalo dùng chung)."""

    def digest(names: list[str]) -> str:
        data = b"".join((_STATIC / n).read_bytes() for n in names)
        return hashlib.sha256(data).hexdigest()[:10]

    return digest(["site.css"]), digest(sorted(n for n in _STATIC_FILES if n != "site.css"))


_NAV_LABELS = {href: label for _, items in _NAV for href, label, _ in items}


def _structured_data(
    title: str, description: str, active: str, url: str, base: str, modified: str, crumb: str = ""
) -> Markup:
    """JSON-LD cho máy tìm kiếm: trang chủ khai WebSite + tổ chức kèm logo; trang khác
    khai WebPage (kèm dateModified nếu biết giờ cập nhật số liệu) + đường dẫn mục
    (``crumb``: tên trang con dưới mục, vd một ngày lễ dưới "Ngày lễ")."""
    website = {"@type": "WebSite", "@id": f"{base}/#website", "url": f"{base}/", "name": "LCTA"}
    if active == "/":
        org = {
            "@type": "Organization",
            "@id": f"{base}/#organization",
            "name": "LCTA",
            "url": f"{base}/",
            "logo": f"{base}/static/icon.png",
        }
        graph = [
            {
                **website,
                "inLanguage": "vi",
                "description": description,
                "publisher": {"@id": org["@id"]},
            },
            org,
        ]
    else:
        page = {
            "@type": "WebPage",
            "url": url,
            "name": title,
            "description": description,
            "inLanguage": "vi",
            "isPartOf": website,
        }
        if modified:
            page["dateModified"] = modified
        graph = [page]
        if active in _NAV_LABELS:
            crumbs = [("Hôm nay", f"{base}/"), (_NAV_LABELS[active], f"{base}{active}")]
            if crumb:
                crumbs.append((crumb, url))
            graph.append(
                {
                    "@type": "BreadcrumbList",
                    "itemListElement": [
                        {"@type": "ListItem", "position": i, "name": n, "item": item}
                        for i, (n, item) in enumerate(crumbs, 1)
                    ],
                }
            )
    text = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)
    return Markup(text.replace("<", "\\u003c"))  # chữ từ nguồn ngoài không đóng được <script>


def _vn_stamp(text: str | None) -> str:
    """Giờ cập nhật dạng 07:30 27/10/2026 (giờ VN của nguồn giá) -> ISO 8601 cho
    dateModified; sai dạng thì chuỗi rỗng."""
    try:
        stamp = dt.datetime.strptime(str(text or "").strip(), "%H:%M %d/%m/%Y")
    except ValueError:
        return ""
    return stamp.replace(tzinfo=_VN_TZ).isoformat()


def _render(
    name: str,
    *,
    title: str,
    description: str,
    active: str,
    modified: str = "",
    canonical: str = "",
    crumb: str = "",
    **ctx,
) -> HTMLResponse:
    """``canonical``: đường dẫn chuẩn khi cùng nội dung mở được bằng nhiều kiểu URL;
    trống thì lấy đường dẫn đang xem (chỉ giữ tham số nội dung). ``crumb``: tên trang
    con trong đường dẫn mục của JSON-LD."""
    css_version, version = _assets()
    logged_in, path, base = _viewer.get()
    canonical = canonical or _canonical_path(path)
    page_url = f"{base}{canonical}" if base else ""
    # Hỏi đáp chỉ ở trang gốc (không tham số, vào chỉ mục): không lặp trên hàng nghìn
    # trang ngày/tháng/đài/địa điểm.
    faq = [] if ctx.get("noindex") or "?" in canonical else _faq().get(canonical, [])
    ld_json = (
        _structured_data(title, description, active, page_url, base, modified, crumb)
        if base and not ctx.get("noindex")
        else ""
    )
    html = (
        _env()
        .get_template(name)
        .render(
            title=title,
            description=description,
            active=active,
            nav=_NAV,
            css_v=css_version,
            v=version,
            logged_in=logged_in,
            # Link tuyệt đối cho canonical/og: (chưa biết gốc URL thì bỏ các thẻ này).
            page_url=page_url,
            site_url=base,
            ld_json=ld_json,
            faq=faq,
            site_verification=config.GOOGLE_SITE_VERIFICATION,
            **ctx,
        )
    )
    # Lỗi nhập liệu hiện ngay trên trang (role=alert) với HTTP 200: trang HTML
    # trả 4xx làm trình duyệt ghi lỗi console dù người dùng chỉ nhập sai. Trang lỗi
    # nhập liệu truyền noindex để URL gõ bừa không thành trang trùng trong chỉ mục.
    return HTMLResponse(html)


def _source_down(page: HTMLResponse) -> HTMLResponse:
    """Nguồn ngoài lỗi/chậm: 503 + Retry-After để máy tìm kiếm giữ bản đã lập chỉ mục và
    quay lại sau, không lấy trang báo lỗi thay vào. Người xem vẫn thấy trang có thông báo."""
    page.status_code = 503
    page.headers["Retry-After"] = "300"
    return page


def _render_lich(name: str, *, tab: str, title: str, description: str, **ctx) -> HTMLResponse:
    return _render(name, title=title, description=description, active=tab, **ctx)


def is_browser(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def wants_html(request: Request) -> bool:
    """GET /: trang chủ HTML cho mọi client trừ health check và client chỉ xin JSON."""
    if is_browser(request):
        return True
    if "application/json" in request.headers.get("accept", ""):
        return False
    return not _HEALTH_AGENTS.search(request.headers.get("user-agent", ""))


# ─── Trang ───────────────────────────────────────────────────────────────────


def _upcoming_holidays(today: dt.date, limit: int) -> list[dict]:
    upcoming = []
    for holiday in calendar_query.HOLIDAYS:
        day = calendar_query.holiday_entry_date(holiday, today)
        if day is not None:
            upcoming.append((day, holiday[0], holiday[1]))
    upcoming.sort()
    return [
        {
            "name": name,
            "slug": site_data.slug(name),
            "date": day,
            "weekday": site_data.day_info(day)["weekday"],
            "isLunar": is_lunar,
            "days": (day - today).days,
        }
        for day, name, is_lunar in upcoming[:limit]
    ]


async def home(request: Request) -> Response:
    """Trang chủ cho trình duyệt (web.py gọi khi Accept có text/html)."""
    await _set_viewer(request)
    return home_page()


def _hour_strip(lucky_hours: list[str], hour: int) -> list[dict]:
    """12 giờ địa chi của ngày: giờ hoàng đạo (từ luckyHours dạng "Dần (3h-5h)"), giờ
    đang là lúc này (giờ VN, 0-23), giờ đã qua, và giờ tốt cần chú ý (isFocus): giờ
    hiện tại nếu tốt, không thì giờ hoàng đạo kế tiếp trong ngày."""
    good = {h.split(" (")[0] for h in lucky_hours}
    now = (hour + 1) // 2 % 12
    focus = next((i for i in range(now, 12) if _HOURS[i][0] in good), None)
    return [
        {
            "name": name,
            "span": span,
            "isGood": name in good,
            "isNow": i == now,
            "isPast": i < now,
            "isFocus": i == focus,
        }
        for i, (name, span) in enumerate(_HOURS)
    ]


def home_page() -> HTMLResponse:
    now = site_data.now()
    today = now.date()
    info = site_data.day_info(today)
    # Ngày âm đổi ở đầu giờ Tý (23h): từ 23h dải giờ là của ngày hôm sau.
    hours_day = today + dt.timedelta(days=1) if now.hour == 23 else today
    lucky = (
        info["luckyHours"] if hours_day == today else site_data.day_info(hours_day)["luckyHours"]
    )
    hours = _hour_strip(lucky, now.hour)
    ld = info["lunar"]
    page = _render(
        "home.html",
        title=f"Lịch âm hôm nay {today:%d/%m/%Y}: giờ hoàng đạo, ngày tốt xấu",
        # Mô tả nêu luôn giờ hoàng đạo của ngày: Google lấy làm đoạn trích kết quả tìm kiếm.
        description=f"Hôm nay {today:%d/%m/%Y} dương lịch, tức ngày {ld['day']}/{ld['month']} âm lịch, "
        f"ngày {'hoàng đạo' if info['isHoangDao'] else 'hắc đạo'}. Giờ hoàng đạo: "
        f"{', '.join(h.split(' (')[0] for h in info['luckyHours'])}. Kèm ngày lễ sắp tới, giá "
        "vàng, thời tiết.",
        active="/",
        today=info,
        today_date=today,
        hours=hours,
        hours_tomorrow=hours_day != today,
        next_good=next((h for h in hours if h["isFocus"]), None),
        holidays=_upcoming_holidays(today, 3),
    )
    # Dải giờ đánh dấu "bây giờ": trình duyệt phải hỏi lại server thay vì dùng bản cũ.
    page.headers["Cache-Control"] = "no-cache"
    return page


def _parse_month(raw: str) -> tuple[int, int] | None:
    """Link của trang là 2026-10; người gõ tay hay viết 10/2026. Sai dạng -> None."""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})|(\d{1,2})[/-](\d{4})", raw)
    return (int(m.group(1) or m.group(4)), int(m.group(2) or m.group(3))) if m else None


def _month_error(raw: str) -> str:
    return (
        f"Không đọc được tháng “{raw}”. Bấm Tháng trước/Tháng sau, hoặc ghi tháng/năm như 10/2026."
    )


def _weeks(grid: dict) -> list[list[dict | None]]:
    weeks: list[list[dict | None]] = []
    week: list[dict | None] = [None] * grid["days"][0]["weekday"]
    for d in grid["days"]:
        week.append(d)
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        weeks.append(week + [None] * (7 - len(week)))
    return weeks


@router.get("/lich", response_class=HTMLResponse)
async def lich_page(request: Request) -> Response:
    params = request.query_params
    # Link cũ: /lich?q=... và /lich?am=... nay là trang riêng.
    if params.get("q"):
        return RedirectResponse(f"/lich/hoi?{urlencode({'q': params['q']})}", status_code=307)
    if params.get("am"):
        query = {"am": params["am"], **({"nhuan": "1"} if params.get("nhuan") == "1" else {})}
        return RedirectResponse(f"/lich/doi-ngay?{urlencode(query)}", status_code=307)
    today = site_data.today()
    error = ""
    selected_date = today
    month_year, month = today.year, today.month
    raw_month = params.get("thang", "").strip()[:20]
    if raw_month:
        parsed = _parse_month(raw_month)
        if parsed:
            month_year, month = parsed
        else:
            error = _month_error(raw_month)
    raw_day = params.get("ngay", "")
    day_page = False
    try:
        if raw_day:
            selected_date = site_data.parse_date(raw_day)
            day_page = True
            if not raw_month:
                month_year, month = selected_date.year, selected_date.month
        grid = site_data.month_grid(month_year, month)
    except InputError as exc:
        error, grid = exc.message, site_data.month_grid(today.year, today.month)
        selected_date, day_page = today, False
    selected = site_data.day_info(selected_date)
    # "Lịch vạn niên" là cách gọi hay tìm nhất cho lịch âm dương theo tháng.
    title = f"Lịch vạn niên tháng {grid['month']}/{grid['year']}: lịch âm, ngày hoàng đạo"
    description = (
        f"Lịch vạn niên, lịch âm dương tháng {grid['month']}/{grid['year']}: ngày âm từng ngày, "
        "ngày hoàng đạo, mùng 1, rằm, ngày lễ. Bấm một ngày để xem can chi, giờ tốt."
    )
    canonical = ""
    if day_page and not error:
        # Mỗi ngày một trang riêng (người ta hay tìm "lịch âm ngày 5/10/2026"): tiêu đề, mô
        # tả theo ngày đó, URL chuẩn chỉ giữ ?ngay= (link cũ kèm ?thang= trỏ về đây).
        ld = selected["lunar"]
        lunar_short = f"{ld['day']}/{ld['month']}{' nhuận' if ld['isLeapMonth'] else ''}"
        kind = "hoàng đạo" if selected["isHoangDao"] else "hắc đạo"
        hours = ", ".join(h.split(" (")[0] for h in selected["luckyHours"])
        title = f"Lịch âm ngày {selected_date:%d/%m/%Y}: {lunar_short} âm lịch, ngày {kind}"
        description = (
            f"{selected['weekday']}, {selected_date:%d/%m/%Y} dương lịch là {ld['text']} âm "
            f"lịch, ngày {ld['dayCanChi']}, {kind} ({selected['dayGod']}). Giờ tốt: {hours}."
        )
        if selected["holidays"]:
            description += f" {', '.join(selected['holidays'])}."
        canonical = f"/lich?ngay={selected_date.isoformat()}"
    elif not error:
        # Tháng này (nút Tháng trước/sau dẫn tới) là /lich; tháng gõ 10/2026 quy về ?thang=2026-10.
        current = (grid["year"], grid["month"]) == (today.year, today.month)
        canonical = "/lich" if current else f"/lich?thang={grid['year']}-{grid['month']:02d}"
    return _render_lich(
        "lich.html",
        tab="/lich",
        title=title,
        description=description,
        canonical=canonical,
        noindex=bool(error),
        month=grid,
        weeks=_weeks(grid),
        selected=selected,
        selected_date=selected_date,
        today_iso=today.isoformat(),
        today_month=today.year * 12 + today.month,
        error=error,
    )


@router.get("/lich/hoi", response_class=HTMLResponse)
async def lich_ask_page(request: Request) -> HTMLResponse:
    q = request.query_params.get("q", "").strip()[:200]
    return _render_lich(
        "lich_hoi.html",
        tab="/lich/hoi",
        title=f"{q[:60]}: hỏi lịch" if q else "Hỏi lịch âm: ngày tốt, tuổi, sao hạn",
        description="Hỏi tự do về lịch âm: ngày tốt cưới hỏi, khai trương, động thổ, rằm, Tết, "
        "tuổi, sao hạn, đổi ngày; trả lời bằng thuật toán, không dùng AI.",
        noindex=bool(q),
        q=q,
        answer=site_data.calendar_answer(q) if q else "",
        examples=_ASK_EXAMPLES,
    )


@router.get("/lich/doi-ngay", response_class=HTMLResponse)
async def lich_convert_page(request: Request) -> HTMLResponse:
    params = request.query_params
    am, nhuan = params.get("am", "").strip()[:20], params.get("nhuan") == "1"
    error, result = "", None
    if am:
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", am)
        if not m:
            error = (
                f"“{am}” không phải ngày âm lịch. Nhập ngày/tháng hoặc ngày/tháng/năm, "
                "ví dụ 15/8 hoặc 15/8/2027."
            )
        else:
            day, _, msg = lunar.find_lunar_date(
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)) if m.group(3) else None,
                nhuan,
                site_data.today(),
            )
            if day is None:
                error = msg
            else:
                result = site_data.day_info(day)
    title, description = (
        "Đổi ngày âm sang dương lịch",
        "Đổi ngày âm lịch sang dương lịch, có tháng nhuận: nhập 15/8 để biết Trung thu năm nay "
        "là ngày nào, kèm can chi, giờ tốt.",
    )
    if result:
        solar = dt.date.fromisoformat(result["date"])
        title = f"{am} âm lịch là ngày {solar:%d/%m/%Y} dương lịch"
        description = (
            f"Ngày {am} âm lịch ({result['lunar']['text']}) là {result['weekday']}, "
            f"{solar:%d/%m/%Y} dương lịch; kèm can chi, ngày hoàng đạo, giờ tốt."
        )
    return _render_lich(
        "lich_doi.html",
        tab="/lich/doi-ngay",
        title=title,
        description=description,
        noindex=bool(error),
        am=am,
        nhuan=nhuan,
        result=result,
        error=error,
    )


@router.get("/lich/ngay-le", response_class=HTMLResponse)
async def lich_holidays_page() -> HTMLResponse:
    return _render_lich(
        "lich_le.html",
        tab="/lich/ngay-le",
        title="Ngày lễ sắp tới trong năm: âm lịch và dương lịch",
        description="Ngày lễ, Tết âm lịch và dương lịch 12 tháng tới: Tết Nguyên đán, Giỗ Tổ "
        "Hùng Vương, Vu Lan, Trung thu, Quốc khánh, kèm thứ, ngày dương lịch, số ngày còn lại.",
        holidays=_upcoming_holidays(site_data.today(), len(calendar_query.HOLIDAYS)),
    )


_HOLIDAY_TEXTS_PATH = _ROOT / "assets" / "site_holidays.yaml"


@functools.lru_cache(maxsize=1)
def _holiday_texts() -> dict[str, dict]:
    """Giới thiệu ngắn + ngày nghỉ theo luật cho trang từng ngày lễ."""
    with _HOLIDAY_TEXTS_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@router.get("/lich/ngay-le/{key}", response_class=HTMLResponse)
async def holiday_detail_page(request: Request, key: str) -> Response:
    """Một ngày lễ (người ta hay tìm "Tết 2027 là ngày nào", "còn bao nhiêu ngày nữa đến
    Tết"): lần tới, số ngày còn lại, các năm sau."""
    data = site_data.holiday_page(key[:40], site_data.today())
    if data is None:
        return await not_found(request)
    nxt = data["dates"][0]
    day = dt.date.fromisoformat(nxt["date"])
    left = "hôm nay" if nxt["days"] == 0 else f"còn {nxt['days']} ngày"
    lead = (
        f"{data['short']} {day.year} rơi vào {nxt['weekday']}, {day:%d/%m/%Y} dương lịch "
        f"({nxt['lunar']['text']} âm lịch), {left}."
    )
    texts = _holiday_texts().get(key, {})
    page = _render(
        "ngay_le.html",
        title=f"{data['short']} {day.year} là ngày nào? Còn bao nhiêu ngày",
        description=f"{lead} Kèm ngày {data['short']} các năm sau.",
        active="/lich/ngay-le",
        crumb=data["short"],
        holiday=data,
        year=day.year,
        lead=lead,
        about=texts.get("about", ""),
        off=texts.get("nghi_le", ""),
    )
    page.headers["Cache-Control"] = "no-cache"  # số ngày còn lại đổi mỗi ngày
    return page


def _good_days_link(month: str, purpose: str, current: str) -> str:
    """URL chuẩn trang ngày tốt: tháng này bỏ ?thang= (như /lich), tham số theo một thứ tự."""
    query = {"thang": month} if month and month != current else {}
    if purpose:
        query["viec"] = purpose
    return "/lich/ngay-tot" + (f"?{urlencode(query)}" if query else "")


@router.get("/lich/ngay-tot", response_class=HTMLResponse)
async def good_days_page(request: Request) -> HTMLResponse:
    """Ngày tốt theo tháng, lọc theo việc (cưới hỏi, khai trương, động thổ...) - cùng cách
    chọn với Hỏi lịch, mỗi tháng/việc một URL cố định để máy tìm kiếm lập chỉ mục."""
    params = request.query_params
    today = site_data.today()
    year, month, error = today.year, today.month, ""
    raw_month = params.get("thang", "").strip()[:20]
    purpose = params.get("viec", "").strip()[:30]
    if raw_month:
        parsed = _parse_month(raw_month)
        if parsed:
            year, month = parsed
        else:
            error = _month_error(raw_month)
    try:
        data = site_data.good_days(year, month, purpose)
    except InputError as exc:
        error, purpose = exc.message, ""
        year, month = today.year, today.month
        data = site_data.good_days(year, month)
    current = f"{today:%Y-%m}"
    shown = f"{year:04d}-{month:02d}"
    index = year * 12 + month - 1
    prev_month = f"{(index - 1) // 12:04d}-{(index - 1) % 12 + 1:02d}"
    next_month = f"{(index + 1) // 12:04d}-{(index + 1) % 12 + 1:02d}"
    name = data["purposeName"]
    short = name.split(",")[0]
    when = f"tháng {month}/{year}"
    # Tháng này: tóm tắt các ngày tốt CÒN LẠI (bảng vẫn hiện cả tháng, ngày đã qua có ghi chú).
    upcoming = [d for d in data["days"] if shown != current or d["date"] >= today.isoformat()]
    dates = ", ".join(dt.date.fromisoformat(d["date"]).strftime("%d/%m") for d in upcoming[:8])
    dates += "..." if len(upcoming) > 8 else "."
    scope = f"còn lại trong {when}" if shown == current else f"trong {when}"
    if short:
        heading = f"Ngày tốt {short} {when}"
        rule = (
            f"Cách chọn: ngày hoàng đạo, không phạm Tam nương, Nguyệt kỵ, trực "
            f"{'/'.join(data['trucs'])} theo Thập nhị trực."
        )
        kind = f"ngày tốt để {name}"
    else:
        heading = f"Ngày tốt {when}"
        rule = "Cách chọn: ngày hoàng đạo, không phạm Tam nương, Nguyệt kỵ."
        kind = "ngày hoàng đạo tốt"
    summary = (
        f"{len(upcoming)} {kind} {scope}: {dates}"
        if upcoming
        else f"Không có {kind} {scope}, xem tháng sau."
    )
    # Chỉ các tháng gần (tháng trước tới 2 năm sau) vào chỉ mục: nút Tháng trước/sau dẫn
    # được tới mọi tháng 1900-2100, tháng quá xa ít người tìm.
    offset = index - (today.year * 12 + today.month - 1)
    return _render(
        "ngay_tot.html",
        title=f"{heading}: ngày hoàng đạo, giờ tốt",
        description=f"{summary} Kèm ngày âm, giờ hoàng đạo từng ngày.",
        active="/lich/ngay-tot",
        canonical=_good_days_link(shown, purpose, current),
        noindex=bool(error) or not -1 <= offset <= 24,
        heading=heading,
        summary=summary,
        rule=rule,
        data=data,
        today_iso=today.isoformat(),
        error=error,
        purposes=[
            {"label": label, "href": _good_days_link(shown, key, current), "on": key == purpose}
            for key, label in [("", "Tất cả việc")]
            + [(k, v[0].split(",")[0].capitalize()) for k, v in site_data.PURPOSE_SLUGS.items()]
        ],
        months={
            "prev": _good_days_link(prev_month, purpose, current),
            "now": _good_days_link(current, purpose, current),
            "next": _good_days_link(next_month, purpose, current),
        },
    )


_TUOI_FROM = 1940  # trang mục lục /tuoi và sitemap liệt kê từ năm này tới năm nay


def _han_target(data: dict, today: dt.date) -> int | None:
    """Năm sao hạn đưa lên tiêu đề: từ tháng 10 người ta tìm hạn năm sau."""
    years = [h["year"] for h in data["han"]]
    wanted = data["thisYear"] + (today.month >= 10)
    return wanted if wanted in years else (years[-1] if years else None)


@router.get("/tuoi", response_class=HTMLResponse)
async def tuoi_index_page(request: Request) -> Response:
    today = site_data.today()
    raw = request.query_params.get("nam", "").strip()[:10]
    error = ""
    if raw:
        if raw.isdigit() and site_data.MIN_YEAR <= int(raw) <= today.year:
            return RedirectResponse(f"/tuoi/{int(raw)}", status_code=303)
        error = f"Năm sinh cần từ {site_data.MIN_YEAR} đến {today.year}."
    this_year = can_chi.lunar_year_of(today)
    target = this_year + (today.month >= 10)
    decades = [
        (
            start,
            [
                (y, can_chi.year_profile(y)["can_chi"])
                for y in range(max(start, _TUOI_FROM), min(start + 9, today.year) + 1)
            ],
        )
        for start in range(today.year // 10 * 10, _TUOI_FROM - 1, -10)
    ]
    return _render(
        "tuoi.html",
        title=f"Xem tuổi theo năm sinh: mệnh gì, hợp tuổi nào, sao hạn {target}",
        description=f"Tra tuổi theo năm sinh từ {_TUOI_FROM} đến {today.year}: can chi, con giáp, "
        f"mệnh ngũ hành, tuổi hợp và kỵ, sao hạn, Tam tai, Kim lâu, Hoang ốc năm {target}.",
        active="/tuoi",
        noindex=bool(error),
        decades=decades,
        error=error,
        value=raw,
        max_year=today.year,
    )


@router.get("/tuoi/{raw}", response_class=HTMLResponse)
async def tuoi_year_page(request: Request, raw: str) -> Response:
    today = site_data.today()
    if not re.fullmatch(r"\d{4}", raw):
        return await not_found(request)
    try:
        data = site_data.birth_year(int(raw), today)
    except InputError:
        return await not_found(request)
    year = data["year"]
    target = _han_target(data, today)
    han = next((h for h in data["han"] if h["year"] == target), None)
    hop = ", ".join(dict.fromkeys(r["chi"] for r in data["hop"]))
    ky = ", ".join(dict.fromkeys(r["chi"] for r in data["ky"]))
    description = (
        f"Sinh năm {year} là tuổi {data['canChi']} (con {data['conGiap']}), mệnh {data['napAm']}. "
        f"Hợp tuổi {hop}; kỵ tuổi {ky}."
    )
    if han:  # bỏ "(sao xấu)" cho mô tả gọn dưới 160 ký tự, trang vẫn ghi đủ
        sao = {k: han[k].split(" (")[0] for k in ("saoNam", "saoNu")}
        description += f" Sao hạn {target}: nam {sao['saoNam']}, nữ {sao['saoNu']}."
    return _render(
        "tuoi_nam.html",
        title=f"Tuổi {data['canChi']} {year}: mệnh {data['menh']}, hợp tuổi nào"
        + (f", sao hạn {target}" if han else ""),
        description=description,
        active="/tuoi",
        crumb=f"Tuổi {data['canChi']} {year}",
        p=data,
        prev_year=year - 1 if year > site_data.MIN_YEAR else None,
        next_year=year + 1 if year < today.year else None,
    )


async def _limited_weather(
    place: str, coords: tuple[float, float] | None = None, name: str = ""
) -> dict:
    async with _weather_slots:
        if coords:
            label = name or f"Vị trí của tôi ({coords[0]:.2f}; {coords[1]:.2f})".replace(".", ",")
            return await weather.get_weather_at(*coords, label, days=_WEATHER_DAYS)
        return await weather.get_weather(place, days=_WEATHER_DAYS)


def _parse_my_place(raw: str) -> tuple[float, float, str] | None:
    """Cookie vị trí đã lưu -> (lat, lon, tên nơi hoặc ""); hỏng -> None. Cookie sửa tay
    được nên tên chỉ hiện lại cho chính người đó (vẫn escape), cắt độ dài, bỏ ký tự điều
    khiển."""
    m = _MY_PLACE_RE.fullmatch(raw or "")
    coords = _parse_coords(m.group(1)) if m else None
    if coords is None:
        return None
    name = unquote(m.group(2) or "")
    name = "".join(ch for ch in name if ch.isprintable()).strip()[:_MY_PLACE_NAME_MAX]
    return coords[0], coords[1], name


def _parse_coords(raw: str) -> tuple[float, float] | None:
    """Toạ độ "vĩ,kinh" hoặc "vĩ_kinh" -> (lat, lon) làm tròn 2 số lẻ; sai dạng hoặc
    ngoài khoảng -> None."""
    m = _COORDS_RE.fullmatch(raw or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return round(lat, 2), round(lon, 2)


def _expire(times: collections.deque[float], now: float) -> None:
    while times and now - times[0] > _NEW_PLACE_WINDOW_SEC:
        times.popleft()


def _new_place_allowed(ip: str, now: float | None = None) -> str:
    """Lấy 1 lượt tra địa điểm lạ cho ``ip``: "" = được tra, "ip" = IP này hết lượt,
    "site" = cả site hết lượt (không gọi API)."""
    now = time.monotonic() if now is None else now
    mine = _new_place_by_ip.pop(ip, None) or collections.deque()
    _expire(mine, now)
    _expire(_new_place_times, now)
    if len(mine) >= _NEW_PLACE_PER_IP:
        verdict = "ip"
    elif len(_new_place_times) >= _NEW_PLACE_LIMIT:
        verdict = "site"
    else:
        verdict = ""
        mine.append(now)
        _new_place_times.append(now)
    if mine:
        _new_place_by_ip[ip] = mine  # đưa về cuối: IP vừa dùng
        while len(_new_place_by_ip) > _NEW_PLACE_MAX_IPS:
            _new_place_by_ip.popitem(last=False)
    return verdict


@router.get("/thoi-tiet", response_class=HTMLResponse)
async def weather_page(request: Request) -> HTMLResponse:
    place = request.query_params.get("noi", "").strip()[:60]
    places = await weather_places.get()
    error, data = "", None
    mine = _parse_my_place(request.cookies.get(_MY_PLACE_COOKIE, "")[:1000])
    # Không nhập địa điểm mà đã lưu vị trí hiện tại: xem ở đó thay cho TP.HCM.
    coords = mine[:2] if mine and not place else None
    mine_name = mine[2] if mine else ""
    known = {weather_places.fold(p) for p in places}
    limited = ""
    if coords:
        # Toạ độ trong cookie do trình duyệt gửi (sửa tay được): chưa có trong cache thì
        # tính vào cùng hạn mức tra địa điểm mới.
        if not weather.forecast_cached(*coords, _WEATHER_DAYS):
            limited = _new_place_allowed(web_admin.client_ip(request))
    elif place and weather_places.fold(place) not in known and not weather.location_cached(place):
        limited = _new_place_allowed(web_admin.client_ip(request))
    unavailable = not_found = False
    if limited:
        unavailable = True
        error = (
            "Bạn vừa tra nhiều địa điểm mới, thử lại sau ít phút hoặc chọn một địa điểm "
            "trong danh sách."
            if limited == "ip"
            else "Đang tạm giới hạn tra địa điểm mới, chọn một địa điểm trong danh sách."
        )
    else:
        try:
            # Hạn chót tính cả lúc xếp hàng chờ slot, không chỉ lúc tra.
            data = await asyncio.wait_for(
                _limited_weather(place, coords, mine_name), _WEATHER_TIMEOUT_SEC
            )
        except (weather.WeatherError, asyncio.TimeoutError):
            unavailable = True
            error = "Nguồn thời tiết đang chậm hoặc lỗi, thử lại sau ít phút."
    if data and data.get("error"):
        not_found = True
        error, data = f"Không tìm thấy địa điểm “{place}”. Thử gõ tên tỉnh/thành phố.", None
    if data:
        daily = data.get("daily") or []
        temps = [float(t) for d in daily for t in (d.get("min_c"), d.get("max_c")) if t is not None]
        low, span = (min(temps), (max(temps) - min(temps)) or 1) if temps else (0, 1)
        for i, day in enumerate(daily):
            day["label"] = "Hôm nay" if i == 0 else ("Ngày mai" if i == 1 else day["weekday"])
            day["short"] = dt.date.fromisoformat(day["date"]).strftime("%d/%m")
            # Vạch thấp-cao của ngày trên thang chung cả tuần (phần trăm), thiếu số thì bỏ vạch.
            if day.get("min_c") is not None and day.get("max_c") is not None:
                left = (float(day["min_c"]) - low) / span * 100
                width = (float(day["max_c"]) - low) / span * 100 - left
                day["bar"] = (round(left), max(round(width), 4))
    days = len(data["daily"]) if data and data.get("daily") else _WEATHER_DAYS
    heading = "Thời tiết"
    if coords:
        heading = f"Thời tiết {mine_name}" if mine_name else "Thời tiết ở vị trí của bạn"
    elif data:
        heading = f"Thời tiết {data['location']}"
    canonical = ""
    if place and weather.is_default_location(place):
        canonical = "/thoi-tiet"  # TP.HCM, Sài Gòn...: cùng nội dung với trang gốc
    elif place:
        listed = next(
            (p for p in places if weather_places.fold(p) == weather_places.fold(place)), ""
        )
        if listed:  # "ha noi" -> "Hà Nội" như link trong danh sách và sitemap
            canonical = f"/thoi-tiet?{urlencode({'noi': listed}, quote_via=quote)}"
    page = _render(
        "thoi_tiet.html",
        title=f"{heading} hôm nay và {days} ngày tới",
        description=f"{heading}: nhiệt độ, cảm giác, độ ẩm, gió, lượng mưa hiện tại và dự báo "
        f"{days} ngày, cập nhật khoảng 10 phút một lần.",
        active="/thoi-tiet",
        canonical=canonical,
        noindex=not_found,
        heading=heading,
        place=place,
        data=data,
        days=days,
        error=error,
        places=places,
        mine=mine,
        mine_name=mine_name,
        showing_mine=coords is not None,
        notice=_PLACE_NOTICES.get(request.query_params.get("tb", "")),
        fmt=lambda x: "?" if x is None else round(float(x)),
        # lượng mưa có số lẻ: dấu phẩy thập phân kiểu Việt (0,2 mm), số chẵn bỏ ",0"
        mm=lambda x: "?" if x is None else vn_market_data.vnd(float(x), 1).removesuffix(",0"),
    )
    if mine:
        # Trang theo cookie riêng của người xem: không cho bộ đệm dùng chung giữ lại.
        page.headers["Cache-Control"] = "private, no-cache"
    return _source_down(page) if unavailable else page


async def _lottery_data(selector: str, day: str, ticket: str) -> dict:
    async with _lottery_slots:
        return await lottery_lookup.lookup(selector, day, ticket)


async def _limited_lottery(selector: str, day: str, ticket: str) -> dict:
    """Raise LookupInputError (nhập sai) hoặc MarketDataError (nguồn lỗi/chậm)."""
    try:
        # Hạn chót tính cả lúc xếp hàng chờ slot.
        return await asyncio.wait_for(_lottery_data(selector, day, ticket), _LOTTERY_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        raise vn_market_data.MarketDataError("Nguồn xổ số chậm") from None


@router.get("/xo-so", response_class=HTMLResponse)
async def lottery_page(request: Request) -> HTMLResponse:
    params = request.query_params
    selector = params.get("dai", "").strip()[:60]
    raw_day = params.get("ngay", "").strip()[:20]
    raw_ticket = params.get("ve", "").strip()[:12]
    today = site_data.today()
    error, error_field, data = "", "", None
    unavailable = False
    try:
        data = await _limited_lottery(selector, raw_day, raw_ticket)
    except lottery_lookup.LookupInputError as exc:
        error = exc.message
        # Ô nhập sai được tô đỏ và ghi lỗi ngay bên dưới (trang vẫn hiện kỳ mới nhất).
        error_field = {"INVALID_TICKET": "ve", "INVALID_DATE": "ngay"}.get(exc.code, "dai")
        if exc.code != "INVALID_PROVINCE":  # đài đúng mà ngày/vé sai: vẫn hiện kỳ mới nhất
            try:
                data = await _limited_lottery(selector, "", "")
            except (lottery_lookup.LookupInputError, vn_market_data.MarketDataError):
                data = None
    except vn_market_data.MarketDataError:
        unavailable = True
        error = (
            "Nguồn kết quả xổ số đang lỗi hoặc chậm, thử lại sau ít phút. Trang không tự đoán số."
        )
    current = data["target"]["key"] if data else (selector or "mn")
    today_names = [(p.key, p.name) for p in lottery_provinces.drawing_on(today) if p.region != "mb"]
    title = "Kết quả xổ số 3 miền và từng tỉnh"
    description = (
        "Kết quả xổ số miền Nam, miền Trung, miền Bắc và từng đài tỉnh mới nhất: giải đặc biệt "
        "đến giải tám, xem theo ngày, dò vé bằng code. Nguồn xskt.com.vn."
    )
    modified = canonical = ""
    if data and not error:
        # Một kỳ một URL: tên đài, viết tắt (Tây Ninh, xsmn, mien-bac) quy về khoá đài, ngày
        # 26/10/2026 về dạng ISO; miền Nam là trang mặc định /xo-so.
        key = data["target"]["key"]
        query = {} if key == "mn" else {"dai": key}
        # lookup đã kiểm ngày (không ở tương lai): ở đây chỉ đổi dạng, không so lại với hôm nay.
        if asked := lottery_lookup.parse_day(raw_day, dt.date.max):
            query["ngay"] = asked.isoformat()
        canonical = "/xo-so" + (f"?{urlencode(query)}" if query else "")
    if data and data.get("draw"):
        modified = data["draw"]["date"]
        name, day = data["target"]["name"], dt.date.fromisoformat(data["draw"]["date"])
        short = _LOTTERY_SHORT.get(data["target"]["key"])
        title = f"Kết quả xổ số {name} {day:%d/%m/%Y}" + (f" ({short})" if short else "")
        description = (
            f"Kết quả xổ số {name} kỳ {data['draw']['weekday']} {day:%d/%m/%Y}: giải đặc biệt "
            "đến giải tám, dò vé bằng code. Nguồn xskt.com.vn."
        )
    page = _render(
        "xo_so.html",
        title=title,
        description=description,
        modified=modified,
        canonical=canonical,
        active="/xo-so",
        # Nhập sai đài/ngày/vé: trang báo lỗi không vào chỉ mục (URL gõ bừa thành trang trùng).
        noindex=bool(error) and not unavailable,
        data=data,
        error=error,
        error_field=error_field,
        form={"dai": current, "ngay": raw_day, "ve": raw_ticket},
        groups=lottery_lookup.selector_groups(today),
        today_iso=today.isoformat(),
        today_names=today_names,
        prize_order=lottery_lookup.PRIZE_ORDER,
        prize_labels=lottery_lookup.PRIZE_LABELS,
    )
    page.headers["Cache-Control"] = "no-cache"  # kỳ hôm nay cập nhật dần theo giờ quay
    return _source_down(page) if unavailable else page


async def _price_data(load) -> dict:
    async with _price_slots:
        return await load()


async def _limited_prices(load) -> dict:
    """Bảng giá qua services/price_lookup (``load`` = gold/fuel/fx). Raise
    MarketDataError khi nguồn lỗi/chậm - kể cả hết hạn chót lúc chờ slot."""
    try:
        return await asyncio.wait_for(_price_data(load), _PRICE_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        raise vn_market_data.MarketDataError("Nguồn giá chậm") from None


@router.get("/gia-vang", response_class=HTMLResponse)
async def gold_page() -> HTMLResponse:
    error, data = "", None
    try:
        data = await _limited_prices(price_lookup.gold)
    except vn_market_data.MarketDataError:
        error = "Nguồn giá vàng đang lỗi hoặc chậm, thử lại sau ít phút. Trang không tự đoán giá."
    today = f"{site_data.today():%d/%m/%Y}"
    description = (
        "Giá vàng SJC, nhẫn, DOJI, PNJ, Bảo Tín, vàng thế giới quy đổi và tỷ giá "
        "Vietcombank. Nguồn vang.today, SJC dự phòng."
    )
    if data and data.get("domestic"):
        r = data["domestic"][0]
        trieu = functools.partial(vn_market_data.trieu, fixed=True)
        description = (
            f"Giá vàng {r['name']} hôm nay {today}: mua {trieu(r['buy'])}, bán {trieu(r['sell'])} "
            "triệu đồng/lượng. Bảng giá nhẫn SJC, DOJI, PNJ, Bảo Tín, vàng thế giới, tỷ giá."
        )
    page = _render(
        "gia_vang.html",
        title=f"Giá vàng hôm nay {today}: SJC, nhẫn 9999, DOJI, PNJ",
        description=description,
        modified=_vn_stamp(data and data.get("updated")),
        active="/gia-vang",
        data=data,
        error=error,
    )
    page.headers["Cache-Control"] = "no-cache"  # giá đổi trong ngày, cache ở server
    return _source_down(page) if data is None else page


@router.get("/gia-xang", response_class=HTMLResponse)
async def fuel_page() -> HTMLResponse:
    error, data = "", None
    try:
        data = await _limited_prices(price_lookup.fuel)
    except vn_market_data.MarketDataError:
        error = (
            "Nguồn giá xăng dầu đang lỗi hoặc chậm, thử lại sau ít phút. Trang không tự đoán giá."
        )
    today = f"{site_data.today():%d/%m/%Y}"
    description = (
        "Giá xăng dầu Petrolimex (vùng 1, vùng 2) và PVOIL theo kỳ điều hành. "
        "Nguồn giaxanghomnay.com."
    )
    if data and data.get("petrolimex"):
        r, vnd = data["petrolimex"][0], vn_market_data.vnd
        description = (
            f"Giá {r['name']} hôm nay {today}: {vnd(r['zone1'])} đồng/lít vùng 1, "
            f"{vnd(r['zone2'])} đồng/lít vùng 2. Bảng giá xăng dầu Petrolimex, PVOIL theo kỳ điều hành."
        )
    page = _render(
        "gia_xang.html",
        title=f"Giá xăng dầu hôm nay {today}: Petrolimex, PVOIL",
        description=description,
        modified=_vn_stamp(data and data.get("updated")),
        active="/gia-xang",
        data=data,
        error=error,
    )
    page.headers["Cache-Control"] = "no-cache"
    return _source_down(page) if data is None else page


async def _change_place(request: Request, action) -> Response:
    if not _viewer.get()[0]:
        # Chưa đăng nhập (trang không có nút này, chỉ ai tự gửi form): bỏ qua, không đọc form.
        return RedirectResponse("/thoi-tiet", status_code=303)
    form = await _form(request)
    try:
        await action(form.get("ten", ""))
        code = "da-them" if action is weather_places.add else "da-xoa"
    except weather_places.PlacesError as exc:
        code = exc.code
    query = {"tb": code}
    if form.get("noi"):
        query["noi"] = form["noi"]
    return RedirectResponse("/thoi-tiet?" + urlencode(query), status_code=303)


@router.post("/thoi-tiet/vi-tri")
async def weather_my_place_save(request: Request) -> Response:
    """Lưu vị trí hiện tại (JS gửi lat/lon đã làm tròn) vào cookie của trình duyệt, kèm
    tên nơi tra qua Nominatim. Toạ độ chưa tra tên lần nào tính vào hạn mức tra địa điểm
    mới theo IP; hết lượt, lỗi hay chậm thì vẫn lưu, chỉ thiếu tên."""
    form = await _form(request)
    coords = _parse_coords(f"{form.get('lat', '')},{form.get('lon', '')}")
    if coords is None:
        return RedirectResponse("/thoi-tiet?tb=vi-tri-loi", status_code=303)
    name = None
    ip = web_admin.client_ip(request)
    if weather.reverse_cached(*coords) or _new_place_allowed(ip) == "":
        try:
            name = await asyncio.wait_for(weather.reverse_geocode(*coords), _REVERSE_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            name = None
    value = f"{coords[0]:.2f}_{coords[1]:.2f}"
    if name:
        value += "_" + quote(name[:_MY_PLACE_NAME_MAX], safe="")
    response = RedirectResponse("/thoi-tiet?tb=da-luu-vi-tri", status_code=303)
    response.set_cookie(
        _MY_PLACE_COOKIE,
        value,
        max_age=_MY_PLACE_MAX_AGE,
        path=_MY_PLACE_PATH,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@router.post("/thoi-tiet/vi-tri/xoa")
async def weather_my_place_remove() -> Response:
    response = RedirectResponse("/thoi-tiet?tb=da-xoa-vi-tri", status_code=303)
    response.delete_cookie(
        _MY_PLACE_COOKIE, path=_MY_PLACE_PATH, httponly=True, secure=True, samesite="lax"
    )
    return response


@router.post("/thoi-tiet/dia-diem/them")
async def weather_place_add(request: Request) -> Response:
    return await _change_place(request, weather_places.add)


@router.post("/thoi-tiet/dia-diem/xoa")
async def weather_place_remove(request: Request) -> Response:
    return await _change_place(request, weather_places.remove)


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: str(v)[:100] for k, v in form.items()}


def _tuvi_form(form: dict, error: str = "") -> HTMLResponse:
    return _render(
        "tuvi.html",
        title="Lá số tử vi: an sao 12 cung theo ngày giờ sinh",
        description="Lập lá số Tử Vi Đẩu Số theo ngày, giờ sinh dương lịch hoặc âm lịch: 12 cung, "
        "chính tinh kèm độ sáng, phụ tinh, Tứ hóa, Tuần Triệt, đại hạn.",
        active="/tu-vi",
        # Theo giờ đồng hồ từ 23h: Tý đầu (23h-24h) đứng trước Tý (0h-1h).
        hours=[site_data.HOUR_OPTIONS[site_data.EARLY_TY], *site_data.HOUR_OPTIONS[:12]],
        hour_table=[(name, *(f"{h}h" for h in span[:-1].split("-"))) for name, span in _HOURS],
        form=form,
        error=error,
    )


@router.get("/tu-vi", response_class=HTMLResponse)
async def tuvi_page() -> HTMLResponse:
    return _tuvi_form({"gio": "", "lich": "duong", "gioi": "male"})


_TUVI_FIELDS = ("ngay", "thang", "nam", "gio", "gioi", "lich", "nhuan")
_TSH_FIELDS = ("ngay", "ten")


def _clean_ai_text(text: str) -> str:
    """Bỏ ký hiệu markdown hay gặp (**, #) vì trang hiện chữ thường."""
    lines = [
        line.strip().lstrip("#").strip() if line.lstrip().startswith("#") else line
        for line in text.splitlines()
    ]
    return "\n".join(lines).replace("**", "").strip()


async def _ai_reading(form: dict, kind: str, explain) -> str:
    """Luận giải AI khi người xem bấm nút AI và đã đăng nhập; còn lại trả "".
    Chạy dưới khoá lượt của chủ bot như chat (AI qua provider-chain, có hạn chót)."""
    if form.get("ai") != "1" or not _viewer.get()[0]:
        return ""
    async with concurrency.assistant_turn(concurrency.OWNER_TURN_KEY):
        text = await divination_commands.ai_text(config.ALLOWED_USER_ID, kind, "web", explain)
    return _clean_ai_text(text)


@router.post("/tu-vi", response_class=HTMLResponse)
async def tuvi_submit(request: Request) -> HTMLResponse:
    """Kết quả ở trang riêng (tuvi_result.html); nhập sai thì quay lại form.
    Có ai=1 và đã đăng nhập thì kèm AI luận giải."""
    form = await _form(request)
    if form.get("sua") == "1":  # "Sửa thông tin" ở trang kết quả: về form đã điền sẵn
        return _tuvi_form(form)
    try:
        try:
            day, month, year, hour = (int(form.get(k, "")) for k in ("ngay", "thang", "nam", "gio"))
        except ValueError:
            raise InputError("INVALID_DATE", "Nhập đủ ngày, tháng, năm sinh và giờ sinh.") from None
        early_ty = hour == site_data.EARLY_TY
        c = site_data.build_tuvi(
            year,
            month,
            day,
            0 if early_ty else hour,
            form.get("gioi", ""),
            is_lunar=form.get("lich") == "am",
            is_leap=form.get("nhuan") == "1",
            early_ty=early_ty,
        )
    except InputError as exc:
        return _tuvi_form(form, exc.message)
    ai_text = await _ai_reading(form, "tuvi_chitiet", lambda: tuvi.explain_with_ai(c))
    return _render(
        "tuvi_result.html",
        title="Lá số tử vi",
        description="Lá số Tử Vi Đẩu Số.",
        noindex=True,  # kết quả theo ngày sinh của từng người
        active="/tu-vi",
        chart=tuvi.chart_data(c),
        reading=tuvi.reading(c, site_data.today()),
        ai_text=ai_text,
        ai_action="/tu-vi",
        ai_fields={k: form[k] for k in _TUVI_FIELDS if k in form},
        ai_label="AI luận giải lá số",
    )


def _tsh_form(form: dict, error: str = "") -> HTMLResponse:
    return _render(
        "tsh.html",
        title="Thần số học: tính con số chủ đạo theo ngày sinh",
        description="Tính thần số học Pythagoras theo ngày sinh và họ tên: con số chủ đạo, biểu "
        "đồ ngày sinh, mũi tên, 4 đỉnh cao, năm và tháng cá nhân.",
        active="/than-so-hoc",
        form=form,
        error=error,
    )


@router.get("/than-so-hoc", response_class=HTMLResponse)
async def tsh_page() -> HTMLResponse:
    return _tsh_form({})


@router.post("/than-so-hoc", response_class=HTMLResponse)
async def tsh_submit(request: Request) -> HTMLResponse:
    form = await _form(request)
    try:
        birth = site_data.parse_date(form.get("ngay", ""))
        reading = site_data.numerology_reading(birth, form.get("ten", "").strip())
    except InputError as exc:
        return _tsh_form(form, exc.message)
    rows = [
        ("Số ngày sinh", reading["dayNumber"]),
        ("Số thái độ", reading["attitude"]),
        ("Số sứ mệnh (cả tên)", reading["expression"]),
        ("Số linh hồn (nguyên âm)", reading["soul"]),
        ("Số nhân cách (phụ âm)", reading["personality"]),
        ("Số trưởng thành (sau 35-40 tuổi)", reading["maturity"]),
    ]
    name = form.get("ten", "").strip()
    ai_text = await _ai_reading(
        form, "tsh_chitiet", lambda: numerology.explain_with_ai(numerology.compute(birth, name))
    )
    return _render(
        "tsh_result.html",
        title="Thần số học",
        description="Kết quả thần số học.",
        noindex=True,
        active="/than-so-hoc",
        reading=reading,
        rows=rows,
        ai_text=ai_text,
        ai_action="/than-so-hoc",
        ai_fields={k: form[k] for k in _TSH_FIELDS if k in form},
        ai_label="AI diễn giải chi tiết",
    )


def _kymon_page(form: dict, *, ai_text: str = "", error: str = "") -> HTMLResponse:
    """Trang /ky-mon: form ngày/giờ (GET, chia sẻ link được) + bàn 9 cung. Nhập
    sai thì báo lỗi và hiện bàn lúc này."""
    is_now = not (form.get("ngay") or form.get("gio"))
    try:
        moment = site_data.qimen_moment(form.get("ngay", ""), form.get("gio", ""))
    except InputError as exc:
        error, is_now = error or exc.message, True
        moment = site_data.qimen_moment()
    chart = kymon.build_chart(moment)
    data = kymon.chart_data(chart)
    page = _render(
        "kymon.html",
        title="Kỳ Môn Độn Giáp: lập bàn 9 cung theo giờ",
        noindex=not is_now,  # bàn của một giờ tự chọn: vô số URL, chỉ trang gốc vào chỉ mục
        description="Lập bàn Kỳ Môn Độn Giáp theo giờ (chuyển bàn, chiết bổ): cửu tinh, bát môn, "
        "bát thần, cách cục - tính bằng thuật toán.",
        active="/ky-mon",
        chart=data,
        reading=kymon.reading(chart),
        rows=[[data["palaces"][p - 1] for p in row] for row in kymon.GRID],
        good_doors=[f"{d} môn" for d in kymon.CAT_DOORS],
        texts=kymon.texts(),
        form={"ngay": moment.date().isoformat(), "gio": moment.strftime("%H:%M")},
        is_now=is_now,
        question=form.get("cau_hoi", ""),
        ai_text=ai_text,
        error=error,
    )
    if is_now:  # bàn "lúc này" đổi theo giờ: trình duyệt phải hỏi lại server
        page.headers["Cache-Control"] = "no-cache"
    return page


@router.get("/ky-mon", response_class=HTMLResponse)
async def kymon_page(request: Request) -> HTMLResponse:
    params = request.query_params
    return _kymon_page({"ngay": params.get("ngay", "")[:20], "gio": params.get("gio", "")[:10]})


@router.post("/ky-mon", response_class=HTMLResponse)
async def kymon_submit(request: Request) -> HTMLResponse:
    """AI luận giải bàn cho "việc cần xem" (chỉ khi đã đăng nhập và bấm ai=1).
    Câu hỏi đi bằng POST để không nằm trong URL/log truy cập."""
    form = await _form(request)
    raw = await request.form()  # Starlette giữ sẵn form đã đọc ở _form
    form["cau_hoi"] = str(raw.get("cau_hoi", "")).strip()[: kymon.MAX_QUESTION_LEN]
    if form.get("ai") == "1" and _viewer.get()[0] and not form["cau_hoi"]:
        return _kymon_page(form, error="Nhập việc cần xem để AI luận giải.")
    try:
        moment = site_data.qimen_moment(form.get("ngay", ""), form.get("gio", ""))
    except InputError as exc:
        return _kymon_page(form, error=exc.message)
    chart = kymon.build_chart(moment)
    question = form["cau_hoi"]
    ai_text = await _ai_reading(
        form, "kymon_chitiet", lambda: kymon.explain_with_ai(chart, question)
    )
    return _kymon_page(form, ai_text=ai_text)


@static_router.get("/static/{name}")
async def static_file(name: str) -> Response:
    media_type = _STATIC_FILES.get(name)
    if media_type is None:
        return Response(status_code=404)
    return FileResponse(
        _STATIC / name,
        media_type=media_type,
        # URL kèm ?v=<hash nội dung> nên cache lâu được; đổi ảnh thì đổi URL.
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@static_router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return await static_file("favicon.png")


# Bot lấy dữ liệu HUẤN LUYỆN AI (chủ trang chọn chặn): không đem người đọc về, lại quét
# hàng chục nghìn trang ngày trên máy chủ 512 MB. Bot tìm kiếm (Googlebot - kể cả AI
# Overviews, Bingbot, OAI-SearchBot, Claude-SearchBot, PerplexityBot) và bot đọc trang theo
# yêu cầu người dùng (ChatGPT-User, Claude-User) vẫn theo nhóm "*". Google-Extended chỉ là
# token tắt dùng cho Gemini, không ảnh hưởng Google Search.
_AI_TRAINING_BOTS = (
    "GPTBot",
    "ClaudeBot",
    "Google-Extended",
    "Applebot-Extended",
    "CCBot",
    "Bytespider",
    "meta-externalagent",
    "cohere-ai",
)


@static_router.get("/robots.txt", include_in_schema=False)
async def robots(request: Request) -> Response:
    """Cho máy tìm kiếm đọc trang công khai; chặn trang quản trị, API, webhook và bot
    huấn luyện AI. Bot theo nhóm nêu đúng tên nó (RFC 9309), nên nhóm riêng phải đứng
    tách khỏi "*"."""
    lines = [
        *(f"User-agent: {bot}" for bot in _AI_TRAINING_BOTS),
        "Disallow: /",
        "",
        # Không ghi "Allow: /": mặc định đã cho phép, và trình đọc khớp dòng đầu tiên
        # (không theo luật dài nhất như Google) sẽ hiểu "Allow: /" là mở cả /admin.
        "User-agent: *",
        "Disallow: /admin",
        "Disallow: /api/",
        "Disallow: /internal/",
        "Disallow: /webhook",
        f"Sitemap: {_base_url(request)}/sitemap.xml",
    ]
    return PlainTextResponse(
        "\n".join(lines) + "\n", headers={"Cache-Control": "public, max-age=86400"}
    )


# /llms.txt: cách mở từng trang bằng URL (tham số nội dung, trang dùng form POST).
_LLMS_NOTES = {
    "/": "ngày âm hôm nay, giờ hoàng đạo, ngày lễ sắp tới",
    "/lich": "lịch vạn niên theo tháng (?thang=YYYY-MM) hoặc một ngày (?ngay=YYYY-MM-DD)",
    "/lich/ngay-tot": "ngày tốt trong tháng (?thang=YYYY-MM), theo việc (&viec=cuoi-hoi, "
    "khai-truong, dong-tho, nhap-trach, xuat-hanh, ky-ket)",
    "/lich/hoi": "hỏi tự do bằng tiếng Việt (?q=rằm tháng 8)",
    "/lich/doi-ngay": "ngày âm sang dương (?am=15/8 hoặc 15/8/2027, &nhuan=1 cho tháng nhuận)",
    "/lich/ngay-le": "ngày lễ trong 12 tháng tới; từng lễ ở /lich/ngay-le/<tên> (tet-nguyen-dan, "
    "tet-trung-thu...)",
    "/thoi-tiet": "thời tiết hiện tại và 7 ngày tới (?noi=Hà Nội)",
    "/xo-so": "kết quả xổ số (?dai=mn, mt, mb hoặc mã đài như tay-ninh; &ngay=YYYY-MM-DD)",
    "/gia-vang": "giá vàng trong nước, vàng thế giới quy đổi, tỷ giá Vietcombank",
    "/gia-xang": "giá xăng dầu Petrolimex, PVOIL theo kỳ điều hành",
    "/tuoi": "tuổi theo năm sinh: mệnh, hợp/xung, sao hạn (/tuoi/1990)",
    "/tu-vi": "lập lá số tử vi theo ngày giờ sinh (form POST)",
    "/ky-mon": "bàn Kỳ Môn Độn Giáp theo giờ (?ngay=YYYY-MM-DD&gio=HH:MM)",
    "/than-so-hoc": "thần số học theo ngày sinh, họ tên (form POST)",
}


@static_router.get("/llms.txt", include_in_schema=False)
async def llms_txt(request: Request) -> Response:
    """Tóm tắt site cho trợ lý AI/agent theo llmstxt.org: trang công khai và JSON API.
    Google không dùng file này để xếp hạng; nó chỉ giúp agent biết tra gì ở đâu."""
    base, today = _base_url(request), site_data.today()
    pages = [f"- [{_NAV_LABELS[p]}]({base}{p}): {note}" for p, note in _LLMS_NOTES.items()]
    api = [
        f"- [Một ngày âm dương]({base}/api/v1/calendar/days/{today.isoformat()})",
        f"- [Lưới tháng]({base}/api/v1/calendar/months/{today:%Y-%m})",
        f"- [Đổi ngày âm]({base}/api/v1/calendar/conversions?lunarDay=15&lunarMonth=8"
        f"&lunarYear={today.year}&isLeapMonth=false)",
        f"- [Hỏi lịch]({base}/api/v1/calendar/answers?q={quote('rằm tháng 8')})",
        f"- [Xổ số]({base}/api/v1/lottery?province=mn): province, date, ticket",
        f"- [Giá vàng]({base}/api/v1/gold)",
        f"- [Giá xăng dầu]({base}/api/v1/fuel)",
        f"- [Tỷ giá]({base}/api/v1/fx)",
        '- POST /api/v1/tuvi/charts {"birthDate", "calendar", "hourBranch", "gender"}: lá số',
        '- POST /api/v1/numerology/readings {"birthDate", "name"}: thần số học',
        '- POST /api/v1/qimen/charts {"dateTime"}: bàn Kỳ Môn',
    ]
    text = "\n".join(
        [
            "# LCTA",
            "",
            "> Lịch âm dương Việt Nam và tra cứu hằng ngày bằng tiếng Việt: ngày âm, giờ "
            "hoàng đạo, ngày lễ, thời tiết, kết quả xổ số, giá vàng, giá xăng dầu, lá số tử "
            "vi, Kỳ Môn Độn Giáp, thần số học.",
            "",
            "Lịch, tử vi, Kỳ Môn, thần số học tính bằng thuật toán; thời tiết, xổ số, giá đọc "
            "từ nguồn công khai ghi trên từng trang. Trang render sẵn ở server, không cần "
            "JavaScript.",
            "",
            "## Trang",
            *pages,
            "",
            "## API JSON",
            "",
            'Lỗi nhập liệu: HTTP 422 dạng {"error": {"code", "message"}}; nguồn ngoài lỗi: 503.',
            "",
            *api,
        ]
    )
    return PlainTextResponse(text + "\n", headers={"Cache-Control": "public, max-age=86400"})


@static_router.get("/sitemap.xml", include_in_schema=False)
async def sitemap(request: Request) -> Response:
    """Trang công khai + trang tra cứu hay tìm (từng đài xổ số, thời tiết các nơi có sẵn,
    lịch 12 tháng tới). lastmod chỉ ghi khi biết chắc: trang đổi số liệu mỗi ngày ghi hôm
    nay, đài xổ số ghi ngày quay gần nhất; trang công cụ tĩnh bỏ trống."""
    base, today = _base_url(request), site_data.today()
    entries: list[tuple[str, dt.date | None]] = [
        (path, today if path in _DAILY_PATHS else None) for path in _SITEMAP_PATHS
    ]
    # Miền Nam chính là /xo-so; đài "mien-bac" là kỳ chung miền Bắc (?dai=mb).
    entries += [(f"/xo-so?dai={key}", today) for key in ("mt", "mb")]
    entries += [
        (f"/xo-so?dai={p.key}", lottery_provinces.last_draw_day(p, today))
        for p in lottery_provinces.PROVINCES
        if p.region != "mb"
    ]
    entries += [
        (f"/thoi-tiet?{urlencode({'noi': place}, quote_via=quote)}", today)
        for place in weather_places.DEFAULT_PLACES[1:]  # chỗ đầu (TP.HCM) chính là /thoi-tiet
    ]
    # Trang từng ngày lễ, ngày tốt 6 tháng tới theo từng việc, tuổi theo năm sinh.
    entries += [(f"/lich/ngay-le/{key}", None) for key in site_data.HOLIDAY_SLUGS]
    current = f"{today:%Y-%m}"
    for i in range(6):
        index = today.year * 12 + today.month - 1 + i
        month = f"{index // 12:04d}-{index % 12 + 1:02d}"
        entries += [
            (_good_days_link(month, purpose, current), None)
            for purpose in ("", *site_data.PURPOSE_SLUGS)
            if i or purpose  # /lich/ngay-tot (tháng này) đã có trong _SITEMAP_PATHS
        ]
    entries += [(f"/tuoi/{y}", None) for y in range(today.year, _TUOI_FROM - 1, -1)]
    first = today.replace(day=1)
    for i in range(1, 12):
        month = (first.month - 1 + i) % 12 + 1
        entries.append(
            (f"/lich?thang={first.year + (first.month - 1 + i) // 12}-{month:02d}", None)
        )
    urls = "".join(
        f"<url><loc>{escape(base + path)}</loc>"
        + (f"<lastmod>{day.isoformat()}</lastmod>" if day else "")
        + "</url>"
        for path, day in entries
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    )
    return Response(
        body,
        media_type="application/xml",
        # lastmod theo ngày: bộ đệm 1 giờ để sang ngày mới không giữ bản cũ quá lâu
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def not_found(request: Request) -> HTMLResponse:
    """Trang 404 cho trình duyệt (web.py gọi khi đường dẫn không tồn tại)."""
    await _set_viewer(request)
    page = _render(
        "404.html",
        title="Không tìm thấy trang",
        description="Trang bạn tìm không có hoặc đã đổi địa chỉ.",
        active="",
        noindex=True,
    )
    page.status_code = 404
    return page


# ─── JSON API ───────────────────────────────────────────────────────────────


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _ok(data: dict) -> JSONResponse:
    return JSONResponse(data)


@api_router.get("/calendar/days/{date}")
async def api_day(date: str) -> JSONResponse:
    if not re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", date):
        return _error(422, "INVALID_DATE", "Ngày có dạng YYYY-MM-DD.")
    try:
        return _ok(site_data.day_info(site_data.parse_date(date)))
    except InputError as exc:
        return _error(422, exc.code, exc.message)


@api_router.get("/calendar/months/{month}")
async def api_month(month: str) -> JSONResponse:
    m = re.fullmatch(r"(\d{4})-(\d{2})", month)
    if not m:
        return _error(422, "INVALID_MONTH", "Tháng có dạng YYYY-MM.")
    try:
        return _ok(site_data.month_grid(int(m.group(1)), int(m.group(2))))
    except InputError as exc:
        return _error(422, exc.code, exc.message)


@api_router.get("/calendar/conversions")
async def api_conversion(request: Request) -> JSONResponse:
    p = request.query_params
    try:
        day, month, year = (int(p.get(k, "")) for k in ("lunarDay", "lunarMonth", "lunarYear"))
    except ValueError:
        return _error(422, "INVALID_LUNAR_DATE", "Cần lunarDay, lunarMonth, lunarYear là số.")
    flag = p.get("isLeapMonth", "false").lower()
    if flag not in ("true", "false", "1", "0"):
        return _error(422, "INVALID_LEAP_FLAG", "isLeapMonth là true hoặc false.")
    try:
        return _ok(site_data.lunar_to_solar(day, month, year, flag in ("true", "1")))
    except InputError as exc:
        return _error(422, exc.code, exc.message)


@api_router.get("/calendar/answers")
async def api_answer(q: str = "") -> JSONResponse:
    try:
        return _ok({"question": q, "answer": site_data.calendar_answer(q)})
    except InputError as exc:
        return _error(422, exc.code, exc.message)


async def _json_body(request: Request) -> dict:
    body = await request.body()
    if len(body) > _MAX_JSON_BYTES:
        raise InputError("BODY_TOO_LARGE", "Body tối đa 4 KB.")
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise InputError("INVALID_JSON", "Body phải là JSON.") from None
    if not isinstance(data, dict):
        raise InputError("INVALID_JSON", "Body phải là object JSON.")
    return data


@api_router.post("/numerology/readings")
async def api_numerology(request: Request) -> JSONResponse:
    try:
        data = await _json_body(request)
        name = data.get("name") or ""
        if not isinstance(name, str):
            raise InputError("INVALID_NAME", "name phải là chuỗi.")
        birth = site_data.parse_date(str(data.get("birthDate") or ""))
        return _ok(site_data.numerology_reading(birth, name.strip()))
    except InputError as exc:
        return _error(422, exc.code, exc.message)


@api_router.post("/tuvi/charts")
async def api_tuvi(request: Request) -> JSONResponse:
    try:
        data = await _json_body(request)
        calendar = data.get("calendar", "solar")
        if calendar not in ("solar", "lunar"):
            raise InputError("INVALID_CALENDAR", "calendar là solar hoặc lunar.")
        hour = data.get("hourBranch")
        if not isinstance(hour, int) or isinstance(hour, bool):
            raise InputError("INVALID_HOUR", "hourBranch là số 0 (Tý) đến 11 (Hợi).")
        leap, early = data.get("isLeapMonth", False), data.get("isEarlyTy", False)
        if not isinstance(leap, bool) or not isinstance(early, bool):
            raise InputError("INVALID_FLAG", "isLeapMonth và isEarlyTy là true/false (bool).")
        if leap and calendar != "lunar":
            raise InputError("INVALID_LEAP_FLAG", "isLeapMonth chỉ dùng với calendar=lunar.")
        year, month, day = site_data.parse_ymd(str(data.get("birthDate") or ""))
        chart = site_data.tuvi_chart(
            year,
            month,
            day,
            hour,
            str(data.get("gender", "")),
            is_lunar=calendar == "lunar",
            is_leap=leap,
            early_ty=early,
        )
        return _ok(chart)
    except InputError as exc:
        return _error(422, exc.code, exc.message)


@api_router.post("/qimen/charts")
async def api_qimen(request: Request) -> JSONResponse:
    try:
        data = await _json_body(request)
        raw = data.get("dateTime")
        if raw is not None and not isinstance(raw, str):
            raise InputError("INVALID_DATETIME", "dateTime phải là chuỗi ISO 8601.")
        moment = site_data.parse_datetime(raw) if raw else site_data.qimen_moment()
        return _ok(site_data.qimen_chart(moment))
    except InputError as exc:
        return _error(422, exc.code, exc.message)


@api_router.get("/lottery")
async def api_lottery(province: str = "", date: str = "", ticket: str = "") -> JSONResponse:
    try:
        return _ok(await _limited_lottery(province[:60], date[:20], ticket[:12]))
    except lottery_lookup.LookupInputError as exc:
        return _error(422, exc.code, exc.message)
    except vn_market_data.MarketDataError:
        return _error(503, "SOURCE_UNAVAILABLE", "Nguồn kết quả xổ số đang lỗi, thử lại sau.")


async def _api_prices(load, label: str) -> JSONResponse:
    try:
        return _ok(await _limited_prices(load))
    except vn_market_data.MarketDataError:
        return _error(503, "SOURCE_UNAVAILABLE", f"Nguồn {label} đang lỗi, thử lại sau.")


@api_router.get("/gold")
async def api_gold() -> JSONResponse:
    return await _api_prices(price_lookup.gold, "giá vàng")


@api_router.get("/fuel")
async def api_fuel() -> JSONResponse:
    return await _api_prices(price_lookup.fuel, "giá xăng dầu")


@api_router.get("/fx")
async def api_fx() -> JSONResponse:
    return await _api_prices(price_lookup.fx, "tỷ giá")
