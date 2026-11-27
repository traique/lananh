"""Cache nhỏ của nhánh cổ phiếu: thống kê backtest theo mtime file, bối cảnh ngành
20 phút (kết quả rỗng chỉ 3 phút)."""

import json
import os

import pytest

from stock import backtest, sector


def test_thong_ke_backtest_chi_doc_lai_khi_file_doi(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest, "_setup_stats_cache", None)
    path = tmp_path / "stats.json"
    path.write_text(json.dumps({"pullback": {"n": 40}}), encoding="utf-8")
    first = backtest.load_setup_stats(path)
    assert first == {"pullback": {"n": 40}}
    stat = path.stat()
    # Đổi nội dung nhưng giữ nguyên mtime: vẫn trả bản đã cache (không đọc đĩa).
    path.write_text(json.dumps({"pullback": {"n": 99}}), encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert backtest.load_setup_stats(path) == {"pullback": {"n": 40}}
    # mtime đổi -> đọc lại.
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert backtest.load_setup_stats(path) == {"pullback": {"n": 99}}
    # Caller sửa dict trả về không làm hỏng cache.
    backtest.load_setup_stats(path).clear()
    assert backtest.load_setup_stats(path) == {"pullback": {"n": 99}}


def test_thong_ke_backtest_thieu_hoac_hong_thi_rong(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest, "_setup_stats_cache", None)
    assert backtest.load_setup_stats(tmp_path / "khong-co.json") == {}
    broken = tmp_path / "hong.json"
    broken.write_text("{rác", encoding="utf-8")
    assert backtest.load_setup_stats(broken) == {}
    listing = tmp_path / "list.json"
    listing.write_text("[1, 2]", encoding="utf-8")
    assert backtest.load_setup_stats(listing) == {}
    assert backtest.format_setup_stats_line("pullback", backtest.load_setup_stats(listing)) is None


@pytest.mark.asyncio
async def test_boi_canh_nganh_cache_20_phut_rong_chi_3_phut(monkeypatch):
    calls = []
    result = {"value": None}

    async def fake_build(keys):
        calls.append(tuple(keys))
        return result["value"]

    monkeypatch.setattr(sector, "_build_sector_context_uncached", fake_build)
    monkeypatch.setattr(sector, "_sector_cache", {})

    def age(seconds):
        key = ("steel",)
        ts, value = sector._sector_cache[key]
        sector._sector_cache[key] = (ts - seconds, value)

    assert await sector.build_sector_context(["steel"]) is None
    await sector.build_sector_context(["steel"])
    assert len(calls) == 1
    age(sector._SECTOR_NEGATIVE_TTL + 1)  # rỗng: hết 3 phút là lấy lại
    result["value"] = sector.SectorContext([], [], [], "Dòng tiền chưa rõ")
    assert await sector.build_sector_context(["steel"]) is result["value"]
    assert len(calls) == 2
    age(sector._SECTOR_NEGATIVE_TTL + 1)  # có dữ liệu: vẫn dùng cache sau 3 phút
    await sector.build_sector_context(["steel"])
    assert len(calls) == 2
    assert sector._SECTOR_CACHE_TTL == 20 * 60
    age(sector._SECTOR_CACHE_TTL)
    await sector.build_sector_context(["steel"])
    assert len(calls) == 3
