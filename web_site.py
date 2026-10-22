"""Web LCTA: trang chủ, /lich (+ /hoi, /doi-ngay, /ngay-le), /tu-vi,
/than-so-hoc (form -> trang kết quả riêng), /thoi-tiet và JSON API /api/v1.

Xem nội dung KHÔNG cần đăng nhập: lịch, tử vi, thần số học tính bằng code
(services/site_data.py), không gọi AI, không đụng DB. /thoi-tiet tra Open-Meteo
qua services/weather.py (cache có trần), giới hạn số lượt tra song song. Trang
render sẵn ở server nên chạy được cả khi tắt JavaScript.

Cần đăng nhập (tài khoản admin, chung phiên với /admin) cho việc tốn quota hoặc
đổi dữ liệu chung: nút "AI luận giải" ở kết quả tử vi/thần số học, thêm/xoá địa
điểm thời tiết. Đăng nhập rồi header mới hiện nút "Quản trị".

JSON API (chỉ thêm trường mới, không đổi/bỏ trường cũ):
  GET  /api/v1/calendar/days/{YYYY-MM-DD}
  GET  /api/v1/calendar/months/{YYYY-MM}
  GET  /api/v1/calendar/conversions?lunarDay=&lunarMonth=&lunarYear=&isLeapMonth=true|false
  GET  /api/v1/calendar/answers?q=
  POST /api/v1/numerology/readings  {"birthDate": "YYYY-MM-DD"|"DD/MM/YYYY", "name"?: str}
  POST /api/v1/tuvi/charts          {"birthDate": "YYYY-MM-DD"|"DD/MM/YYYY",
                                     "calendar": "solar"|"lunar", "isLeapMonth"?: bool (chỉ lunar),
                                     "hourBranch": 0-11, "gender": "male"|"female",
                                     "isEarlyTy"?: bool (sinh 23h-24h, an theo ngày hôm sau)}
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
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

import web_admin
from core import config
from services import (
    calendar_query,
    concurrency,
    divination_commands,
    lunar,
    numerology,
    site_data,
    tuvi,
    weather,
    weather_places,
)
from services.site_data import InputError

_ROOT = Path(__file__).resolve().parent
_STATIC = _ROOT / "static"
_STATIC_FILES = {
    "logo-light.webp": "image/webp",
    "logo-dark.webp": "image/webp",
    "favicon.png": "image/png",
}
_NAV = [
    ("/", "Trang chủ"),
    ("/lich", "Lịch"),
    ("/thoi-tiet", "Thời tiết"),
    ("/tu-vi", "Tử vi"),
    ("/than-so-hoc", "Thần số học"),
]
_LICH_TABS = [
    ("/lich", "Lịch tháng"),
    ("/lich/hoi", "Hỏi lịch"),
    ("/lich/doi-ngay", "Đổi ngày âm"),
    ("/lich/ngay-le", "Ngày lễ"),
]
_ASK_EXAMPLES = [
    "rằm tháng 8",
    "còn mấy ngày nữa Tết",
    "ngày tốt tháng này",
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
    "dang-nhap": ("error", "Cần đăng nhập để thêm/xoá địa điểm."),
}
_WEATHER_DAYS = 7
_WEATHER_TIMEOUT_SEC = 15
# Trang công khai: chặn số lượt gọi Open-Meteo song song để người lạ spam
# địa danh không chiếm hết kết nối/RAM của 1 worker (webhook Telegram/Zalo).
_weather_slots = asyncio.Semaphore(3)
# Địa điểm LẠ (không có trong danh sách hay xem) tốn 3-4 request Open-Meteo mỗi
# lượt (geocoding + dự báo) và dùng chung hạn mức miễn phí với tool thời tiết
# của chat: giới hạn tổng số lượt tra địa điểm lạ toàn site trong 10 phút.
_NEW_PLACE_LIMIT, _NEW_PLACE_WINDOW_SEC = 30, 600
_new_place_times: collections.deque[float] = collections.deque()
_MAX_JSON_BYTES = 4096


# (đã đăng nhập, đường dẫn đang xem) của request hiện tại - để header hiện nút
# Quản trị/Đăng xuất hay Đăng nhập mà không phải truyền request qua mọi hàm.
_viewer: contextvars.ContextVar[tuple[bool, str]] = contextvars.ContextVar(
    "site_viewer", default=(False, "/")
)


def _current_path(request: Request) -> str:
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


def _login_url(path: str) -> str:
    return "/admin?" + urlencode({"next": web_admin.safe_next(path)})


async def _set_viewer(request: Request) -> bool:
    logged_in = await web_admin.is_logged_in(request)
    # POST (kết quả form) không mở lại được bằng link: đăng nhập xong về trang form.
    path = _current_path(request) if request.method == "GET" else request.url.path
    _viewer.set((logged_in, path))
    return logged_in


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


@functools.lru_cache(maxsize=1)
def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_ROOT / "templates" / "site")),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["vn_date"] = lambda iso: dt.date.fromisoformat(iso).strftime("%d/%m/%Y")
    return env


@functools.lru_cache(maxsize=1)
def _assets() -> tuple[Markup, str]:
    """(CSS nhúng thẳng vào HTML, phiên bản ảnh). CSS inline để trang không kéo
    thêm request song song: uvicorn chạy --limit-concurrency (đếm cả kết nối
    keep-alive rảnh), nhiều request phụ từng làm webhook Telegram/Zalo nhận 503.
    Logo/favicon vẫn là file riêng nhưng cache 1 năm theo phiên bản nội dung."""
    css = Markup((_STATIC / "site.css").read_text(encoding="utf-8"))
    digest = hashlib.sha256(b"".join((_STATIC / n).read_bytes() for n in sorted(_STATIC_FILES)))
    return css, digest.hexdigest()[:10]


def _render(name: str, *, title: str, description: str, active: str, **ctx) -> HTMLResponse:
    css, version = _assets()
    html = (
        _env()
        .get_template(name)
        .render(
            title=title,
            description=description,
            active=active,
            nav=_NAV,
            inline_css=css,
            v=version,
            logged_in=_viewer.get()[0],
            login_url=_login_url(_viewer.get()[1]),
            **ctx,
        )
    )
    # Lỗi nhập liệu hiện ngay trên trang (role=alert) với HTTP 200: trang HTML
    # trả 4xx làm trình duyệt ghi lỗi console dù người dùng chỉ nhập sai.
    return HTMLResponse(html)


def _render_lich(name: str, *, tab: str, title: str, description: str, **ctx) -> HTMLResponse:
    return _render(
        name, title=title, description=description, active="/lich", tabs=_LICH_TABS, tab=tab, **ctx
    )


def wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


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


def home_page() -> HTMLResponse:
    today = site_data.today()
    return _render(
        "home.html",
        title="Lịch âm, tử vi, thần số học",
        description="Tra lịch âm dương, ngày tốt xấu, lá số tử vi và thần số học - tính bằng thuật toán.",
        active="/",
        today=site_data.day_info(today),
        today_date=today,
        next_holiday=_upcoming_holidays(today, 1)[0],
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
    raw_month = params.get("thang", "")
    if raw_month:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})", raw_month)
        if m:
            month_year, month = int(m.group(1)), int(m.group(2))
        else:
            error = "Tháng phải có dạng YYYY-MM."
    raw_day = params.get("ngay", "")
    try:
        if raw_day:
            selected_date = site_data.parse_date(raw_day)
            if not raw_month:
                month_year, month = selected_date.year, selected_date.month
        grid = site_data.month_grid(month_year, month)
    except InputError as exc:
        error, grid = exc.message, site_data.month_grid(today.year, today.month)
        selected_date = today
    return _render_lich(
        "lich.html",
        tab="/lich",
        title="Lịch tháng",
        description="Lịch tháng dương - âm, ngày hoàng đạo, ngày lễ.",
        month=grid,
        weeks=_weeks(grid),
        selected=site_data.day_info(selected_date),
        selected_date=selected_date,
        today_iso=today.isoformat(),
        error=error,
    )


@router.get("/lich/hoi", response_class=HTMLResponse)
async def lich_ask_page(request: Request) -> HTMLResponse:
    q = request.query_params.get("q", "").strip()[:200]
    return _render_lich(
        "lich_hoi.html",
        tab="/lich/hoi",
        title="Hỏi lịch",
        description="Hỏi tự do về lịch âm, ngày tốt, tuổi, sao hạn - trả lời bằng thuật toán.",
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
            error = "Ngày âm phải có dạng ngày/tháng hoặc ngày/tháng/năm, ví dụ 15/8/2027."
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
    return _render_lich(
        "lich_doi.html",
        tab="/lich/doi-ngay",
        title="Đổi ngày âm sang dương",
        description="Đổi ngày âm lịch sang dương lịch, có tháng nhuận.",
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
        title="Ngày lễ sắp tới",
        description="Các ngày lễ âm lịch và dương lịch sắp tới.",
        holidays=_upcoming_holidays(site_data.today(), len(calendar_query.HOLIDAYS)),
    )


async def _limited_weather(place: str) -> dict:
    async with _weather_slots:
        return await weather.get_weather(place, days=_WEATHER_DAYS)


def _new_place_allowed(now: float | None = None) -> bool:
    """Lấy 1 lượt trong ngân sách tra địa điểm lạ; hết thì False (không gọi API)."""
    now = time.monotonic() if now is None else now
    while _new_place_times and now - _new_place_times[0] > _NEW_PLACE_WINDOW_SEC:
        _new_place_times.popleft()
    if len(_new_place_times) >= _NEW_PLACE_LIMIT:
        return False
    _new_place_times.append(now)
    return True


@router.get("/thoi-tiet", response_class=HTMLResponse)
async def weather_page(request: Request) -> HTMLResponse:
    place = request.query_params.get("noi", "").strip()[:60]
    places = await weather_places.get()
    error, data = "", None
    known = {weather_places.fold(p) for p in places}
    if place and weather_places.fold(place) not in known and not _new_place_allowed():
        error = "Đang tạm giới hạn tra địa điểm mới, chọn một địa điểm trong danh sách."
    else:
        try:
            # Hạn chót tính cả lúc xếp hàng chờ slot, không chỉ lúc tra.
            data = await asyncio.wait_for(_limited_weather(place), _WEATHER_TIMEOUT_SEC)
        except (weather.WeatherError, asyncio.TimeoutError):
            error = "Nguồn thời tiết đang chậm hoặc lỗi, thử lại sau ít phút."
    if data and data.get("error"):
        error, data = f"Không tìm thấy địa điểm “{place}”. Thử gõ tên tỉnh/thành phố.", None
    if data:
        for i, day in enumerate(data.get("daily") or []):
            day["label"] = "Hôm nay" if i == 0 else ("Ngày mai" if i == 1 else day["weekday"])
            day["short"] = dt.date.fromisoformat(day["date"]).strftime("%d/%m")
    return _render(
        "thoi_tiet.html",
        title=f"Thời tiết {data['location']}" if data else "Thời tiết",
        description="Thời tiết hiện tại và dự báo 7 ngày cho các tỉnh thành Việt Nam.",
        active="/thoi-tiet",
        place=place,
        data=data,
        error=error,
        places=places,
        notice=_PLACE_NOTICES.get(request.query_params.get("tb", "")),
        fmt=lambda x: "?" if x is None else round(float(x)),
    )


async def _change_place(request: Request, action) -> Response:
    form: dict[str, str] = {}
    if not _viewer.get()[0]:
        code = "dang-nhap"  # chưa đăng nhập: không đọc form
    else:
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
        title="Lá số tử vi",
        description="An lá số Tử Vi Đẩu Số: 12 cung, chính tinh, phụ tinh, đại hạn.",
        active="/tu-vi",
        hours=site_data.HOUR_OPTIONS,
        form=form,
        error=error,
    )


@router.get("/tu-vi", response_class=HTMLResponse)
async def tuvi_page() -> HTMLResponse:
    return _tuvi_form({"gio": "0", "lich": "duong", "gioi": "male"})


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
        active="/tu-vi",
        chart=tuvi.chart_data(c),
        ai_text=ai_text,
        ai_action="/tu-vi",
        ai_fields={k: form[k] for k in _TUVI_FIELDS if k in form},
        ai_label="AI luận giải lá số",
    )


def _tsh_form(form: dict, error: str = "") -> HTMLResponse:
    return _render(
        "tsh.html",
        title="Thần số học",
        description="Con số chủ đạo, biểu đồ ngày sinh, năm cá nhân theo thần số học Pythagoras.",
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
        active="/than-so-hoc",
        reading=reading,
        rows=rows,
        ai_text=ai_text,
        ai_action="/than-so-hoc",
        ai_fields={k: form[k] for k in _TSH_FIELDS if k in form},
        ai_label="AI diễn giải chi tiết",
    )


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
