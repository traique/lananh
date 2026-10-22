"""Test chặn tái phát các bug tìm được khi review (mỗi test fail nếu bỏ bản sửa).

Ngày "hôm nay" cố định để kết quả không đổi theo thời gian.
"""

import datetime as dt

import httpx
import pytest

from services import article_reader, calendar_query, lunar_chat, tuvi, web_reader

OCT = dt.date(2026, 10, 22)
JAN = dt.date(2027, 1, 15)  # sau Tết dương, trước Tết âm (06/02/2027)


# 1. Ngày âm có "rằm/tết" nhưng câu hỏi chuyện khác -> để AI, không trả 1 dòng ngày.
@pytest.mark.parametrize("text", ["rằm tháng 7 nên cúng gì", "mùng 3 tết đi đâu chơi"])
def test_ngay_am_kem_cau_hoi_khac_de_ai(text):
    assert lunar_chat.answer(text, OCT) is None


@pytest.mark.parametrize("text", ["rằm tháng 8 ạ", "rằm tháng 8 là ngày bao nhiêu"])
def test_ngay_am_van_tra_loi_khi_hoi_ngay(text):
    assert "15/09/2027" in lunar_chat.answer(text, OCT)


# 2. "năm nay" trong tháng 1 dương = năm dương, không phải năm âm cũ.
def test_nam_nay_thang_mot_la_nam_duong():
    assert "06/02/2027" in lunar_chat.answer("tết năm nay ngày nào", JAN)
    assert "15/09/2027" in calendar_query.answer("trung thu năm nay", JAN)
    assert "06/10/2025" in lunar_chat.answer("rằm tháng 8 năm ngoái là ngày nào", OCT)


# 3. Chữ trùng sau khi bỏ dấu không bị hiểu thành câu âm lịch.
@pytest.mark.parametrize(
    "text",
    [
        "hôm nay độ ẩm bao nhiêu",  # ẩm != âm
        "một năm âm lịch có bao nhiêu ngày",  # một != mốt
        "chị Mai sinh năm 1990 can chi là gì",  # tên Mai != ngày mai
        "nghỉ tết mấy ngày",  # không hỏi ngày Tết
    ],
)
def test_chu_trung_sau_bo_dau(text):
    assert lunar_chat.answer(text, OCT) is None


def test_ngay_hoang_dao_khac_gio_hoang_dao():
    reply = lunar_chat.answer("mai có phải ngày hoàng đạo không", OCT)
    assert "hắc đạo (Thiên Hình)" in reply and "giờ hoàng đạo" not in reply
    assert "giờ hoàng đạo" in lunar_chat.answer("mai giờ hoàng đạo", OCT)
    assert "23/12/2026" in lunar_chat.answer("rằm tháng mười một", OCT)


# 4. /lich: "cưới/họp/hàn" (có dấu) không bị hiểu là "cuối/hợp/hạn".
def test_lich_tu_khoa_so_tren_chu_co_dau():
    assert calendar_query.answer("cưới ngày 20/12/2026 được không", OCT).startswith(
        "📅 Ngày 20/12/2026"
    )
    assert "Tết Hàn thực" in calendar_query.answer("hàn thực năm 2027", OCT)
    assert calendar_query.answer("họp ngày 20/11/2026", OCT).startswith("📅 Ngày 20/11/2026")
    assert "Sao hạn (nam): Thổ Tú" in calendar_query.answer("sao hạn 1990 nam năm 2027", OCT)


# 5. Bảng tháng âm đọc năm ở bất cứ đâu; "tuổi Tý" có dấu không hỏi lại.
def test_lich_thang_am_co_nam_va_thang_nhuan():
    assert "năm Đinh Mùi (2027)" in calendar_query.answer("tháng 2 âm năm 2027", OCT)
    reply = calendar_query.answer("tháng 6 nhuận âm năm 2025", OCT)
    assert reply.startswith("📅 Tháng 6 nhuận âm lịch năm Ất Tỵ (2025)")
    assert calendar_query.answer("tuổi Tý hợp tuổi nào", OCT).startswith("Tuổi Tý:")
    assert calendar_query.answer("tuổi Tỵ hợp tuổi nào", OCT).startswith("Tuổi Tỵ:")


# 6. /tuvi: giờ chiều/tối và chữ "âm" không đứng đầu.
@pytest.mark.parametrize(
    "arg,hour",
    [("15/3/1990 9 giờ tối nam", "Hợi"), ("15/3/1990 3h chiều nữ", "Thân"),
     ("15/3/1990 11h đêm nam", "Tý"), ("15/3/1990 9h30 sáng nam", "Tỵ")],
)  # fmt: skip
def test_tuvi_gio_chieu_toi(arg, hour):
    assert tuvi.CHI[tuvi.parse(arg).hour_chi] == hour


def test_tuvi_chu_am_o_giua_cau():
    c = tuvi.parse("20/2/1990 âm giờ tỵ nữ")
    assert (c.lunar_day, c.lunar_month) == (20, 2)


# 7. Link kèm câu hỏi đi chat AI; chỉ link / "đọc bài này" mới đọc nguyên bài.
@pytest.mark.parametrize(
    "text",
    [
        "tin này tốt hay xấu cho HPG https://cafef.vn/hpg-123.chn",
        "sao giá vàng tăng mạnh vậy em https://vnexpress.net/a-1.html",
        "summarize https://vnexpress.net/a-1.html",
    ],
)
def test_link_kem_cau_hoi_khong_doc_bai(text):
    assert article_reader.find_request(text) is None


# 8. Lỗi mạng khi đọc bài -> câu báo lỗi, không văng lỗi ra pipeline chat.
async def test_loi_mang_khi_doc_bai(monkeypatch):
    async def timeout(*args, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(web_reader, "_get_public", timeout)
    reply = await article_reader.maybe_handle("https://vnexpress.net/a-1.html")
    assert reply.startswith("❌ Không đọc được bài này")


async def test_read_url_chi_raise_web_reader_error(monkeypatch):
    async def timeout(*args, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(web_reader, "_get_public", timeout)
    with pytest.raises(web_reader.WebReaderError):
        await web_reader.read_url("https://vnexpress.net/a-1.html")


# 10. Giờ đăng không ghi múi giờ là giờ Việt Nam, không phải giờ server.
def test_gio_dang_khong_mui_gio(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    assert article_reader._format_published("2026-10-21T15:30:00") == "15:30 21/10/2026"
