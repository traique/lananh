"""Hoàn tất xoay khoá SETTINGS_ENC_KEY: mã hoá lại giá trị còn ở khoá cũ bằng
compare-and-swap (ghi đồng thời của gateway Zalo thắng), đếm giá trị không giải
mã được, báo chủ bot khi kết quả đổi."""

import pytest
from cryptography.fernet import Fernet, MultiFernet

from services import key_rotation

OLD, NEW = Fernet.generate_key(), Fernet.generate_key()


def _enc(key: bytes, text: str) -> str:
    return "enc:" + Fernet(key).encrypt(text.encode()).decode()


@pytest.fixture
def table(monkeypatch):
    rows: dict[str, str] = {}
    store: dict[str, str] = {}
    sent: list[str] = []
    race: dict[str, str] = {}

    async def list_encrypted_settings(limit=500):
        return sorted((k, v) for k, v in rows.items() if v.startswith("enc:"))[:limit]

    async def compare_and_set_setting(key, expected, value):
        if key in race:  # gateway ghi session mới ngay trước câu UPDATE
            rows[key] = race.pop(key)
        if rows.get(key) != expected:
            return False
        rows[key] = value
        return True

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        store[key] = value

    monkeypatch.setattr(key_rotation.db, "list_encrypted_settings", list_encrypted_settings)
    monkeypatch.setattr(key_rotation.db, "compare_and_set_setting", compare_and_set_setting)
    monkeypatch.setattr(key_rotation.db, "get_setting", get_setting)
    monkeypatch.setattr(key_rotation.db, "set_setting", set_setting)
    monkeypatch.setattr(key_rotation.alerts, "send", lambda text, **kw: sent.append(text) or True)
    monkeypatch.setattr(key_rotation.crypto, "_fernet", MultiFernet([Fernet(NEW), Fernet(OLD)]))
    monkeypatch.setattr(key_rotation.config, "SETTINGS_ENC_KEY_OLD", OLD.decode())
    monkeypatch.delenv("SETTINGS_AUTO_ROTATE", raising=False)
    return rows, store, sent, race


@pytest.mark.asyncio
async def test_scan_dem_khong_ghi(table):
    rows, _, _, _ = table
    rows.update(
        {
            "provider_api_key_groq": _enc(OLD, "gsk-1"),
            "provider_api_key_tavily": _enc(NEW, "tvly-1"),
            "zalo:session:v1": _enc(Fernet.generate_key(), "hỏng"),
            "router9_model": "gpt",
        }
    )
    before = dict(rows)
    report = await key_rotation.scan()
    assert (report.total, report.remaining, report.unreadable) == (3, 1, 1)
    assert report.keys_old == ("provider_api_key_groq",)
    assert report.keys_unreadable == ("zalo:session:v1",)
    assert rows == before


@pytest.mark.asyncio
async def test_rotate_all_ma_hoa_lai_bang_khoa_moi(table):
    rows, _, _, _ = table
    rows["provider_api_key_groq"] = _enc(OLD, "gsk-1")
    rows["provider_api_key_agnes"] = _enc(OLD, "ag-1")
    report = await key_rotation.rotate_all()
    assert (report.rotated, report.remaining, report.conflicts) == (2, 0, 0)
    for key in ("provider_api_key_groq", "provider_api_key_agnes"):
        assert Fernet(NEW).decrypt(rows[key][4:].encode())  # chỉ khoá mới là đủ
    assert key_rotation.crypto.decrypt(rows["provider_api_key_groq"]) == "gsk-1"


@pytest.mark.asyncio
async def test_ghi_dong_thoi_thi_gia_tri_moi_thang(table):
    rows, _, _, race = table
    rows["zalo:session:v1"] = _enc(OLD, "session-cu")
    fresh = _enc(NEW, "session-moi")
    race["zalo:session:v1"] = fresh
    report = await key_rotation.rotate_all()
    assert (report.rotated, report.conflicts, report.remaining) == (0, 1, 0)
    assert rows["zalo:session:v1"] == fresh  # không bị ghi đè bằng bản xoay từ giá trị cũ


@pytest.mark.asyncio
async def test_khoi_dong_tu_xoay_va_chi_bao_khi_ket_qua_doi(table):
    rows, _, sent, _ = table
    rows["provider_api_key_groq"] = _enc(OLD, "gsk-1")
    report = await key_rotation.on_startup()
    assert report is not None and report.rotated == 1
    assert "mã hoá lại 1" in sent[-1] and "xoá SETTINGS_ENC_KEY_OLD" in sent[-1]
    await key_rotation.on_startup()  # restart: không còn gì đổi -> không báo lại
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_tat_tu_xoay_thi_chi_bao_so_con_lai(table, monkeypatch):
    rows, _, sent, _ = table
    monkeypatch.setenv("SETTINGS_AUTO_ROTATE", "0")
    rows["provider_api_key_groq"] = _enc(OLD, "gsk-1")
    report = await key_rotation.on_startup()
    assert report is not None and report.rotated == 0 and report.remaining == 1
    assert rows["provider_api_key_groq"].startswith("enc:")
    assert key_rotation.crypto.needs_rotation(rows["provider_api_key_groq"])
    assert "Xoay khoá ngay" in sent[-1]


@pytest.mark.asyncio
async def test_khong_co_khoa_cu_thi_bo_qua(table, monkeypatch):
    _, _, sent, _ = table
    monkeypatch.setattr(key_rotation.config, "SETTINGS_ENC_KEY_OLD", None)
    assert await key_rotation.on_startup() is None
    assert sent == []


def test_report_text_khong_chua_gia_tri():
    report = key_rotation.RotationReport(3, 0, 0, 0, 1, 1, ("provider_api_key_groq",), ("x",))
    text = key_rotation.report_text(report, auto=True)
    assert "provider_api_key_groq" in text and "enc:" not in text
