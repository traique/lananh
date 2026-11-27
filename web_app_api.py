"""API cho app Android màn hình thông tin (android/).

- ``POST /api/v1/app/pair``: ``{"code": "K7QF-3MXA"}`` (mã từ lệnh /app moi) ->
  ``{"token": "la_...", "device": {"id", "name"}}``. Mã dùng 1 lần.
- ``GET /api/v1/app/dashboard[?place=Hà Nội]``: header ``Authorization: Bearer la_...``
  -> JSON của services/app_dashboard.py (lịch, thời tiết, vàng, xăng, tin, watchlist).
  Kèm ETag yếu theo nội dung (bỏ ``generatedAt``): app gửi lại ``If-None-Match``, dữ liệu
  không đổi thì nhận 304 không body. Một IP gửi token sai quá nhiều lần -> 429 +
  Retry-After (services/app_devices.py).

Lỗi cùng dạng API công khai: ``{"error": {"code", "message"}}``. Dữ liệu riêng
(watchlist) nên trả ``Cache-Control: no-store``; JSON nén gzip ở web.CompressionMiddleware
(HttpURLConnection của Android tự gửi ``Accept-Encoding: gzip`` và tự giải nén).
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import web_admin
from services import app_dashboard, app_devices
from web_site import etag_matches, weak_etag

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/app", include_in_schema=False)
_MAX_BODY_BYTES = 1024
_NO_STORE = {"Cache-Control": "no-store"}


def _error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status,
        headers={**_NO_STORE, **(headers or {})},
    )


def _dumps(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str).encode()


def _json(data: dict, headers: dict | None = None) -> Response:
    return Response(
        _dumps(data), media_type="application/json", headers={**_NO_STORE, **(headers or {})}
    )


def _dashboard_etag(payload: dict) -> str:
    """ETag theo nội dung, bỏ ``generatedAt`` (đổi mỗi lần dựng lại bản cache 60 giây dù
    số liệu y nguyên; app không dùng trường này)."""
    return weak_etag(_dumps({k: v for k, v in payload.items() if k != "generatedAt"}))


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
    return _json({"token": token, "device": {"id": device.id, "name": device.name}})


@router.get("/dashboard")
async def dashboard(request: Request, place: str = "") -> Response:
    try:
        device = await app_devices.authenticate(
            request.headers.get("authorization", ""), web_admin.client_ip(request)
        )
    except app_devices.AuthLocked as exc:
        return _error(
            429,
            "TOO_MANY_FAILURES",
            "Gửi mã thiết bị sai quá nhiều lần, thử lại sau ít phút.",
            {"Retry-After": str(exc.retry_after)},
        )
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
    payload = {**data, "device": {"id": device.id, "name": device.name}}
    etag = _dashboard_etag(payload)
    if etag_matches(request.headers.get("if-none-match", ""), etag):
        return Response(status_code=304, headers={**_NO_STORE, "ETag": etag})
    return _json(payload, {"ETag": etag})
