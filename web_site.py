"""Web công khai: trang chủ, /lich, /tu-vi, /than-so-hoc và JSON API /api/v1.

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
import base64
import functools
import json
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from services import calendar_query, site_data
from services.site_data import InputError

_ROOT = Path(__file__).resolve().parent
_STATIC = _ROOT / "static"
_STATIC_FILES = {"site.css": "text/css; charset=utf-8", "logo.svg": "image/svg+xml"}
_NAV = [("/", "Trang chủ"), ("/lich", "Lịch"), ("/tu-vi", "Tử vi"), ("/than-so-hoc", "Thần số học")]
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
def _inline_assets() -> tuple[Markup, str]:
    """CSS và logo nhúng thẳng vào HTML: mỗi lượt xem trang chỉ còn 1 request.
    Uvicorn chạy --limit-concurrency (đếm cả kết nối keep-alive đang rảnh);
    trình duyệt tải CSS + logo + favicon song song từng làm nhiều người xem
    cùng lúc vượt giới hạn -> 503 cho chính webhook Telegram/Zalo."""
    css = Markup((_STATIC / "site.css").read_text(encoding="utf-8"))
    logo = base64.b64encode((_STATIC / "logo.svg").read_bytes()).decode("ascii")
    return css, f"data:image/svg+xml;base64,{logo}"


def _render(name: str, *, title: str, description: str, active: str, **ctx) -> HTMLResponse:
    css, logo = _inline_assets()
    html = (
        _env()
        .get_template(name)
        .render(
            title=title,
            description=description,
            active=active,
            nav=_NAV,
            inline_css=css,
            logo_uri=logo,
            **ctx,
        )
    )
    # Lỗi nhập liệu hiện ngay trên trang (role=alert) với HTTP 200: trang HTML
    # trả 4xx làm trình duyệt ghi lỗi console dù người dùng chỉ nhập sai.
    return HTMLResponse(html)


def wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


# ─── Trang ───────────────────────────────────────────────────────────────────


def home_page() -> HTMLResponse:
    today = site_data.today()
    upcoming = []
    for holiday in calendar_query.HOLIDAYS:
        day = calendar_query.holiday_entry_date(holiday, today)
        if day is not None:
            upcoming.append((day, holiday[0]))
    upcoming.sort()
    items = [
        {"name": name, "when": f"{day:%d/%m/%Y}", "days": (day - today).days}
        for day, name in upcoming[:5]
    ]
    return _render(
        "home.html",
        title="Lịch âm, tử vi, thần số học",
        description="Tra lịch âm dương, ngày tốt xấu, lá số tử vi và thần số học - tính bằng thuật toán.",
        active="/",
        today=site_data.day_info(today),
        today_date=today,
        upcoming=items,
    )


@router.get("/lich", response_class=HTMLResponse)
async def lich_page(request: Request) -> HTMLResponse:
    params = request.query_params
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

    q, answer = params.get("q", "").strip()[:200], ""
    if q:
        answer = site_data.calendar_answer(q)
    am, nhuan = params.get("am", "").strip()[:20], params.get("nhuan") == "1"
    if am:
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", am)
        if not m:
            error = "Ngày âm phải có dạng ngày/tháng hoặc ngày/tháng/năm."
        else:
            year = f"/{m.group(3)}" if m.group(3) else ""
            answer = calendar_query.answer(
                f"âm {m.group(1)}/{m.group(2)}{'n' if nhuan else ''}{year}", today
            )

    weeks, week = [], [None] * grid["days"][0]["weekday"]
    for d in grid["days"]:
        week.append(d)
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        weeks.append(week + [None] * (7 - len(week)))
    return _render(
        "lich.html",
        title="Lịch âm dương",
        description="Lịch tháng có ngày âm, ngày hoàng đạo, ngày lễ; đổi ngày âm dương.",
        active="/lich",
        month=grid,
        weeks=weeks,
        selected=site_data.day_info(selected_date),
        selected_date=selected_date,
        today_iso=today.isoformat(),
        q=q,
        answer=answer,
        am=am,
        nhuan=nhuan,
        error=error,
    )


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: str(v)[:100] for k, v in form.items()}


def _tuvi_render(form: dict, chart: dict | None = None, error: str = "") -> HTMLResponse:
    return _render(
        "tuvi.html",
        title="Lá số tử vi",
        description="An lá số Tử Vi Đẩu Số: 12 cung, chính tinh, phụ tinh, đại hạn.",
        active="/tu-vi",
        hours=site_data.HOUR_BRANCHES,
        form=form,
        chart=chart,
        error=error,
    )


@router.get("/tu-vi", response_class=HTMLResponse)
async def tuvi_page() -> HTMLResponse:
    return _tuvi_render({"gio": "0", "lich": "duong", "gioi": "male"})


@router.post("/tu-vi", response_class=HTMLResponse)
async def tuvi_submit(request: Request) -> HTMLResponse:
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
        return _tuvi_render(form, error=exc.message)
    return _tuvi_render(form, chart)


def _tsh_render(form: dict, reading: dict | None = None, error: str = "") -> HTMLResponse:
    rows = []
    if reading:
        rows = [
            ("Số ngày sinh", reading["dayNumber"]),
            ("Số thái độ", reading["attitude"]),
            ("Số sứ mệnh (cả tên)", reading["expression"]),
            ("Số linh hồn (nguyên âm)", reading["soul"]),
            ("Số nhân cách (phụ âm)", reading["personality"]),
        ]
    return _render(
        "tsh.html",
        title="Thần số học",
        description="Con số chủ đạo, biểu đồ ngày sinh, năm cá nhân theo thần số học Pythagoras.",
        active="/than-so-hoc",
        form=form,
        reading=reading,
        rows=rows,
        error=error,
    )


@router.get("/than-so-hoc", response_class=HTMLResponse)
async def tsh_page() -> HTMLResponse:
    return _tsh_render({})


@router.post("/than-so-hoc", response_class=HTMLResponse)
async def tsh_submit(request: Request) -> HTMLResponse:
    form = await _form(request)
    try:
        birth = site_data.parse_date(form.get("ngay", ""))
        reading = site_data.numerology_reading(birth, form.get("ten", "").strip())
    except InputError as exc:
        return _tsh_render(form, error=exc.message)
    return _tsh_render(form, reading)


@router.get("/static/{name}")
async def static_file(name: str) -> Response:
    media_type = _STATIC_FILES.get(name)
    if media_type is None:
        return Response(status_code=404)
    return FileResponse(
        _STATIC / name,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


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
