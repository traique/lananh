"""Danh sách địa điểm thời tiết trên web (services/weather_places.py)."""

import pytest

from services import weather_places as wp


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(wp, "_cache", None)


async def test_doc_db_loi_dung_mac_dinh_va_khong_ghi_de(monkeypatch):
    writes = []

    async def broken(key):
        raise RuntimeError("db down")

    async def set_setting(key, value):
        writes.append(value)

    monkeypatch.setattr(wp.db, "get_setting", broken)
    monkeypatch.setattr(wp.db, "set_setting", set_setting)
    assert await wp.get() == wp.DEFAULT_PLACES
    with pytest.raises(wp.PlacesError) as exc:
        await wp.add("Vũng Tàu")
    assert exc.value.code == "luu" and writes == []  # không đè danh sách thật bằng mặc định


async def test_tran_so_muc_va_ten_hop_le(monkeypatch):
    store = {}

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        store[key] = value

    monkeypatch.setattr(wp.db, "get_setting", get_setting)
    monkeypatch.setattr(wp.db, "set_setting", set_setting)
    for i in range(wp.MAX_PLACES - len(wp.DEFAULT_PLACES)):
        await wp.add(f"Nơi {i}")
    with pytest.raises(wp.PlacesError) as exc:
        await wp.add("Thêm nữa")
    assert exc.value.code == "day"
    for bad in ("", "   ", "x" * (wp.MAX_NAME_LEN + 1), "<b>", 'a"b'):
        with pytest.raises(wp.PlacesError):
            wp.clean_name(bad)
    assert await wp.remove("ha noi") == [p for p in await wp.get() if p != "Hà Nội"]


async def test_ghi_db_loi_bao_loi_khong_doi_cache(monkeypatch):
    async def get_setting(key):
        return None

    async def broken(key, value):
        raise RuntimeError("db down")

    monkeypatch.setattr(wp.db, "get_setting", get_setting)
    monkeypatch.setattr(wp.db, "set_setting", broken)
    with pytest.raises(wp.PlacesError) as exc:
        await wp.add("Vũng Tàu")
    assert exc.value.code == "luu" and "Vũng Tàu" not in await wp.get()
