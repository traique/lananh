import pytest

from channels import zalo_session


@pytest.mark.asyncio
async def test_load_account_id_from_current_session(monkeypatch):
    async def fake_load_session():
        return {"accountId": "84901234567", "cookie": []}

    monkeypatch.setattr(zalo_session, "load_session", fake_load_session)

    assert await zalo_session.load_account_id() == "84901234567"


@pytest.mark.asyncio
async def test_load_account_id_is_empty_without_session(monkeypatch):
    async def fake_load_session():
        return None

    monkeypatch.setattr(zalo_session, "load_session", fake_load_session)

    assert await zalo_session.load_account_id() == ""
