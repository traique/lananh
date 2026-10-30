"""Test cho tầng nhận diện mã cổ phiếu trong tin nhắn.

Regression cho bug: mã nằm ngoài ALL_KNOWN_SYMBOLS mà gõ viết thường (vd
"gvr") không được nhận, khiến câu hỏi rơi xuống Gemini không kèm dữ liệu giá
thật và bị trả lời bằng số bịa.

Xem thêm test/test_market_data_guard.py cho vòng sửa thứ hai (tin nhắn trơ
trọi dạng mã, nhãn realtime ngoài giờ, dữ liệu thị trường ngoài sàn VN).
"""

import pytest

from stock import analysis as stock_analysis


def test_lowercase_symbol_with_price_keyword_is_candidate():
    known, unverified = stock_analysis.detect_symbol_candidates("giá gvr")
    assert "GVR" in known + unverified


def test_lowercase_symbol_with_stock_context_is_candidate():
    known, unverified = stock_analysis.detect_symbol_candidates("cổ phiếu gvr sao rồi")
    assert "GVR" in known + unverified


def test_uppercase_symbol_still_works_without_any_context():
    known, unverified = stock_analysis.detect_symbol_candidates("GVR")
    assert "GVR" in known + unverified


def test_bare_lowercase_is_now_a_candidate():
    """Trước đây test này khẳng định điều ngược lại: "gvr" trơ trọi bị bỏ qua.

    Đó là quyết định sai. Thực tế người dùng tra mã bằng cách gõ ĐÚNG mã và
    không gì khác, và hậu quả của việc bỏ qua không phải là "bot không trả
    lời" mà là "bot trả lời bằng giá bịa": 30/07/2026 bot báo GVR 34.500 VND
    trong khi giá thật là 27.550 VND. Xem stock_analysis.is_bare_symbol_message.
    """
    known, unverified = stock_analysis.detect_symbol_candidates("gvr")
    assert "GVR" in known + unverified


def test_known_symbol_lowercase_unaffected():
    known, _ = stock_analysis.detect_symbol_candidates("vcb")
    assert "VCB" in known


def test_ambiguous_known_symbol_still_needs_context():
    known, _ = stock_analysis.detect_symbol_candidates("đổ xăng hết bao nhiêu gas")
    assert "GAS" not in known


def test_price_question_without_any_symbol_is_not_guarded():
    assert stock_analysis.looks_like_price_question("giá vàng hôm nay bao nhiêu") is False


def test_price_question_with_unknown_token_is_guarded():
    assert stock_analysis.looks_like_price_question("giá xyz bao nhiêu") is True


def test_casual_message_is_not_guarded():
    assert stock_analysis.looks_like_price_question("tối nay ăn gì anh ơi") is False


def test_why_question_about_nonstock_prices_is_not_guarded():
    """Regression 09/09/2026: "tại sao giá điện thoại ... tăng giá mạnh trong
    năm 2026" bị chặn trả lời cứng vì token tiếng Việt có dấu bị regex ASCII
    cắt thành mảnh giả mã ("liên quan" -> QUAN, "điện thoại" -> THO). Câu hỏi
    lý giải không phải yêu cầu đọc giá, phải đi xuống chat thường."""
    assert (
        stock_analysis.looks_like_price_question(
            "tại sao giá điện thoại và các thiết bị điện tử liên quan lại tăng giá mạnh trong năm 2026"
        )
        is False
    )


def test_why_question_without_diacritics_is_not_guarded():
    assert stock_analysis.looks_like_price_question("tai sao gia dien thoai tang manh") is False


# ─── Sửa 27/10: chỉ báo "chưa tra ra mã" khi rõ ràng hỏi cổ phiếu ───────────


@pytest.mark.parametrize(
    "text",
    [
        "giá samsung s24 plus cũ",
        "giá iphone 15 pro max",
        "giá xe sh 2024",
        "giá sầu riêng hôm nay",
        "giá vàng",
        "giá xăng",
    ],
)
def test_hoi_gia_san_pham_khong_bi_chan_va_bat_tra_web(text):
    assert stock_analysis.looks_like_price_question(text) is False
    assert stock_analysis.wants_product_price(text) is True


@pytest.mark.parametrize(
    "text", ["giá cổ phiếu ABCXYZ", "giá mã abc", "giá xyz bao nhiêu", "giá ABCD", "Xyz"]
)
def test_hoi_co_phieu_ro_rang_van_bao_chua_tra_ra_ma(text):
    assert stock_analysis.looks_like_price_question(text) is True


def test_gia_fpt_van_la_co_phieu():
    known, _ = stock_analysis.detect_symbol_candidates("giá FPT")
    assert known == ["FPT"]


@pytest.mark.parametrize(
    "text", ["đánh giá phim này", "giá trị cuộc sống", "giá như anh ở đây", "giá cổ phiếu hôm nay"]
)
def test_khong_phai_hoi_gia_san_pham(text):
    assert stock_analysis.wants_product_price(text) is False


# ─── Persona: lượt nào cần phần phân tích cổ phiếu (chat_needs_stock_rules) ──


@pytest.mark.parametrize(
    "text",
    [
        "FPT có đáng tích luỹ dài hạn không",
        "hpg thế nào",
        "sao em bảo giữ HPG",
        "VN-Index hôm nay sao",
        "vnindex",
        "thị trường chứng khoán tuần này ra sao",
        "mã nào đáng mua",
        "cổ phiếu ngành thép",
        "ngân hàng nào đáng mua bây giờ",
        "Danh mục của anh thế nào",
        "nên cắt lỗ không em",
        "anh muốn đầu tư vào đâu",
        "có nên mua vàng lúc này",
        "gia vang hom nay",
        "bitcoin còn lên không",
        "tỷ giá USD hôm nay",
        "vàng nhẫn hôm nay bao nhiêu",
        "SJC hôm nay bao nhiêu",
        "đô la hôm nay bao nhiêu",
        "thị trường hôm nay thế nào em",
        "sàn hôm nay đỏ quá",
    ],
)
def test_luot_co_phieu_dau_tu_can_phan_co_phieu(text):
    assert stock_analysis.chat_needs_stock_rules(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "chào em",
        "tối nay ăn gì giờ",
        "toi nay an gi",
        "mai Đà Lạt mưa không",
        "mai co mua khong em",
        "tâm sự chút đi em",
        "ma cà rồng có thật không",
        "có nên đi Đà Lạt không",
        "dự định cuối tuần của anh",
        "mua cho anh cái áo",
        "giá iPhone 16 bao nhiêu",
        "giá ck bao nhiêu",  # ck viết thường = chuyển khoản
        "hết dầu gội rồi",
        "hoa vàng nở rồi",
        "em thích màu vàng hay màu hồng",
        "anh vừa đi ngân hàng về",
        "nhắc anh 8h tối mai gọi mẹ",
        "https://cafef.vn/hpg-lai-ky-luc",  # slug trong link không phải mã
    ],
)
def test_chuyen_doi_thuong_khong_keo_phan_co_phieu(text):
    assert stock_analysis.chat_needs_stock_rules(text) is False
