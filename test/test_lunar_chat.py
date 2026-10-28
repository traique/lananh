"""Test services/lunar_chat.py: trả lời âm lịch bằng code, không gọi AI."""

import datetime as dt

import pytest

from services import lunar_chat

TODAY = dt.date(2026, 10, 21)  # 12/9 năm Bính Ngọ


@pytest.mark.parametrize(
    "text,expected",
    [
        # Âm -> dương; không nói năm thì lấy lần sắp tới (rằm tháng 8 năm nay đã qua).
        ("rằm tháng 8 là ngày nào", "Thứ Tư 15/09/2027"),
        ("ram thang 8 la ngay nao", "Thứ Tư 15/09/2027"),
        ("rằm tháng mười một", "Thứ Tư 23/12/2026"),
        ("rằm tháng chạp", "Thứ Sáu 22/01/2027"),
        ("Rằm tháng giêng năm sau ngày mấy dương", "Thứ Bảy 20/02/2027"),
        ("mùng 1 tết", "Thứ Bảy 06/02/2027"),
        ("mùng 10 tháng 3 âm", "Thứ Sáu 16/04/2027"),
        ("ngày 15 tháng 6 nhuận năm 2025", "Ngày 15 tháng 6 nhuận năm Ất Tỵ"),
        ("ngày 15 tháng 8 âm lịch rơi vào thứ mấy", "Thứ Tư 15/09/2027"),
        ("15/8 âm là ngày nào", "Thứ Tư 15/09/2027"),
        # Dương -> âm, chỉ trả đúng ngày âm (không kể thêm giờ hoàng đạo...).
        ("20/11 là ngày mấy âm", "là ngày 12 tháng 10 năm Bính Ngọ âm lịch"),
        ("mùng 2 tháng 9 là ngày mấy âm", "là ngày 21 tháng 7 năm Bính Ngọ âm lịch"),
        ("hôm nay âm lịch ngày mấy", "Hôm nay (Thứ Tư 21/10/2026) là ngày 12 tháng 9"),
        ("hom nay mung may am", "là ngày 12 tháng 9 năm Bính Ngọ"),
        # Hỏi riêng một phần.
        ("mai giờ hoàng đạo", "Ngày mai (Thứ Năm 22/10/2026) giờ hoàng đạo: Sửu (1h-3h)"),
        ("hôm nay tiết khí gì", "thuộc tiết Hàn lộ"),
        ("hôm qua can chi", "là ngày Đinh Mão, tháng Mậu Tuất, năm Bính Ngọ"),
        # Tết.
        ("còn mấy ngày nữa tết", "Còn 108 ngày nữa là Tết Nguyên đán năm Đinh Mùi"),
        # không có "còn" (câu chat thật từng đi sang AI)
        ("bao nhiêu ngày nữa đến tết.", "Còn 108 ngày nữa là Tết Nguyên đán năm Đinh Mùi"),
        ("mấy ngày nữa là tết", "Còn 108 ngày nữa là Tết Nguyên đán năm Đinh Mùi"),
        ("bao giờ tết?", "Còn 108 ngày nữa"),
        ("tết năm 2028 ngày nào", "Thứ Tư 26/01/2028"),
        # Xem tổng quan.
        ("âm lịch", "Giờ hoàng đạo:"),
        # Ngày không tồn tại.
        ("ngày 30 tháng 2 âm năm 2026", "chỉ có 29 ngày"),
        ("mùng 1 tháng 3 nhuận năm 2025", "không có tháng 3 nhuận"),
    ],
)
def test_tra_loi_thang(text, expected):
    reply = lunar_chat.answer(text, TODAY)
    assert reply is not None
    assert expected in reply


def test_hoi_ngay_am_khong_ke_them_gio_hoang_dao():
    reply = lunar_chat.answer("hôm nay âm lịch ngày mấy", TODAY)
    assert "hoàng đạo" not in reply and "Tiết khí" not in reply


@pytest.mark.parametrize(
    "text",
    [
        # Không rõ âm hay dương -> để AI (router + chat) lo như cũ.
        "mùng 2 tháng 9 là ngày gì",
        "ngày 21 tháng 10 âm lịch là bao nhiêu",
        "20/11 âm lịch là bao nhiêu",
        "rằm năm 2027",
        # Không phải câu âm lịch.
        "trời ấm không em",
        "nhạc âm thanh hay",
        "tết này em về quê không",
        "anh mua mùng 1 cái nha",
        "giá vàng hôm nay",
    ],
)
def test_khong_chac_thi_tra_none(text):
    assert lunar_chat.answer(text, TODAY) is None
