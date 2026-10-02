"""Read public HTTP headers without following unchecked redirects or loading bodies."""

import asyncio
from urllib.parse import urljoin

import httpx

from services.web_reader import WebReaderError, normalize_public_http_url


async def public_status(client: httpx.AsyncClient, url: str, *, method: str, timeout: float) -> int:
    current = await asyncio.to_thread(normalize_public_http_url, url)
    for _ in range(6):
        async with client.stream(
            method, current, timeout=timeout, follow_redirects=False
        ) as response:
            status = response.status_code
            if status not in {301, 302, 303, 307, 308}:
                return status
            location = response.headers.get("location")
            if not location:
                return status
            current = await asyncio.to_thread(normalize_public_http_url, urljoin(current, location))
    raise WebReaderError("Link chuyển hướng quá nhiều lần.")
