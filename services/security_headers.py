"""Header bảo mật cho mọi response HTTP (web công khai, /admin, API).

Middleware ASGI thuần (không qua BaseHTTPMiddleware để không đệm body, không
đụng streaming): chỉ chèn header vào message ``http.response.start``.

- ``X-Content-Type-Options: nosniff``: trình duyệt không đoán lại kiểu file.
- ``Referrer-Policy: strict-origin-when-cross-origin``: link ra ngoài chỉ lộ
  tên miền, không lộ đường dẫn/query (vd /admin?...).
- ``X-Frame-Options: DENY`` + CSP ``frame-ancestors 'none'``: không trang nào
  nhúng site vào iframe (chống clickjacking trang admin). An toàn vì bot
  không dùng Telegram WebApp/iframe nào.
- ``Strict-Transport-Security`` (không preload): Render KHÔNG tự thêm HSTS;
  trình duyệt chỉ ghi nhận header này qua HTTPS nên chạy local http vô hại.

Header nào app đã tự đặt thì giữ nguyên, không ghi đè. Tắt HSTS qua env
SECURITY_HSTS=0 nếu cần (vd tên miền riêng chưa có HTTPS).
"""

from __future__ import annotations

import os

_HSTS_VALUE = b"max-age=31536000; includeSubDomains"


def _base_headers() -> list[tuple[bytes, bytes]]:
    headers = [
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (b"x-frame-options", b"DENY"),
        (b"content-security-policy", b"frame-ancestors 'none'"),
    ]
    if os.getenv("SECURITY_HSTS", "1").strip().lower() not in {"0", "false", "no", "off"}:
        headers.append((b"strict-transport-security", _HSTS_VALUE))
    return headers


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app
        self.headers = _base_headers()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                existing = list(message.get("headers") or [])
                present = {name.lower() for name, _ in existing}
                extra = [(name, value) for name, value in self.headers if name not in present]
                if extra:
                    message = {**message, "headers": existing + extra}
            await send(message)

        await self.app(scope, receive, send_with_headers)
