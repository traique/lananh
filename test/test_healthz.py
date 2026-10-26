"""/healthz (dùng cho UptimeRobot cảnh báo) phải trả 503 khi DB không phản hồi và
200 khi DB ổn; GET / (Render health check) không chạm DB."""

import asyncio
import json

import httpx
import pytest

import web


class _Pool:
    def __init__(self, error: BaseException | None = None, delay: float = 0.0):
        self.error = error
        self.delay = delay

    async def fetchval(self, sql):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return 1


@pytest.mark.asyncio
async def test_healthz_ok_khi_db_tra_loi(monkeypatch):
    async def get_pool():
        return _Pool()

    monkeypatch.setattr(web.db, "get_pool", get_pool)
    response = await web.healthz()
    assert response.status_code == 200
    assert json.loads(bytes(response.body)) == {"status": "ok", "db": "ok"}


@pytest.mark.asyncio
async def test_healthz_503_khi_db_loi(monkeypatch):
    async def get_pool():
        return _Pool(OSError("connection refused"))

    monkeypatch.setattr(web.db, "get_pool", get_pool)
    response = await web.healthz()
    assert response.status_code == 503
    assert json.loads(bytes(response.body)) == {"status": "degraded", "db": "down"}


@pytest.mark.asyncio
async def test_healthz_503_khi_db_treo(monkeypatch):
    async def get_pool():
        return _Pool(delay=10)

    real_wait_for = asyncio.wait_for

    async def fast_wait_for(awaitable, **kwargs):
        return await real_wait_for(awaitable, timeout=min(kwargs["timeout"], 0.05))

    monkeypatch.setattr(web.db, "get_pool", get_pool)
    monkeypatch.setattr(web.asyncio, "wait_for", fast_wait_for)
    response = await web.healthz()
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_root_health_khong_cham_db(monkeypatch):
    async def get_pool():
        raise AssertionError("GET / không được chạm DB")

    monkeypatch.setattr(web.db, "get_pool", get_pool)
    transport = httpx.ASGITransport(app=web.api)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.get("/", headers={"Accept": "application/json"})
    assert response.status_code == 200 and response.json() == {"status": "ok"}
