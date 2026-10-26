"""Bound request allocations before JSON/base64 decoding on a 512 MB worker.

Cờ "heavy" chỉ bảo vệ bước ĐỌC body lớn vào RAM (mỗi lúc tối đa 1 body lớn
đang được gom). Cờ được nhả ngay khi đọc xong body, TRƯỚC khi app xử lý - nếu
giữ suốt thời gian xử lý (gọi AI 1-2 phút) thì mọi request lớn khác, kể cả
đăng nhập admin, đều nhận 503 oan. Request nhỏ (đăng nhập, JSON admin vài KB)
không bao giờ bị coi là heavy.

Ngoài ra tổng số byte body lớn ĐANG ĐƯỢC XỬ LÝ (đã đọc xong, app còn giải mã
JSON/base64, nén ảnh...) bị chặn bởi ngân sách REQUEST_HEAVY_BUDGET_MB (mặc định
30 MB, đủ 1 bài Facebook 22 MB + 1 ảnh 8 MB cùng lúc): vượt ngân sách thì trả
503 + Retry-After thay vì để nhiều body lớn giải mã song song làm tràn 512 MB.
Ngân sách giữ theo số byte thực đọc được nên ảnh nhỏ không chiếm chỗ oan.
"""

import os

from fastapi.responses import JSONResponse

MIB = 1024 * 1024


def _heavy_budget() -> int:
    try:
        mb = int(os.getenv("REQUEST_HEAVY_BUDGET_MB", "30"))
    except ValueError:
        mb = 30
    # Tối thiểu 22 MB để 1 bài Facebook lớn nhất luôn lọt khi không có gì khác.
    return max(22, min(256, mb)) * MIB


# Body khai báo trên ngưỡng này ở /admin/* mới coi là heavy (upload); form đăng
# nhập và JSON cấu hình chỉ vài KB.
_ADMIN_HEAVY_THRESHOLD = 256 * 1024
# Tin thoại (/voice) và file tài liệu (/file) từ zalo-gateway: body thô <= 8 MB
# như ảnh (service tự chặn chặt hơn: VOICE_MAX_BYTES, DOCUMENT_MAX_BYTES).
_MEDIA_PATHS = (
    "/facebook-group-post",
    "/image-prompt",
    "/internal/zalo/voice",
    "/internal/zalo/file",
)


def _limits(path: str, declared: int) -> tuple[int, bool]:
    """(giới hạn byte, có phải body lớn cần tuần tự hoá khi đọc)."""
    if path.endswith("/facebook-group-post"):
        return 22 * MIB, True
    if path.endswith(_MEDIA_PATHS):
        return 8 * MIB, True
    if path.startswith("/admin/"):
        return 8 * MIB, declared > _ADMIN_HEAVY_THRESHOLD
    return MIB, False


class RequestLimitsMiddleware:
    def __init__(self, app):
        self.app = app
        self.heavy_active = False
        # Tổng byte body lớn đang được app xử lý (đã đặt chỗ trong ngân sách).
        self.heavy_inflight = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        path = scope["path"]
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "Invalid Content-Length"}, 400)(
                scope, receive, send
            )
        limit, heavy = _limits(path, declared)
        if declared > limit:
            return await JSONResponse({"detail": "Request too large"}, 413)(scope, receive, send)
        # Đặt chỗ theo Content-Length (thiếu thì theo giới hạn tối đa), chỉnh lại
        # theo số byte thực đọc được ngay sau khi đọc xong.
        reserved = (declared or limit) if heavy else 0
        # No await between checking and claiming: one large body read at a time,
        # và tổng body lớn đang xử lý không vượt ngân sách.
        if heavy and (self.heavy_active or self.heavy_inflight + reserved > _heavy_budget()):
            return await JSONResponse(
                {"detail": "Media worker busy; retry shortly"}, 503, headers={"Retry-After": "5"}
            )(scope, receive, send)
        if heavy:
            self.heavy_active = True
            self.heavy_inflight += reserved
        try:
            try:
                body = await self._read_body(receive, limit)
            finally:
                if heavy:
                    self.heavy_active = False
            if body is None:
                return await JSONResponse({"detail": "Request too large"}, 413)(
                    scope, receive, send
                )
            if body is False:
                return  # client đã ngắt kết nối giữa chừng
            if heavy:
                self.heavy_inflight += len(body) - reserved
                reserved = len(body)
            await self._run_app(scope, receive, send, body)
        finally:
            if heavy:
                self.heavy_inflight -= reserved

    async def _run_app(self, scope, receive, send, body: bytes) -> None:
        """Chuyển body đã gom cho app như 1 message duy nhất."""
        consumed = False

        async def bounded_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)

    @staticmethod
    async def _read_body(receive, limit: int):
        """Gom body; None nếu vượt giới hạn, False nếu client ngắt kết nối."""
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return False
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                return None
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        chunks.clear()
        return body
