"""Web công khai: trang chủ, /lich (+ /hoi, /doi-ngay, /ngay-le), /tu-vi,
/than-so-hoc (form -> trang kết quả riêng) và JSON API /api/v1.

Chỉ tính toán bằng code (services/site_data.py), KHÔNG gọi AI, không đụng DB
- trang công khai không được đốt quota AI. Trang render sẵn ở server nên chạy
được cả khi tắt JavaScript.

JSON API (chỉ thêm trường mới, không đổi/bỏ trường cũ):
  GET  /api/v1/calendar/days/{YYYY-MM-DD}
  GET  /api/v1/calendar/months/{YYYY-MM}
  GET  /api/v1/calendar/conversions?lunarDay=&lunarMonth=&lunarYear=&isLeapMonth=
  GET  /api/v1/calendar/answers?q=
  POST /api/v1/numerology/readings  {"birthDate": "YYYY-MM-DD", "name"?: str}
  POST /api/v1/tuvi/charts          {"birthDate", "calendar": "solar"|"lunar",
                                     "isLeapMonth"?, "hourBranch": 0-11, "gender": "male"|"female"}
Ngày sinh/họ tên đi bằng POST để không nằm trong URL và log truy cập.
Mọi lỗi: HTTP 4xx + {"error": {"code": "...", "message": "..."}}.
"""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from services import calendar_query, lunar, site_data
from services.site_data import InputError

_ROOT = Path(__file__).resolve().parent
_STATIC = _ROOT / "static"
_STATIC_FILES = {
    "logo-light.webp": "image/webp",
    "logo-dark.webp": "image/webp",
    "favicon.png": "image/png",
}
_NAV = [("/", "Trang chủ"), ("/lich", "Lịch"), ("/tu-vi", "Tử vi"), ("/than-so-hoc", "Thần số học")]
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
_MAX_JSON_BYTES = 4096

router = APIRouter(include_in_schema=False)
api_router = APIRouter(prefix="/api/v1")


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


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: str(v)[:100] for k, v in form.items()}


def _tuvi_form(form: dict, error: str = "") -> HTMLResponse:
    return _render(
        "tuvi.html",
        title="Lá số tử vi",
        description="An lá số Tử Vi Đẩu Số: 12 cung, chính tinh, phụ tinh, đại hạn.",
        active="/tu-vi",
        hours=site_data.HOUR_BRANCHES,
        form=form,
        error=error,
    )


@router.get("/tu-vi", response_class=HTMLResponse)
async def tuvi_page() -> HTMLResponse:
    return _tuvi_form({"gio": "0", "lich": "duong", "gioi": "male"})


@router.post("/tu-vi", response_class=HTMLResponse)
async def tuvi_submit(request: Request) -> HTMLResponse:
    """Kết quả ở trang riêng (tuvi_result.html); nhập sai thì quay lại form."""
    form = await _form(request)
    try:
        try:
            day, month, year, hour = (int(form.get(k, "")) for k in ("ngay", "thang", "nam", "gio"))
        except ValueError:
            raise InputError("INVALID_DATE", "Nhập đủ ngày, tháng, năm sinh và giờ sinh.") from None
        chart = site_data.tuvi_chart(
            year,
            month,
            day,
            hour,
            form.get("gioi", ""),
            is_lunar=form.get("lich") == "am",
            is_leap=form.get("nhuan") == "1",
        )
    except InputError as exc:
        return _tuvi_form(form, exc.message)
    return _render(
        "tuvi_result.html",
        title="Lá số tử vi",
        description="Lá số Tử Vi Đẩu Số.",
        active="/tu-vi",
        chart=chart,
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
    ]
    return _render(
        "tsh_result.html",
        title="Thần số học",
        description="Kết quả thần số học.",
        active="/than-so-hoc",
        reading=reading,
        rows=rows,
    )


@router.get("/static/{name}")
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


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return await static_file("favicon.png")


# ─── JSON API ───────────────────────────────────────────────────────────────


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _ok(data: dict) -> JSONResponse:
    return JSONResponse(data)


@api_router.get("/calendar/days/{date}")
async def api_day(date: str) -> JSONResponse:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
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
    try:
        return _ok(site_data.lunar_to_solar(day, month, year, p.get("isLeapMonth") == "true"))
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
        birth = site_data.parse_date(str(data.get("birthDate", "")))
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
        year, month, day = site_data.parse_ymd(str(data.get("birthDate", "")))
        chart = site_data.tuvi_chart(
            year,
            month,
            day,
            hour,
            str(data.get("gender", "")),
            is_lunar=calendar == "lunar",
            is_leap=data.get("isLeapMonth") is True,
        )
        return _ok(chart)
    except InputError as exc:
        return _error(422, exc.code, exc.message)
