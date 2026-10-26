"""Test services/calendar_query.py: /lich hỏi tự do, không bao giờ gọi AI."""

import datetime as dt

import pytest

from services import calendar_query

TODAY = dt.date(2026, 10, 21)

CASES = [
    ("", "Thứ Tư 21/10/2026 dương lịch, tức ngày 12 tháng 9"),
    ("20/11", "🎉 Ngày Nhà giáo Việt Nam"),
    ("mai", "Thứ Năm 22/10/2026"),
    ("thứ 7 tuần sau", "Thứ Bảy 31/10/2026"),
    ("chủ nhật", "Chủ Nhật 25/10/2026"),
    ("cuối tuần", "Thứ Bảy 24/10/2026"),
    ("rằm tháng 8", "Thứ Tư 15/09/2027"),
    ("âm 15/6n/2025", "Thứ Sáu 08/08/2025"),
    ("rằm tới", "Rằm sắp tới: Thứ Bảy 24/10/2026"),
    ("mùng 1 tới", "Mùng 1 sắp tới: Thứ Hai 09/11/2026"),
    ("tháng này", "📅 Tháng 10/2026"),
    ("tháng 12/2026", "23 T4 | âm 15 🌕"),
    ("tháng 8 âm", "Âm 15 - Thứ Sáu 25/09 🌕 rằm"),
    ("trung thu", "Tết Trung thu: Thứ Tư 15/09/2027"),
    ("vu lan năm sau", "Thứ Hai 16/08/2027"),
    ("giỗ tổ 2027", "Thứ Sáu 16/04/2027"),
    ("các ngày lễ", "• Ngày Nhà giáo Việt Nam (dương): Thứ Sáu 20/11/2026 - còn 30 ngày"),
    ("tết", "Thứ Bảy 06/02/2027"),
    ("còn mấy ngày tới 30/4", "Còn 191 ngày nữa là tới Giải phóng miền Nam"),
    ("còn bao nhiêu ngày nữa đến trung thu", "Còn 329 ngày nữa"),
    ("từ 1/1 đến 20/11", "là 323 ngày"),
    ("ngày tốt tháng này", "Thứ Tư 21/10 (âm 12) - Thanh Long"),
    ("hôm nay hoàng đạo không", "ngày hoàng đạo (Thanh Long)"),
    ("tuổi 15/3/1990", "Canh Ngọ (con Ngựa), mệnh Thổ - Lộ Bàng Thổ"),
    ("tuổi 15/1/1990", "Kỷ Tỵ"),
    ("1990 mệnh gì", "Lộ Bàng Thổ"),
    ("năm 2027 là năm con gì", "Đinh Mùi (con Dê)"),
    ("tuổi Ngọ hợp tuổi nào", "tam hợp với Dần, Tuất"),
    ("1990 và 1995 có hợp không", "Hỏa sinh Thổ"),
    ("sao hạn 1990 nam năm 2027", "Sao hạn (nam): Thổ Tú"),
    ("1995 kim lâu năm 2027", "Kim lâu Tử"),
    ("hôm nay giờ hoàng đạo", "giờ hoàng đạo: Dần (3h-5h)"),
    ("2027", "Đinh Mùi"),
]


@pytest.mark.parametrize("query,expected", CASES)
def test_lich_tra_loi(query, expected):
    assert expected in calendar_query.answer(query, TODAY)


def test_khong_hieu_thi_goi_y():
    reply = calendar_query.answer("asdf qwe", TODAY)
    assert reply.startswith("Em chưa hiểu")
    assert "/lich tháng này" in reply


def test_lich_khong_bao_gio_goi_ai(monkeypatch):
    from ai import official_client, orchestrator

    async def boom(*args, **kwargs):
        raise AssertionError("/lich không được gọi AI")

    monkeypatch.setattr(orchestrator, "ask", boom)
    monkeypatch.setattr(orchestrator, "chat", boom)
    monkeypatch.setattr(orchestrator, "utility_json", boom)
    monkeypatch.setattr(official_client, "generate_utility_json", boom)
    for query, _ in CASES:
        assert calendar_query.answer(query, TODAY)


def test_relative_day_cong_khai_va_ban_bo_dau_cho_nhac_viec_cung_ton_tai():
    """proactive_service dùng relative_day/solar_dates công khai (tự chuẩn hoá, trả
    ngày); vn_time_parse dùng bản *_folded (chữ đã bỏ dấu, trả (ngày, nhãn))."""
    import datetime as dt

    from services import proactive_service

    today = dt.date(2026, 10, 26)
    assert calendar_query.relative_day("Mai", today) == dt.date(2026, 10, 27)
    assert calendar_query.solar_dates("ngày 15/11", today) == [dt.date(2026, 11, 15)]
    folded = calendar_query.relative_day_folded("mai", today)
    assert folded is not None and folded[0] == dt.date(2026, 10, 27)
    assert calendar_query.solar_dates_folded("15/11", today) == [dt.date(2026, 11, 15)]
    assert proactive_service.resolve_when("mai", today) == dt.date(2026, 10, 27)
