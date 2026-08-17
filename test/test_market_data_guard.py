"""Test cho các fix sau sự cố 30/07/2026:

- Fix D: "Gvr" / "Oil" (bàn phím di động tự viết hoa chữ đầu) phải được nhận
  là mã, thay vì rơi xuống Gemini và bị trả lời bằng giá bịa.
- Fix E: tin nhắn trơ trọi dạng mã nhưng không tra được -> chặn lại.
- Fix F: câu hỏi dữ liệu thị trường ngoài sàn VN -> đánh dấu cần search thật.
- Fix G: ngoài giờ giao dịch không được gắn nhãn "khớp lệnh realtime".
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from stock import analysis as stock_analysis
from stock import providers

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


# ─── Fix D ────────────────────────────────────────────────

def test_is_bare_symbol_message():
    assert stock_analysis.is_bare_symbol_message("Gvr")
    assert stock_analysis.is_bare_symbol_message("gvr")
    assert stock_analysis.is_bare_symbol_message("  GVR  ")
    assert not stock_analysis.is_bare_symbol_message("gia gvr")
    assert not stock_analysis.is_bare_symbol_message("ab")
    assert not stock_analysis.is_bare_symbol_message("abcde")
    assert not stock_analysis.is_bare_symbol_message("")


def test_capitalized_bare_symbol_is_candidate():
    """Ca gây lỗi thật: user gõ "gvr", Telegram mobile gửi đi "Gvr"."""
    known, unverified = stock_analysis.detect_symbol_candidates("Gvr")
    assert "GVR" in known + unverified


def test_lowercase_bare_symbol_is_candidate():
    known, unverified = stock_analysis.detect_symbol_candidates("gvr")
    assert "GVR" in known + unverified


def test_ambiguous_known_symbol_bare_message_is_accepted():
    """"Oil" trơ trọi từng bị loại -> Gemini bịa giá Brent/WTI, trong khi
    "OIL" lại ra đúng giá cổ phiếu PVOIL. Hai cách viết phải cho cùng kết quả."""
    known_upper, _ = stock_analysis.detect_symbol_candidates("OIL")
    known_mixed, _ = stock_analysis.detect_symbol_candidates("Oil")
    assert "OIL" in known_upper
    assert known_mixed == known_upper


def test_ambiguous_known_symbol_still_needs_context_in_a_sentence():
    """Trong câu dài thì vẫn phải có ngữ cảnh - không được nới rộng quá tay."""
    known, _ = stock_analysis.detect_symbol_candidates("hôm nay đi đổ gas hết nhiều tiền")
    assert "GAS" not in known


def test_common_word_bare_message_is_not_a_symbol():
    _, unverified = stock_analysis.detect_symbol_candidates("Anh")
    assert unverified == []


def test_long_sentence_without_context_still_ignores_lowercase():
    """Fix D chỉ nới cho tin nhắn TRƠ TRỌI, không đụng tới câu dài."""
    _, unverified = stock_analysis.detect_symbol_candidates("tối nay ăn gì anh ơi")
    assert unverified == []


# ─── Fix E ───────────────────────────────────────────────

def test_bare_unknown_symbol_is_guarded():
    assert stock_analysis.looks_like_price_question("Xyz")


def test_bare_common_word_is_not_guarded():
    assert not stock_analysis.looks_like_price_question("Anh")


def test_casual_message_is_not_guarded():
    assert not stock_analysis.looks_like_price_question("tối nay ăn gì anh ơi")


# ─── Fix F ───────────────────────────────────────────────

def test_external_market_questions_are_detected():
    assert stock_analysis.wants_external_market_data("giá dầu hôm nay thế nào")
    assert stock_analysis.wants_external_market_data("tỷ giá USD bao nhiêu rồi")
    assert stock_analysis.wants_external_market_data("bitcoin đang bao nhiêu")
    assert stock_analysis.wants_external_market_data("đêm qua Dow Jones ra sao")


def test_vn_stock_questions_are_not_forced_to_search():
    """Mã VN đã có grounding từ DNSE - không được ép search, sẽ chậm vô ích."""
    assert not stock_analysis.wants_external_market_data("Gvr")
    assert not stock_analysis.wants_external_market_data("giá VCB bao nhiêu")
    assert not stock_analysis.wants_external_market_data("tối nay ăn gì anh ơi")


# ─── Fix G ───────────────────────────────────────────────

def test_is_market_hours():
    # Thứ Năm 30/07/2026 lúc 10:00 -> trong phiên
    assert stock_analysis.is_market_hours(datetime(2026, 7, 30, 10, 0, tzinfo=VN_TZ))
    # Cùng ngày lúc 23:35 -> đã đóng cửa (ca gây lỗi thật)
    assert not stock_analysis.is_market_hours(datetime(2026, 7, 30, 23, 35, tzinfo=VN_TZ))
    # Trước giờ mở cửa
    assert not stock_analysis.is_market_hours(datetime(2026, 7, 30, 8, 30, tzinfo=VN_TZ))
    # Chủ nhật 02/08/2026 giữa trưa -> nghỉ
    assert not stock_analysis.is_market_hours(datetime(2026, 8, 2, 11, 0, tzinfo=VN_TZ))


def _quote(**kwargs) -> providers.Quote:
    base = dict(
        symbol="GVR", price=27550, prev_close=26750, change=800,
        change_pct=2.99, date="2026-07-30", is_realtime=True,
    )
    base.update(kwargs)
    return providers.Quote(**base)


def test_quote_label_outside_market_hours_is_not_realtime(monkeypatch):
    monkeypatch.setattr(stock_analysis, "is_market_hours", lambda now=None: False)
    msg = stock_analysis.format_quote_message(_quote())
    assert "realtime" not in msg
    assert "cuối phiên" in msg


def test_quote_label_during_market_hours_is_realtime(monkeypatch):
    monkeypatch.setattr(stock_analysis, "is_market_hours", lambda now=None: True)
    msg = stock_analysis.format_quote_message(_quote())
    assert "khớp lệnh realtime" in msg


def test_quote_label_without_tick_is_close_price(monkeypatch):
    monkeypatch.setattr(stock_analysis, "is_market_hours", lambda now=None: True)
    msg = stock_analysis.format_quote_message(_quote(is_realtime=False))
    assert "đóng cửa phiên gần nhất" in msg
