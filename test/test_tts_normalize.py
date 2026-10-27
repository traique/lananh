"""Chuẩn hoá chữ trước khi đọc giọng /noi (services/tts_normalize.py + từ điển
assets/tts_viet_tat.yaml): số tiền, đơn vị, ngày giờ, viết tắt, mã cổ phiếu."""

import pytest

from services import tts_normalize as tn

CASES = [
    # Số tiền và đơn vị tiền
    ("giá 150k", "giá một trăm năm mươi nghìn"),
    ("150 k", "một trăm năm mươi nghìn"),
    ("cafe 45K", "cafe bốn mươi lăm nghìn"),
    ("1tr2", "một triệu hai trăm nghìn"),
    ("lãi 1tr250", "lãi một triệu hai trăm năm mươi nghìn"),
    ("5 triệu", "năm triệu"),
    ("1,5 triệu", "một triệu năm trăm nghìn"),
    ("3 tỷ", "ba tỷ"),
    ("2ty5", "hai tỷ năm trăm triệu"),
    ("1.299.000đ", "một triệu hai trăm chín mươi chín nghìn đồng"),
    ("1.299.000 đồng", "một triệu hai trăm chín mươi chín nghìn đồng"),
    ("24.900 VND", "hai mươi tư nghìn chín trăm đồng"),
    ("25,5 nghìn", "hai mươi lăm nghìn năm trăm"),
    ("10 USD", "mười đô la"),
    ("$5", "năm đô la"),
    ("VND", "đồng"),
    # Phần trăm, dấu, khoảng
    ("tăng 1,5%", "tăng một phẩy năm phần trăm"),
    ("+2,35%", "cộng hai phẩy ba mươi lăm phần trăm"),
    ("-1,2%", "âm một phẩy hai phần trăm"),
    ("±5%", "cộng trừ năm phần trăm"),
    ("10-20%", "mười đến hai mươi phần trăm"),
    ("▲ 12,5 điểm", "tăng mười hai phẩy năm điểm"),
    ("▼ 3 điểm", "giảm ba điểm"),
    # Số lớn, số thập phân, số đọc từng chữ số
    ("21", "hai mươi mốt"),
    ("24", "hai mươi tư"),
    ("15", "mười lăm"),
    ("105", "một trăm linh năm"),
    ("1005", "một nghìn không trăm linh năm"),
    ("1.500", "một nghìn năm trăm"),
    ("1234567", "một triệu hai trăm ba mươi tư nghìn năm trăm sáu mươi bảy"),
    ("1.000.000.000", "một tỷ"),
    ("2.500.000.000.000", "hai nghìn năm trăm tỷ"),
    ("3,14", "ba phẩy mười bốn"),
    ("0,05", "không phẩy không năm"),
    ("0901234567", "không chín không một hai ba bốn năm sáu bảy"),
    ("năm 2026", "năm hai nghìn không trăm hai mươi sáu"),
    # Ngày giờ
    ("26/10", "ngày hai mươi sáu tháng mười"),
    ("ngày 26/10", "ngày hai mươi sáu tháng mười"),
    (
        "ngày 26/10/2026",
        "ngày hai mươi sáu tháng mười năm hai nghìn không trăm hai mươi sáu",
    ),
    ("2026-10-27", "ngày hai mươi bảy tháng mười năm hai nghìn không trăm hai mươi sáu"),
    ("tháng 10/2026", "tháng mười năm hai nghìn không trăm hai mươi sáu"),
    ("8h30", "tám giờ ba mươi phút"),
    ("8h", "tám giờ"),
    ("15:05", "mười lăm giờ năm phút"),
    ("tỷ lệ 100:25", "tỷ lệ một trăm trên hai mươi lăm"),
    # Đơn vị đo
    ("5 km", "năm ki lô mét"),
    ("3kg", "ba ki lô gam"),
    ("50m²", "năm mươi mét vuông"),
    ("50 m2", "năm mươi mét vuông"),
    ("30°C", "ba mươi độ xê"),
    ("60 km/h", "sáu mươi ki lô mét trên giờ"),
    # Viết tắt
    ("TP.HCM", "thành phố Hồ Chí Minh"),
    ("HN", "Hà Nội"),
    ("VN", "Việt Nam"),
    ("CK", "chứng khoán"),
    ("ck cho anh", "chuyển khoản cho anh"),
    ("BĐS", "bất động sản"),
    ("NĐT", "nhà đầu tư"),
    ("LNST", "lợi nhuận sau thuế"),
    ("P/E", "pê trên e"),
    ("ROE", "rô e"),
    ("ĐHCĐ", "đại hội cổ đông"),
    ("CTCK", "công ty chứng khoán"),
    ("KQKD", "kết quả kinh doanh"),
    ("YoY", "so với cùng kỳ năm trước"),
    ("QoQ", "so với quý trước"),
    ("anh ko đi", "anh không đi"),
    ("k sao đâu", "không sao đâu"),
    ("K sao đâu", "không sao đâu"),
    ("dc rồi", "được rồi"),
    ("đc nha", "được nha"),
    ("FPT vs HPG", "ép pê tê so với hát pê giê"),
    # Mã cổ phiếu, chỉ số
    ("FPT", "ép pê tê"),
    ("mã VNM", "mã vê en em"),
    ("VN-Index", "vê en in đéc"),
    ("VNINDEX", "vê en in đéc"),
    ("HNX-Index", "hát en ích in đéc"),
    ("VN30", "vê en ba mươi"),
    # Không đánh vần từ in hoa tiếng Việt / câu viết HOA nhấn mạnh
    ("ANH ƠI", "ANH ƠI"),
    ("CHÚ Ý QUAN TRỌNG LẮM NHA", "CHÚ Ý QUAN TRỌNG LẮM NHA"),
    # Chữ thường không đổi
    ("Dạ chào anh, hôm nay trời đẹp.", "Dạ chào anh, hôm nay trời đẹp."),
    ("5 người", "năm người"),
]


@pytest.mark.parametrize(("raw", "spoken"), CASES)
def test_bang_chuan_hoa(raw, spoken):
    assert tn.normalize(raw) == spoken


def test_cau_dai_ghep_nhieu_loai():
    text = (
        "Dạ anh, FPT hôm nay ▲ 1,5% lên 120.500đ, KL 2,3 triệu CP; VN-Index +8,2 điểm. "
        "ĐHCĐ ngày 26/10 lúc 8h30 tại TP.HCM, LNST QoQ tăng 12%, ko có gì đáng lo."
    )
    assert tn.normalize(text) == (
        "Dạ anh, ép pê tê hôm nay tăng một phẩy năm phần trăm lên một trăm hai mươi nghìn "
        "năm trăm đồng, khối lượng hai triệu ba trăm nghìn cổ phiếu; vê en in đéc cộng tám "
        "phẩy hai điểm. đại hội cổ đông ngày hai mươi sáu tháng mười lúc tám giờ ba mươi "
        "phút tại thành phố Hồ Chí Minh, lợi nhuận sau thuế so với quý trước tăng mười hai "
        "phần trăm, không có gì đáng lo."
    )


def test_doc_so_co_ban():
    assert tn.read_int(0) == "không"
    assert tn.read_int(10) == "mười"
    assert tn.read_int(11) == "mười một"
    assert tn.read_int(101) == "một trăm linh một"
    assert tn.read_int(1_000_001) == "một triệu không trăm linh một"
    assert tn.read_int(2_000_500_000) == "hai tỷ năm trăm nghìn"
    assert tn.parse_number("1,299,000") == ("1299000", "")
    assert tn.parse_number("25,5") == ("25", "5")
    assert tn.parse_number("abc") is None


def test_tu_dien_doc_duoc_va_khoa_la_chuoi():
    exact, loose, letters = tn._dictionary()
    assert exact["LNST"] == "lợi nhuận sau thuế" and loose["ko"] == "không"
    assert letters["F"] == "ép" and all(isinstance(k, str) for k in exact)
