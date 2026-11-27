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
- ``Permissions-Policy``: tắt camera, micro, vị trí, thanh toán, USB cho mọi trang trừ
  đúng chỗ cần: micro cho /admin/avatar (nói chuyện bằng giọng), vị trí cho /thoi-tiet
  (nút "Lấy vị trí hiện tại"). Không trang nào dùng camera.
- ``Cache-Control: no-store`` cho /admin/api/* (khoá API đã che, thống kê, trí nhớ
  người dùng): không bộ đệm nào - kể cả đĩa của trình duyệt - giữ lại.

Header nào app đã tự đặt thì giữ nguyên, không ghi đè. Tắt HSTS qua env
SECURITY_HSTS=0 nếu cần (vd tên miền riêng chưa có HTTPS).
"""

from __future__ import annotations

import os

_HSTS_VALUE = b"max-age=31536000; includeSubDomains"
_FEATURES = ("camera", "microphone", "geolocation", "payment", "usb")
# Đường dẫn (khớp đúng hoặc là thư mục cha) -> tính năng được dùng trên chính trang đó.
_ALLOWED_FEATURES = {
    "/admin/avatar": ("microphone",),
    "/thoi-tiet": ("geolocation",),
}
_NO_STORE_PREFIX = "/admin/api/"


def _policy(allowed: tuple[str, ...] = ()) -> bytes:
    parts = [f"{name}=({'self' if name in allowed else ''})" for name in _FEATURES]
    return ", ".join(parts).encode()


_DEFAULT_POLICY = _policy()
_PATH_POLICIES = {path: _policy(features) for path, features in _ALLOWED_FEATURES.items()}


def permissions_policy(path: str) -> bytes:
    for prefix, policy in _PATH_POLICIES.items():
        if path == prefix or path.startswith(prefix + "/"):
            return policy
    return _DEFAULT_POLICY


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
        path = scope.get("path", "")
        wanted = [*self.headers, (b"permissions-policy", permissions_policy(path))]
        if path.startswith(_NO_STORE_PREFIX):
            wanted.append((b"cache-control", b"no-store"))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                existing = list(message.get("headers") or [])
                present = {name.lower() for name, _ in existing}
                extra = [(name, value) for name, value in wanted if name not in present]
                if extra:
                    message = {**message, "headers": existing + extra}
            await send(message)

        await self.app(scope, receive, send_with_headers)
