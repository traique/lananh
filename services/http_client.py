"""HTTP client dùng chung cho các API cố định (Facebook Graph, Zoom, DNSE, QuickChart).

Trước đây mỗi lần gọi tạo một ``httpx.AsyncClient`` mới rồi đóng ngay: mỗi
lượt phải bắt tay TCP + TLS lại từ đầu, rất tốn CPU trên Render free (~0,1 CPU).
Client dùng chung giữ kết nối keep-alive tới các host này.

``scoped(timeout=..., headers=...)`` trả một lớp bọc mỏng: mọi request đi qua
client chung nhưng tự áp timeout/header riêng của nơi gọi, nên code cũ chỉ đổi
dòng ``async with httpx.AsyncClient(...) as client`` mà giữ nguyên hành vi.

Không dùng cho URL tuỳ ý người dùng nhập (kiểm tra link /gia): chỗ đó có lớp
chống SSRF riêng trong services/public_http.py.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

# Giới hạn nhỏ để giữ RAM thấp; đủ cho vài request song song của 1 người dùng.
_LIMITS = httpx.Limits(max_connections=10, max_keepalive_connections=4, keepalive_expiry=30.0)
_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=15.0)

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, limits=_LIMITS)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class ScopedClient:
    """Bọc client chung với timeout/header mặc định riêng của một nơi gọi."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        timeout: Any = None,
        headers: dict[str, str] | None = None,
    ):
        self._client = client
        self._timeout = timeout
        self._headers = headers or {}

    def _prepare(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        if self._timeout is not None:
            kwargs.setdefault("timeout", self._timeout)
        if self._headers:
            kwargs["headers"] = {**self._headers, **(kwargs.get("headers") or {})}
        return kwargs

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return await self._client.request(method, url, **self._prepare(kwargs))

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)


@asynccontextmanager
async def scoped(
    timeout: Any = None, headers: dict[str, str] | None = None
) -> AsyncIterator[ScopedClient]:
    """Dùng thay ``async with httpx.AsyncClient(timeout=..., headers=...) as client``.

    Không đóng client khi thoát khối: kết nối được giữ lại cho lượt sau.
    """
    yield ScopedClient(get_client(), timeout=timeout, headers=headers)
