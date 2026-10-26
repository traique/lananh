"""services/vn_time_parse.py + services/recurrence.py: giờ nhắc tính bằng code."""

import datetime as dt

import pytest

from services import recurrence, vn_time_parse as p

VN = p.VN_TZ
NOW = dt.datetime(2026, 10, 26, 22, 37, tzinfo=VN)  # Thứ Hai, 22:37


def at(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=VN)


@pytest.mark.parametrize(
    ("text", "due", "message"),
    [
        # Lỗi kinh điển của LLM tự cộng phút: "8h sáng mai" gửi lúc 22:37.
        ("nhắc anh 8h sáng mai uống thuốc", at(2026, 10, 27, 8), "Uống thuốc"),
        ("nhắc anh 30 phút nữa uống thuốc", at(2026, 10, 26, 23, 7), "Uống thuốc"),
        ("nhắc anh 1 tiếng rưỡi nữa tắt bếp", at(2026, 10, 27, 0, 7), "Tắt bếp"),
        ("nhắc anh 8h tối mai gọi mẹ nhé", at(2026, 10, 27, 20), "Gọi mẹ"),
        ("thứ 6 tuần sau 9h nhắc em nộp báo cáo", at(2026, 11, 6, 9), "Nộp báo cáo"),
        ("nhắc anh 15/11 lúc 9h đi khám", at(2026, 11, 15, 9), "Đi khám"),
        ("nhắc anh 9h kém 15 họp", at(2026, 10, 27, 8, 45), "Họp"),
        ("nhắc anh 20:30 xem bóng đá", at(2026, 10, 27, 20, 30), "Xem bóng đá"),
        ("nhắc anh mốt 9h đi khám", at(2026, 10, 28, 9), "Đi khám"),
        ("nhắc anh 3h chiều mai họp", at(2026, 10, 27, 15), "Họp"),
        ("đầu tuần sau 8h nhắc anh gửi hồ sơ", at(2026, 11, 2, 8), "Gửi hồ sơ"),
        ("chủ nhật 9h nhắc anh đi chợ", at(2026, 11, 1, 9), "Đi chợ"),
        ("nhắc anh 23h tắt máy", at(2026, 10, 26, 23), "Tắt máy"),
        ("nhắc anh ngày 30 lúc 9h trả lương", at(2026, 10, 30, 9), "Trả lương"),
        # "tôi" (đại từ) không phải buổi "tối"; "sang nhà" không phải "sáng".
        ("nhắc tôi 8h gọi điện cho sếp", at(2026, 10, 27, 8), "Gọi điện cho sếp"),
        ("nhắc anh 8h sang nhà mẹ", at(2026, 10, 27, 8), "Sang nhà mẹ"),
        # Ngày âm có tháng đã qua năm nay -> năm sau.
        ("nhắc anh rằm tháng 8 lúc 19h mua bánh", at(2027, 9, 15, 19), "Mua bánh"),
    ],
)
def test_doc_gio_nhac_va_noi_dung(text, due, message):
    when = p.parse(text, NOW)
    assert when.due_at == due and when.recurrence is None and not when.error
    extracted = p.extract_reminder(text, NOW)
    assert extracted is not None and extracted[0] == message


def test_chi_noi_buoi_lay_gio_mac_dinh():
    when = p.parse("nhắc anh sáng mai gọi điện", NOW)
    assert when.due_at == at(2026, 10, 27, 8) and when.approx_time


def test_gio_da_qua_khong_noi_ngay_thi_sang_hom_sau_co_ngay_thi_bao_loi():
    morning = at(2026, 10, 26, 8, 30)
    assert p.parse("nhắc anh 7h sáng uống thuốc", morning).due_at == at(2026, 10, 27, 7)
    # 8h không nói buổi, đã qua 8h sáng -> 20h tối nay.
    assert p.parse("nhắc anh 8h gọi mẹ", at(2026, 10, 26, 9)).due_at == at(2026, 10, 26, 20)
    late = p.parse("nhắc anh tối nay gọi Mai", NOW)
    assert late.due_at is None and "đã qua" in late.error
    assert "1 năm" in p.parse("nhắc anh 25/12/2027 9h họp", NOW).error
    no_time = p.parse("nhắc anh thứ 6 nộp báo cáo", NOW)
    assert no_time.due_at is None and "Cần giờ" in no_time.error


@pytest.mark.parametrize(
    ("text", "rule", "due"),
    [
        (
            "nhắc anh 7h sáng mỗi ngày uống thuốc",
            {"type": "daily", "at": "07:00"},
            at(2026, 10, 27, 7),
        ),
        ("nhắc anh mỗi tối 22h đi ngủ", {"type": "daily", "at": "22:00"}, at(2026, 10, 27, 22)),
        (
            "nhắc anh thứ 2, 4, 6 hằng tuần 6h chạy bộ",
            {"type": "weekly", "at": "06:00", "days": [0, 2, 4]},
            at(2026, 10, 28, 6),
        ),
        (
            "nhắc anh ngày 5 hằng tháng 9h đóng tiền nhà",
            {"type": "monthly", "at": "09:00", "day": 5},
            at(2026, 11, 5, 9),
        ),
        (
            "mùng 1 và rằm nhắc anh thắp hương lúc 6h30",
            {"type": "lunar_monthly", "at": "06:30", "days": [1, 15], "day30": "last"},
            at(2026, 11, 9, 6, 30),
        ),
        (
            "nhắc anh 12/3 âm hằng năm 8h giỗ ông",
            {
                "type": "lunar_yearly",
                "at": "08:00",
                "month": 3,
                "day": 12,
                "leap": "regular",
                "day30": "last",
            },
            at(2027, 4, 18, 8),
        ),
    ],
)
def test_doc_quy_tac_lap(text, rule, due):
    when = p.parse(text, NOW)
    assert when.recurrence == rule and when.due_at == due


def test_lap_khong_co_gio_thi_hoi_lai():
    when = p.parse("nhắc anh uống thuốc mỗi ngày", NOW)
    assert when.due_at is None and "giờ" in when.error


def test_chu_trung_nhau_khi_bo_dau():
    assert p.parse("Bài nhạc anh gửi hay ghê", NOW).found is False
    assert p.parse("nhắc anh chiều vợ", NOW).found is False  # "chiều" không phải buổi
    assert p.extract_reminder("nhắc anh mãi mãi yêu em lúc 9h", NOW)[0] == "Mãi mãi yêu em"


def test_hoan_bao_lau():
    assert p.parse_snooze("", NOW) == NOW + dt.timedelta(minutes=10)
    assert p.parse_snooze("10p", NOW) == NOW + dt.timedelta(minutes=10)
    assert p.parse_snooze("1 tiếng", NOW) == NOW + dt.timedelta(hours=1)
    assert p.parse_snooze("nửa tiếng", NOW) == NOW + dt.timedelta(minutes=30)
    assert p.parse_snooze("30", NOW) == NOW + dt.timedelta(minutes=30)
    assert p.parse_snooze("sáng mai", NOW) == at(2026, 10, 27, 8)
    assert p.parse_snooze("tới 8h tối", NOW) == at(2026, 10, 27, 20)
    assert p.parse_snooze("abc", NOW) is None


def test_doi_nhac_chua_toi_gio_cong_tu_gio_hen_cu():
    later = at(2026, 10, 27, 20)  # nhắc cũ hẹn 20h mai
    assert p.parse_snooze("30p", NOW, base=later) == at(2026, 10, 27, 20, 30)
    # Giờ tuyệt đối vẫn tính theo bây giờ, không theo giờ hẹn cũ.
    assert p.parse_snooze("sáng mai", NOW, base=later) == at(2026, 10, 27, 8)


def test_ngay_trong_nam_cho_ngay_nho():
    lunar_date = p.parse_calendar_date("giỗ ông nội 12/3 âm trước 3 ngày")
    assert (lunar_date.day, lunar_date.month, lunar_date.lunar) == (12, 3, True)
    solar = p.parse_calendar_date("sinh nhật vợ 20/11/1990")
    assert (solar.day, solar.month, solar.year, solar.lunar) == (20, 11, 1990, False)
    assert p.parse_calendar_date("mùng 1 thắp hương") is None  # thiếu tháng


# ─── recurrence: tháng nhuận, ngày 30, Tết, ngày cuối tháng ─────────────────


def test_lunar_yearly_thang_nhuan_va_ngay_30():
    # Năm âm 2025 có tháng 6 nhuận: "leap" chọn tháng nhuận, "regular" giữ tháng thường.
    regular = {"type": "lunar_yearly", "month": 6, "day": 10, "at": "08:00"}
    leap = dict(regular, leap="leap")
    after = at(2025, 1, 1)
    assert recurrence.next_occurrence(regular, after) == at(2025, 7, 4, 8)
    assert recurrence.next_occurrence(leap, after) == at(2025, 8, 3, 8)
    # Tháng 2 âm 2026 chỉ 29 ngày: "last" nhắc ngày 29, "skip" bỏ qua năm đó.
    day30 = {"type": "lunar_yearly", "month": 2, "day": 30, "at": "08:00"}
    assert recurrence.next_occurrence(day30, at(2026, 1, 1)) == at(2026, 4, 16, 8)
    skipped = recurrence.next_occurrence(dict(day30, day30="skip"), at(2026, 1, 1))
    assert skipped is not None and skipped.year > 2026


def test_lunar_yearly_qua_ranh_gioi_tet():
    # Giỗ 20/12 âm: lần kế tiếp sau 01/02/2027 vẫn rơi trước Tết Đinh Mùi (06/02/2027).
    rule = {"type": "lunar_yearly", "month": 12, "day": 20, "at": "08:00"}
    due = recurrence.next_occurrence(rule, at(2027, 1, 20))
    assert due is not None and due.date() < dt.date(2027, 2, 6)


def test_monthly_ngay_31_va_yearly_29_2():
    rule = {"type": "monthly", "day": 31, "at": "09:00"}
    assert recurrence.next_occurrence(rule, at(2026, 11, 1)) == at(2026, 11, 30, 9)
    leap_day = {"type": "yearly", "month": 2, "day": 29, "at": "08:00"}
    assert recurrence.next_occurrence(leap_day, at(2026, 3, 1)) == at(2027, 2, 28, 8)
    assert recurrence.next_occurrence(leap_day, at(2027, 3, 1)) == at(2028, 2, 29, 8)


def test_quy_tac_sai_bi_tu_choi():
    with pytest.raises(recurrence.RecurrenceError):
        recurrence.normalize({"type": "yearly", "month": 4, "day": 31, "at": "08:00"})
    with pytest.raises(recurrence.RecurrenceError):
        recurrence.normalize({"type": "hourly", "at": "08:00"})  # không có lặp theo giờ
    assert recurrence.describe({"type": "lunar_monthly", "days": [1, 15], "at": "06:30"}) == (
        "06:30 mùng 1, rằm hằng tháng âm lịch"
    )


# ─── Ngày không đọc được thì hỏi lại, không lặng lẽ thành hôm nay/ngày mai ───
EVENING = at(2026, 10, 26, 21, 30)  # Thứ Hai 21:30


@pytest.mark.parametrize(
    ("text", "now", "due", "message"),
    [
        ("nhắc anh ngày 15 8h sáng đóng tiền nhà", EVENING, at(2026, 11, 15, 8), "Đóng tiền nhà"),
        ("nhắc anh ngày 31 9h họp", EVENING, at(2026, 10, 31, 9), "Họp"),
        ("nhắc anh ngày 15 9h họp", at(2026, 10, 26, 10), at(2026, 11, 15, 9), "Họp"),
        ("nhắc anh ngày 15 20:30 họp", EVENING, at(2026, 11, 15, 20, 30), "Họp"),
        ("nhắc anh 3 ngày nữa 8h sáng đi khám", EVENING, at(2026, 10, 29, 8), "Đi khám"),
        ("nhắc anh 9h sáng 2 tuần nữa nộp hồ sơ", EVENING, at(2026, 11, 9, 9), "Nộp hồ sơ"),
        # Chữ ngày là nội dung khi đã có ngày rõ ràng.
        ("nhắc anh 8h mai mua đồ Tết", EVENING, at(2026, 10, 27, 8), "Mua đồ Tết"),
        (
            "nhắc anh 9h sáng mai gửi lịch tuần sau",
            EVENING,
            at(2026, 10, 27, 9),
            "Gửi lịch tuần sau",
        ),
    ],
)
def test_ngay_noi_lien_gio_va_khoang_ngay(text, now, due, message):
    when = p.parse(text, now)
    assert when.due_at == due and not when.error and when.recurrence is None
    assert p.extract_reminder(text, now)[0] == message


@pytest.mark.parametrize(
    "text",
    [
        "nhắc anh tuần sau 9h họp",
        "nhắc anh 10h sáng ngày 15 tháng sau đi khám",
        "nhắc anh tối 30 tết 8h cúng",
        "nhắc anh 1 tháng nữa lúc 9h gia hạn",
    ],
)
def test_ngay_chua_hieu_thi_bao_loi_khong_lan_sang_mai(text):
    when = p.parse(text, EVENING)
    assert when.due_at is None and "ngày" in when.error
