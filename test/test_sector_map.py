"""Test tính toàn vẹn của SECTOR_MAP.

SECTOR_MAP vừa là bản đồ ngành, vừa là nguồn sinh ALL_KNOWN_SYMBOLS - danh
sách mã mà stock_analysis coi là đã biết chắc. Sai sót ở đây lan sang tầng
nhận diện mã, nên bắt bằng test thay vì bằng mắt.
"""

import re

from stock import analysis as stock_analysis
from stock import sector as stock_sector

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,4}$")


def test_every_sector_has_label_and_symbols():
    for key, meta in stock_sector.SECTOR_MAP.items():
        assert meta.get("label"), f"ngành {key} thiếu label"
        assert meta.get("symbols"), f"ngành {key} không có mã nào"


def test_symbol_format_is_valid():
    for key, meta in stock_sector.SECTOR_MAP.items():
        for sym in meta["symbols"]:
            assert _SYMBOL_RE.match(sym), f"mã {sym!r} trong ngành {key} sai định dạng"


def test_no_duplicate_symbol_within_a_sector():
    """Trùng trong CÙNG một ngành là lỗi gõ; trùng giữa các ngành thì hợp lệ
    (vd REE ở cả industrial lẫn utilities)."""
    for key, meta in stock_sector.SECTOR_MAP.items():
        symbols = meta["symbols"]
        assert len(symbols) == len(set(symbols)), f"ngành {key} có mã trùng lặp"


def test_no_symbol_collides_with_excluded_words():
    """Rào quan trọng nhất của file này.

    Token nằm trong _COMMON_WORD_EXCLUDE / _LOWERCASE_NOISE_EXCLUDE là từ thông
    dụng; một mã trùng từ đó (vd SAN, HAI) mà thêm suông vào SECTOR_MAP sẽ bị
    nhận ở MỌI câu có từ ấy - bot trả giá cổ phiếu cho câu chẳng liên quan. Mã
    kiểu đó phải khai báo quy tắc ngữ cảnh riêng: _AMBIGUOUS_KNOWN (GAS, OIL)
    hoặc _ACRONYM_SYMBOLS (từ viết tắt vốn viết HOA, vd CEO - C.E.O Group).
    """
    excluded = stock_analysis._COMMON_WORD_EXCLUDE | stock_analysis._LOWERCASE_NOISE_EXCLUDE
    collisions = {
        sym
        for sym in stock_sector.ALL_KNOWN_SYMBOLS
        if sym in excluded and sym not in stock_analysis._AMBIGUOUS_KNOWN
    }
    assert not collisions, (
        f"mã trùng từ thông dụng nhưng chưa khai báo _AMBIGUOUS_KNOWN: {sorted(collisions)}"
    )


def test_ambiguous_known_symbols_exist_in_map():
    """_AMBIGUOUS_KNOWN chỉ có nghĩa với mã thật sự nằm trong bản đồ."""
    for sym in stock_analysis._AMBIGUOUS_KNOWN:
        assert sym in stock_sector.ALL_KNOWN_SYMBOLS, (
            f"{sym} khai ambiguous nhưng không có trong SECTOR_MAP"
        )


def test_gvr_is_covered():
    """Ca gây lỗi 30/07/2026: GVR không có trong bản đồ nên phần bình luận
    ngành trả rỗng và mã bị xếp vào nhóm chưa xác minh."""
    assert "GVR" in stock_sector.ALL_KNOWN_SYMBOLS
    assert "rubber" in stock_sector.get_symbol_sectors("GVR")
    assert stock_sector.get_primary_sector_label("GVR") == "Cao su & Săm lốp"


def test_newly_added_sectors_are_reachable():
    for key in (
        "rubber",
        "chemicals",
        "insurance",
        "aviation",
        "textile",
        "pharma",
        "materials",
        "seafood",
    ):
        assert key in stock_sector.SECTOR_MAP


def test_unknown_symbol_falls_back_to_khac():
    assert stock_sector.get_symbol_sectors("ZZZZ") == []
    assert stock_sector.get_primary_sector_label("ZZZZ") == "Khác"


def test_lookup_is_case_insensitive():
    assert stock_sector.get_symbol_sectors("gvr") == stock_sector.get_symbol_sectors("GVR")
    assert stock_sector.get_symbol_sectors("  gvr  ") == stock_sector.get_symbol_sectors("GVR")


def test_acronym_symbols_exist_in_map():
    """_ACRONYM_SYMBOLS (vd CEO) được nhận thẳng vào nhóm "đã biết" nên phải là mã thật
    có trong bản đồ, và không được nằm trong danh sách từ bị loại."""
    excluded = stock_analysis._COMMON_WORD_EXCLUDE | stock_analysis._LOWERCASE_NOISE_EXCLUDE
    for sym in stock_analysis._ACRONYM_SYMBOLS:
        assert sym in stock_sector.ALL_KNOWN_SYMBOLS, (
            f"{sym} khai acronym nhưng không có trong SECTOR_MAP"
        )
        assert sym not in excluded
    assert stock_sector.get_primary_sector_label("CEO") == "Bất động sản"
