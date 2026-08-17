"""Mỗi test ứng với MỘT lỗi thật đã từng được gửi cho người dùng.

Dữ liệu trong test lấy từ báo cáo GEX, FPT, CII ngày 05/08/2026.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock import report_format as rf
from stock import sector as stock_sector


def test_numbers_use_vietnamese_decimal_comma():
    """Báo cáo FPT trộn "-4,76%" với "60.5" trong cùng một tin nhắn."""
    assert rf.fmt_number(60.5, 1) == "60,5"
    assert rf.fmt_number(-4.76) == "-4,76"
    assert rf.fmt_price(24900) == "24.900"
    assert rf.fmt_signed_pct(2.51) == "+2,51%"


def test_level_always_carries_distance_pct():
    """Mọi mốc giá phải cho biết cách giá hiện tại bao xa."""
    text = rf.format_level(71500, 70370, 3)
    assert "70.370" in text
    assert "%" in text
    assert "3 lần test" in text


def test_far_levels_are_flagged_as_unusable():
    """GEX: kháng cự gần nhất cách +28% vẫn được coi là vùng chốt lời."""
    line = rf.nearest_levels_line(24850, [(19700, 1)], [(31900, 2)])
    assert "KHÔNG dùng làm điểm vào/ra ngắn hạn" in line


def test_level_inside_atr_noise_is_flagged():
    """FPT: hỗ trợ cách 1,58% trong khi ATR một phiên là 2,92%."""
    line = rf.nearest_levels_line(71500, [(70370, 3)], [(74000, 1)], atr_pct=2.92)
    assert "biên nhiễu một phiên" in line


def test_near_level_without_atr_conflict_has_no_warning():
    """Mốc vừa đủ gần và ngoài biên nhiễu thì không được cảnh báo oan."""
    line = rf.nearest_levels_line(71500, [(68000, 2)], [(74000, 1)], atr_pct=1.0)
    assert "biên nhiễu một phiên" not in line
    assert "KHÔNG dùng làm điểm vào/ra" not in line


def test_macd_is_expressed_as_pct_of_price():
    """CII: "MACD cắt lên nhẹ (+24.44)" trên giá 13.950 = 0,18% giá."""
    line = rf.macd_strength_line(24.44, 24.44, 13950)
    assert "%" in line
    assert "rất yếu" in line


def test_adx_line_must_state_direction():
    """ADX 43,7 của một mã đang giảm không được để hiểu là xu hướng tăng."""
    line = rf.adx_direction_line(53.1, 12.0, 30.0, True)
    assert "nghiêng GIẢM" in line
    up = rf.adx_direction_line(43.7, 31.0, 12.0, True)
    assert "nghiêng TĂNG" in up
    unavailable = rf.adx_direction_line(0.0, 0.0, 0.0, False, available=False)
    assert "chưa đủ dữ liệu" in unavailable


def test_news_title_must_mention_the_symbol():
    """FPT bị gán tin "tăng trần" vốn là tin của mã khác."""
    assert rf.title_mentions_symbol("FPT chốt quyền cổ phiếu thưởng", "FPT")
    assert not rf.title_mentions_symbol("Nhóm VN30 đồng loạt tăng trần", "FPT")
    assert not rf.title_mentions_symbol("Cổ phiếu FPTS hút tiền", "FPT")


def test_news_impact_ignores_unrelated_headlines():
    """Sentiment của tin không liên quan từng chảy vào khuyến nghị."""
    unrelated = [
        ("Thị trường chung đồng loạt tăng trần", 1.0),
        ("Họ Gelex hút dòng tiền", 1.0),
    ]
    assert rf.relevant_news_impact(unrelated, "FPT") == 0.0
    mixed = [
        ("FPT công bố kế hoạch cổ phiếu thưởng", 0.6),
        ("VN30 giảm sâu", -1.0),
    ]
    assert rf.relevant_news_impact(mixed, "FPT") > 0


def test_duplicate_disclaimer_is_removed():
    """Báo cáo FPT kết thúc bằng hai đoạn disclaimer lặp nhau."""
    text = (
        "Nội dung phân tích chính.\n\n"
        "Đây chỉ là tham khảo, không phải khuyến nghị đầu tư nha anh.\n\n"
        "Lưu ý: không phải khuyến nghị đầu tư, anh tự chịu trách nhiệm nhé."
    )
    out = rf.clean_analysis_output(text)
    assert out.count("không phải khuyến nghị") == 1
    assert "Nội dung phân tích chính." in out


def test_self_intro_and_pet_name_are_removed():
    """Mọi báo cáo đều mở bằng "em Lan Anh đây ạ", CII còn kết "anh yêu"."""
    text = "Anh ơi, em Lan Anh đây ạ! Cập nhật dữ liệu lúc 11:21 cho CII, anh yêu."
    out = rf.clean_analysis_output(text)
    assert "Lan Anh đây" not in out
    assert "anh yêu" not in out.lower()
    assert "Cập nhật dữ liệu" in out


def test_task_done_sentence_is_removed():
    """FPT: "Nhiệm vụ phân tích của em xong rồi đó ạ!" trước disclaimer."""
    text = "Kết luận chính.\n\nNhiệm vụ phân tích của em xong rồi đó ạ!\n"
    out = rf.clean_analysis_output(text)
    assert "Nhiệm vụ" not in out
    assert "Kết luận chính." in out


def test_long_paragraph_mentioning_reference_is_kept():
    """Không được xoá oan đoạn lập luận chỉ vì có chữ "tham khảo"."""
    long_para = (
        "Về mặt kỹ thuật, giá đang nằm trên SMA20 nhưng dưới SMA50, "
        "cho thấy xu hướng trung hạn vẫn chưa được xác nhận. Anh có thể "
        "tham khảo thêm diễn biến khối lượng ở vùng kháng cự gần nhất "
        "trước khi quyết định giải ngân, vì thanh khoản hiện chỉ đạt khoảng "
        "tám mươi phần trăm so với trung bình hai mươi phiên gần nhất và "
        "chưa cho thấy dòng tiền lớn quay trở lại một cách rõ ràng."
    )
    text = f"{long_para}\n\nĐây chỉ là tham khảo, không phải khuyến nghị."
    out = rf.clean_analysis_output(text)
    assert long_para in out


def test_cii_and_gex_no_longer_share_one_sector():
    """CII và GEX từng nhận cùng con số ngành -11,51%."""
    cii = set(stock_sector.get_symbol_sectors("CII"))
    gex = set(stock_sector.get_symbol_sectors("GEX"))
    assert cii
    assert gex
    assert not (cii & gex)


def test_industrial_park_symbols_are_separated_from_construction():
    """KBC (khu công nghiệp) và CII (hạ tầng) không cùng một rọ."""
    park = set(stock_sector.SECTOR_MAP["industrial_park"]["symbols"])
    construction = set(stock_sector.SECTOR_MAP["construction"]["symbols"])
    assert "KBC" in park
    assert "CII" in construction
    assert not (park & construction)


def test_no_symbol_was_lost_when_splitting_sectors():
    """Tách ngành không được làm rơi mã nào khỏi ALL_KNOWN_SYMBOLS."""
    previously_industrial = [
        "GEX",
        "CTD",
        "VCG",
        "REE",
        "CII",
        "KBC",
        "BCM",
        "SIP",
        "IDC",
        "HHV",
        "LCG",
        "FCN",
        "TCD",
    ]
    for symbol in previously_industrial:
        assert symbol in stock_sector.ALL_KNOWN_SYMBOLS
        assert stock_sector.get_symbol_sectors(symbol)
