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
    # không có "còn" vẫn là đếm ngày, lấy lần sắp tới (không phải 30/4 năm nay đã qua)
    ("bao nhiêu ngày nữa đến 30/4", "Còn 191 ngày nữa là tới Giải phóng miền Nam"),
    ("mấy ngày nữa là tết", "Còn 108 ngày nữa là tới Tết Nguyên đán"),
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


# ─── Sửa sau nhận xét dùng thử 27/10 ─────────────────────────────────────────

REVIEW_TODAY = dt.date(2026, 10, 27)  # thứ Ba


@pytest.mark.parametrize(
    "query, expected",
    [
        ("tết 2028", "Thứ Tư 26/01/2028"),  # năm gõ kèm tên lễ không bị bỏ qua
        ("còn mấy ngày tới tết 2028", "Còn 456 ngày nữa là tới Tết Nguyên đán"),
        ("trung thu 2027", "Thứ Tư 15/09/2027"),
        ("tết 2025", "đã qua 636 ngày"),
        ("29/2/2027", "Năm 2027 không có ngày 29/2"),
        ("32/12", "Tháng 12 chỉ có 31 ngày, không có ngày 32/12"),
        ("5/13/2026", "Không có tháng 13"),
        ("25/12/9999", "Lịch chỉ tra được năm 1900-2100"),
    ],
)
def test_nam_cua_le_va_ngay_khong_co_that(query, expected):
    assert expected in calendar_query.understand(query, REVIEW_TODAY)


def test_ngay_khong_co_that_khong_bat_nham_ngay_am_va_khung_gio():
    assert "Đinh Mùi âm lịch rơi vào" in calendar_query.understand("âm 30/2", REVIEW_TODAY)
    assert "Đinh Mùi âm lịch rơi vào" in calendar_query.understand("ngày âm 30/2", REVIEW_TODAY)
    assert "29/02/2028" in calendar_query.understand("29/2/2028", REVIEW_TODAY)
    reply = calendar_query.understand("giờ hoàng đạo 13-15h", REVIEW_TODAY) or ""
    assert "Không có tháng" not in reply


def test_ngay_tot_thang_nay_bo_ngay_da_qua_va_nhac_loc_theo_viec():
    reply = calendar_query.understand("ngày tốt tháng này", REVIEW_TODAY)
    assert "từ hôm nay (27/10)" in reply and "05/10" not in reply and "26/10" not in reply
    assert "28/10" in reply and "chưa lọc theo việc" in reply
    # Tháng đã qua vẫn tra được, có ghi rõ.
    assert "tháng 9/2026 (đã qua)" in calendar_query.understand(
        "ngày tốt tháng trước", REVIEW_TODAY
    )


def test_xem_ngay_theo_viec_loc_theo_truc():
    from services import can_chi

    wedding = calendar_query.understand("ngày tốt để cưới tháng 12", REVIEW_TODAY)
    opening = calendar_query.understand("xem ngày khai trương tháng 12", REVIEW_TODAY)
    assert wedding.startswith("📅 Ngày tốt cho cưới hỏi, tháng 12/2026")
    assert opening.startswith("📅 Ngày tốt cho khai trương")
    assert wedding != opening  # cưới và khai trương không còn ra cùng một danh sách
    for line in wedding.splitlines()[1:-1]:
        day = dt.date(2026, 12, int(line.split()[3].split("/")[0]))
        assert can_chi.day_truc(day) in ("Định", "Thành", "Khai") and can_chi.day_god(day)[1]
    ground = calendar_query.understand("xem ngày động thổ", REVIEW_TODAY)
    assert "động thổ" in ground and "30 ngày tới" in ground and "trực" in ground
    # Tháng này hết ngày hợp -> gợi ý tháng sau, không trả danh sách rỗng.
    assert "Sang tháng 11/2026" in calendar_query.understand("ngày cưới tháng này", REVIEW_TODAY)
    one = calendar_query.understand("20/12 cưới được không", REVIEW_TODAY)
    assert "chưa hợp cưới hỏi" in one and "Ngày hợp gần nhất" in one
    # Đếm ngày tới đám cưới vẫn là câu đếm ngày.
    assert calendar_query.understand("còn mấy ngày tới đám cưới 20/12", REVIEW_TODAY).startswith(
        "Còn 54 ngày"
    )


def test_truc_ngay_va_ngay_giao_tiet_lap_truc():
    from services import can_chi

    assert can_chi.day_truc(dt.date(2024, 2, 10)) == "Mãn"  # Mùng 1 Tết Giáp Thìn
    # Lập đông 7/11/2026: ngày giao tiết lặp lại trực của hôm trước.
    assert can_chi.day_truc(dt.date(2026, 11, 6)) == can_chi.day_truc(dt.date(2026, 11, 7))


def test_cuoi_tuan_du_thu_bay_chu_nhat():
    good = calendar_query.understand("ngày tốt cuối tuần này", REVIEW_TODAY)
    assert "Thứ Bảy 31/10/2026" in good and "Chủ Nhật 01/11/2026" in good
    both = calendar_query.understand("thứ 7 chủ nhật này", REVIEW_TODAY)
    assert "Thứ Bảy 31/10/2026" in both and "Chủ Nhật 01/11/2026" in both
    # Chủ Nhật hỏi "cuối tuần này": còn mỗi hôm nay.
    sunday = calendar_query.understand("cuối tuần này", dt.date(2026, 11, 1))
    assert "Chủ Nhật 01/11/2026" in sunday and "31/10/2026" not in sunday


def test_khong_hieu_tra_none_lenh_chat_van_goi_y_cu_phap():
    assert calendar_query.understand("abc", REVIEW_TODAY) is None
    assert calendar_query.answer("abc", REVIEW_TODAY).startswith("Em chưa hiểu")
