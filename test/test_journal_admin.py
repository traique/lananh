"""Trang /admin "Nhật ký" (services/journal_admin.py, web_admin /admin/api/journal*):
đăng nhập, chỉ người dùng hợp lệ, gom theo ngày giờ VN, phân trang có trần, sửa/xoá
trong khoá trí nhớ, POST chỉ nhận JSON, escape HTML phía trang."""

import asyncio
import datetime as dt
from pathlib import Path

import pytest

import web_admin
from _memory_fakes import FakeMemoryDB
from services import journal_admin, memory_service
from test_web_admin import _login, client, settings_store, sleeps  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
UTC = dt.timezone.utc


@pytest.fixture
def journal_db(monkeypatch, settings_store):  # noqa: F811
    fake = FakeMemoryDB()
    fake.install(monkeypatch, web_admin.db)
    # FakeMemoryDB thay get_setting/set_setting: giữ bảng settings của phiên admin.
    monkeypatch.setattr(web_admin.db, "get_setting", lambda key: _get(settings_store, key))
    monkeypatch.setattr(web_admin.db, "set_setting", lambda k, v: _set(settings_store, k, v))

    async def search(uid, query="", *, limit=20, offset=0):
        rows = [
            dict(n)
            for (u, _), n in fake.session_notes.items()
            if u == uid
            and (
                not query or query.lower() in f"{n['title']} {n['bullets']} {n['summary']}".lower()
            )
        ]
        rows.sort(key=lambda n: n["started_at"], reverse=True)
        return len(rows), rows[offset : offset + limit]

    async def summary_row(uid):
        text = fake.summary.get(uid)
        return (
            {"summary": text, "updated_at": dt.datetime(2026, 10, 26, tzinfo=UTC)} if text else None
        )

    async def entries():
        return [
            {"user_id": 42, "label": "Chủ bot (Telegram/Zoom)"},
            {"user_id": -5, "label": "<b>Lan</b>"},
        ]

    monkeypatch.setattr(web_admin.db, "search_session_notes", search)
    monkeypatch.setattr(web_admin.db, "get_summary_row", summary_row)
    monkeypatch.setattr(web_admin, "_memory_user_entries", entries)
    return fake


async def _get(store, key):
    return store.get(key)


async def _set(store, key, value):
    store[key] = value


async def _seed(fake):
    # 23:30 UTC ngày 25 = 6:30 sáng ngày 26 giờ VN.
    for key, title, when in (
        (1, "Bàn FPT", dt.datetime(2026, 10, 25, 23, 30, tzinfo=UTC)),
        (2, "Đi chợ", dt.datetime(2026, 10, 26, 3, 0, tzinfo=UTC)),
        (3, "Hỏi thời tiết", dt.datetime(2026, 10, 24, 2, 0, tzinfo=UTC)),
    ):
        await fake.upsert_session_note(42, key, title=title, bullets="- ý", started_at=when)
    await fake.upsert_session_note(-5, 9, title="Của Lan", started_at=dt.datetime.now(UTC))
    fake.summary[42] = "Anh làm PM."


@pytest.mark.asyncio
async def test_nhat_ky_can_dang_nhap_va_nguoi_dung_hop_le(client, journal_db):  # noqa: F811
    assert (await client.get("/admin/api/journal?user_id=42")).status_code == 403
    body = {"user_id": 42, "session_key": 1, "action": "delete"}
    assert (await client.post("/admin/api/journal/note", json=body)).status_code == 403
    await _login(client)
    for bad in ("abc", "7", ""):
        assert (await client.get(f"/admin/api/journal?user_id={bad}")).status_code == 400


@pytest.mark.asyncio
async def test_nhat_ky_gom_theo_ngay_vn_tom_tat_va_tim(client, journal_db):  # noqa: F811
    await _seed(journal_db)
    await _login(client)
    data = (await client.get("/admin/api/journal?user_id=42")).json()
    assert data["label"] == "Chủ bot (Telegram/Zoom)" and data["total"] == 3
    assert [(d["day"], [n["session_key"] for n in d["notes"]]) for d in data["days"]] == [
        ("2026-10-26", [2, 1]),
        ("2026-10-24", [3]),
    ]
    assert data["summary"]["text"] == "Anh làm PM."
    found = (await client.get("/admin/api/journal", params={"user_id": 42, "q": "fpt"})).json()
    assert found["total"] == 1 and found["summary"] is None
    # Dữ liệu tách người: Zalo -5 chỉ thấy ghi chú của mình.
    lan = (await client.get("/admin/api/journal?user_id=-5")).json()
    assert lan["label"] == "<b>Lan</b>" and lan["total"] == 1


@pytest.mark.asyncio
async def test_nhat_ky_phan_trang_co_tran(journal_db, monkeypatch):
    seen = []

    async def search(uid, query="", *, limit=20, offset=0):
        seen.append((query, limit, offset))
        return 5000, []

    async def no_summary(uid):
        return None

    monkeypatch.setattr(web_admin.db, "search_session_notes", search)
    monkeypatch.setattr(web_admin.db, "get_summary_row", no_summary)
    data = await journal_admin.page(42, "x" * 500, 999)
    assert data["page"] == journal_admin.MAX_PAGE and data["pages"] == journal_admin.MAX_PAGE
    assert seen == [("x" * 100, 20, (journal_admin.MAX_PAGE - 1) * 20)]
    assert (await journal_admin.page(42, "", -3))["page"] == 1


@pytest.mark.asyncio
async def test_nhat_ky_sua_xoa_chi_nhan_json(client, journal_db):  # noqa: F811
    await _seed(journal_db)
    await _login(client)
    url = "/admin/api/journal/note"
    edit = {
        "user_id": 42,
        "session_key": 1,
        "action": "edit",
        "title": "  Bàn FPT, HPG\x00 ",
        "bullets": "- giữ FPT\n- chờ HPG",
        "summary": "",
    }
    assert (
        await client.post(url, content="user_id=42", headers={"content-type": "text/plain"})
    ).status_code == 415
    assert (await client.post(url, json=edit)).json() == {"changed": True}
    note = journal_db.session_notes[(42, 1)]
    assert (note["title"], note["bullets"]) == ("Bàn FPT, HPG", "- giữ FPT\n- chờ HPG")
    assert (await client.post(url, json={**edit, "session_key": 99})).status_code == 404
    assert (await client.post(url, json={**edit, "action": "drop"})).status_code == 400
    assert (await client.post(url, json={**edit, "session_key": True})).status_code == 400
    assert (await client.post(url, json={**edit, "user_id": 7})).status_code == 400
    # Thành viên khác không xoá được ghi chú của chủ bot (session_key của người khác).
    other = {"user_id": -5, "session_key": 1, "action": "delete"}
    assert (await client.post(url, json=other)).status_code == 404
    assert (42, 1) in journal_db.session_notes
    delete = {"user_id": 42, "session_key": 1, "action": "delete"}
    assert (await client.post(url, json=delete)).json() == {"changed": True}
    assert (42, 1) not in journal_db.session_notes


@pytest.mark.asyncio
async def test_nhat_ky_ban_khoa_tri_nho_thi_409(client, journal_db, monkeypatch):  # noqa: F811
    await _seed(journal_db)
    await _login(client)
    monkeypatch.setattr(journal_admin, "LOCK_WAIT_SEC", 0.01)
    async with memory_service.user_lock(42):
        r = await client.post(
            "/admin/api/journal/note", json={"user_id": 42, "session_key": 1, "action": "delete"}
        )
    assert r.status_code == 409 and "thử lại" in r.json()["error"]
    await asyncio.sleep(0)


def test_trang_nhat_ky_escape_va_khong_tu_lam_moi():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert 'href="#journal"' in html and 'id="journal-box"' in html
    block = html[html.index("/* ---------- Nhật ký") : html.index("// Zalo: user_id âm")]
    # Mọi chữ từ server qua esc(); không ghép thẳng n.title/n.bullets/n.summary/label.
    for field in ("n.title", "n.bullets", "n.summary", "d.label", "u.label", "d.summary.text"):
        assert f"${{{field}}}" not in block, field
        assert f"esc({field}" in block, field
    assert "/admin/api/journal/note" in block and "window.confirm" in block
    # Không nằm trong vòng tự làm mới (đang sửa không bị đè).
    loaders = html[html.index("const LIVE_LOADERS") : html.index("let refreshing")]
    assert "loadJournal" not in loaders and "initJournal" not in loaders
