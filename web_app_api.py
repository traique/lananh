"""API cho app Android màn hình thông tin (android/).

- ``POST /api/v1/app/pair``: ``{"code": "K7QF-3MXA"}`` (mã từ lệnh /app moi) ->
  ``{"token": "la_...", "device": {"id", "name"}}``. Mã dùng 1 lần.
- ``GET /api/v1/app/dashboard[?place=Hà Nội]``: header ``Authorization: Bearer la_...``
  -> JSON của services/app_dashboard.py (lịch, thời tiết, vàng, xăng, tin, watchlist).

Lỗi cùng dạng API công khai: ``{"error": {"code", "message"}}``. Dữ liệu riêng
(watchlist) nên trả ``Cache-Control: no-store``; JSON nén gzip khi client nhận
(HttpURLConnection của Android tự gửi ``Accept-Encoding: gzip`` và tự giải nén).
"""

from __future__ import annotations

import gzip
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from services import app_dashboard, app_devices

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/app", include_in_schema=False)
_MAX_BODY_BYTES = 1024
_GZIP_MIN_BYTES = 1024


def _error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status,
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


def _json(request: Request, data: dict) -> Response:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    headers = {"Cache-Control": "no-store", "Vary": "Accept-Encoding"}
    if len(body) >= _GZIP_MIN_BYTES and "gzip" in request.headers.get("accept-encoding", ""):
        body = gzip.compress(body, compresslevel=6)
        headers["Content-Encoding"] = "gzip"
    return Response(body, media_type="application/json", headers=headers)


@router.post("/pair")
async def pair(request: Request) -> Response:
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        return _error(413, "BODY_TOO_LARGE", "Body tối đa 1 KB.")
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return _error(422, "INVALID_JSON", "Body phải là JSON.")
    code = data.get("code") if isinstance(data, dict) else None
    if not isinstance(code, str):
        return _error(422, "INVALID_CODE", "Cần trường code (mã ghép từ lệnh /app moi).")
    try:
        token, device = await app_devices.pair(code)
    except app_devices.PairError as exc:
        return _error(exc.status, exc.code, exc.message)
    except Exception:
        logger.warning("app api: ghép thiết bị lỗi (DB).", exc_info=True)
        return _error(503, "DB_UNAVAILABLE", "Máy chủ đang bận, thử lại sau ít phút.")
    return _json(request, {"token": token, "device": {"id": device.id, "name": device.name}})


@router.get("/dashboard")
async def dashboard(request: Request, place: str = "") -> Response:
    try:
        device = await app_devices.authenticate(request.headers.get("authorization", ""))
    except Exception:
        logger.warning("app api: không xác thực được thiết bị (DB lỗi).", exc_info=True)
        return _error(503, "DB_UNAVAILABLE", "Máy chủ đang bận, thử lại sau ít phút.")
    if device is None:
        return _error(
            401,
            "UNAUTHORIZED",
            "Thiết bị chưa ghép hoặc đã bị thu hồi. Gõ /app moi trên Telegram để lấy mã mới.",
            {"WWW-Authenticate": "Bearer"},
        )
    data = await app_dashboard.dashboard(device.user_id, place)
    return _json(request, {**data, "device": {"id": device.id, "name": device.name}})
