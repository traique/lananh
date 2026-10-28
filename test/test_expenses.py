"""Chi tiêu (services/expense_service.py): đọc tiền VN bằng code, ghi bằng chat/lệnh/
ảnh hoá đơn, huỷ, tổng theo danh mục, mục bản tin sáng - DB giả trong RAM."""

import datetime as dt

import pytest

from core import database as db
from services import expense_service as ex
from services import morning_brief
from services import tool_registry as reg
from services.reminder_delivery import NotificationTarget, notification_target

TODAY = dt.date(2026, 10, 26)


@pytest.mark.parametrize(
    ("text", "amount", "note", "category"),
    [
        ("cafe 45k", 45_000, "Cafe", "an_uong"),
        ("đổ xăng 1tr2", 1_200_000, "Đổ xăng", "di_lai"),
        ("2 củ tiền điện", 2_000_000, "Tiền điện", "hoa_don"),
        ("45k cafe", 45_000, "Cafe", "an_uong"),
        ("grab 85.000đ", 85_000, "Grab", "di_lai"),
        ("1.200.000 tiền học", 1_200_000, "Tiền học", "giao_duc"),
        ("bánh mì 2k5", 2_500, "Bánh mì", "an_uong"),
        ("taxi 1m5", 1_500_000, "Taxi", "di_lai"),
        ("xem phim 120 nghìn", 120_000, "Xem phim", "giai_tri"),
        ("cafe không đường 30k", 30_000, "Cafe không đường", "an_uong"),
        ("cafe 45k nha em", 45_000, "Cafe", "an_uong"),
        ("quà cho em 200k", 200_000, "Quà cho em", "gia_dinh"),
        ("ghi chi tiêu: ăn trưa 60k", 60_000, "Ăn trưa", "an_uong"),
    ],
)
def test_doc_tien_viet_nam(text, amount, note, category):
    parsed = ex.parse_expense(text, today=TODAY)
    assert parsed == ex.ParsedExpense(amount, note, category, TODAY)


def test_hom_qua_va_cau_khong_phai_khoan_chi():
    assert ex.parse_expense("hôm qua ăn phở 50k", today=TODAY).spent_on == TODAY - dt.timedelta(1)
    for text in (
        "giá vàng hôm nay 85tr",
        "FPT 120k",
        "lương tháng này 20tr",
        "mua FPT 100 giá 120",
        "cafe 45k đắt quá không",
        "cafe 45k?",
        "ăn trưa 60k và cafe 30k",  # 2 số tiền: để router AI tách
        "cafe 45",  # số trần không đơn vị: không đoán
        "5k",
        "hôm nay trời đẹp",
    ):
        assert ex.parse_expense(text, today=TODAY) is None, text
    # Lệnh /chitieu (không strict): số trần < 1.000 hiểu là nghìn đồng.
    assert ex.parse_expense("cafe 45", today=TODAY, strict=False).amount == 45_000


def test_ky_tong_hop():
    assert ex.period_range("", TODAY)[2] == "tháng 10/2026"
    assert ex.period_range("tháng trước", TODAY)[:2] == (dt.date(2026, 9, 1), dt.date(2026, 10, 1))
    assert ex.period_range("tuần này", TODAY)[:2] == (dt.date(2026, 10, 26), dt.date(2026, 11, 2))
    assert ex.period_range("tháng 12", TODAY)[2] == "tháng 12/2025"
    assert ex.period_range("hôm qua", TODAY)[:2] == (dt.date(2026, 10, 25), TODAY)


class FakeExpenses:
    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.next_id = 0

    async def add(self, uid, amount, **kw):
        key = kw.get("event_key")
        for row in self.rows.values():
            if key and row["event_key"] == key:
                return row["id"], False
        self.next_id += 1
        self.rows[self.next_id] = dict(id=self.next_id, uid=uid, amount_vnd=amount, **kw)
        return self.next_id, True

    async def count(self, uid, within):
        return sum(1 for r in self.rows.values() if r["uid"] == uid)

    async def delete(self, uid, eid):
        row = self.rows.get(eid)
        if not row or row["uid"] != uid:
            return None
        return self.rows.pop(eid)

    async def last(self, uid, within):
        mine = [r for r in self.rows.values() if r["uid"] == uid]
        return mine[-1] if mine else None

    async def totals(self, uid, start, end):
        out: dict[str, dict] = {}
        for r in self.rows.values():
            if r["uid"] == uid and start <= r["spent_on"] < end:
                row = out.setdefault(
                    r["category"], {"category": r["category"], "total": 0, "items": 0}
                )
                row["total"] += r["amount_vnd"]
                row["items"] += 1
        return sorted(out.values(), key=lambda r: -r["total"])

    async def recent(self, uid, limit=10):
        return [r for r in self.rows.values() if r["uid"] == uid][-limit:]


@pytest.fixture
def fake(monkeypatch):
    store = FakeExpenses()
    monkeypatch.setattr(db, "expense_add", store.add)
    monkeypatch.setattr(db, "expense_count_since", store.count)
    monkeypatch.setattr(db, "expense_delete", store.delete)
    monkeypatch.setattr(db, "expense_last", store.last)
    monkeypatch.setattr(db, "expense_totals", store.totals)
    monkeypatch.setattr(db, "expense_recent", store.recent)
    return store


@pytest.mark.asyncio
async def test_chat_ghi_ngay_va_huy(fake):
    with notification_target(NotificationTarget("telegram", "1", event_key="telegram:5")):
        reply = await ex.maybe_handle_chat(1, "cafe 45k")
        again = await ex.maybe_handle_chat(1, "cafe 45k")  # webhook gửi lại cùng update
    assert reply.startswith("💸 Dạ em ghi rồi: #1 45.000đ - Cafe [Ăn uống]")
    assert '"huỷ"' in reply and "/chitieu xoa 1" in reply
    assert again.startswith("💸 Dạ em ghi rồi: #1") and len(fake.rows) == 1
    assert fake.rows[1]["event_key"] == "telegram:5:chi"

    assert await ex.maybe_handle_chat(1, "chào em") is None
    assert "huỷ khoản #1" in await ex.maybe_handle_chat(1, "huỷ")
    assert not fake.rows
    assert await ex.maybe_handle_chat(1, "huỷ") is None  # không còn gì để huỷ -> chat thường


@pytest.mark.asyncio
async def test_tat_ghi_tu_dong_bang_env(fake, monkeypatch):
    monkeypatch.setattr(ex.config, "EXPENSE_CHAT_CAPTURE", False)
    assert await ex.maybe_handle_chat(1, "cafe 45k") is None and not fake.rows


@pytest.mark.asyncio
async def test_lenh_chitieu_tong_ds_xoa(fake):
    today = dt.datetime.now(ex.VN_TZ).date().isoformat()
    out = (await ex.handle_command("/chitieu", "cafe 45k", 1))[0]
    assert "#1 45.000đ - Cafe" in out and fake.rows[1]["source"] == "command"
    await ex.handle_command("/chitieu", "đổ xăng 80k", 1)
    await ex.handle_command("/chitieu", "ăn trưa 60k", 1)
    await ex.handle_command("/chitieu", "trà sữa 30k", -5)  # người khác

    summary = (await ex.handle_command("/chitieu", "", 1))[0]
    assert summary.splitlines()[0].endswith(": 185.000đ")
    assert "• Ăn uống: 105.000đ (2 khoản, 56%)" in summary
    assert "• Đi lại: 80.000đ (1 khoản, 43%)" in summary
    assert "185.000đ" in (await ex.handle_command("/chitieu", "tháng này", 1))[0]
    assert all(r["spent_on"] == today for r in fake.rows.values())

    listing = (await ex.handle_command("/chitieu", "ds", 1))[0]
    assert "#3" in listing and "Trà sữa" not in listing
    assert "Không thấy khoản #4" in (await ex.handle_command("/chitieu_xoa", "4", 1))[0]
    assert "Đã xoá khoản #2" in (await ex.handle_command("/chitieu", "xoa 2", 1))[0]
    assert "chưa đọc được số tiền" in (await ex.handle_command("/chitieu", "abc", 1))[0]


@pytest.mark.asyncio
async def test_tool_router_chi_nhan_so_tien_co_trong_tin_nhan(fake):
    result = await ex.record_from_tool(
        1, ["đổ xăng 80k", "ăn trưa 70k"], source_text="hôm qua đổ xăng hết 80k, ăn trưa 60k"
    )
    assert result.status == reg.STATUS_OK
    assert [r["amount_vnd"] for r in fake.rows.values()] == [80_000]  # 70k là LLM tự đặt
    assert "70.000đ không có trong tin nhắn" in result.text
    bad = await ex.record_from_tool(1, ["ăn trưa"], source_text="ăn trưa")
    assert bad.status == reg.STATUS_INVALID


@pytest.mark.asyncio
async def test_anh_hoa_don_luu_nhap(fake, monkeypatch):
    from ai import orchestrator

    class Response:
        text = '```json\n{"amount_vnd": 125000, "date": "", "merchant": "Highlands Coffee"}\n```'

    async def analyze(instruction, path):
        assert "hoá đơn" in instruction
        return Response()

    monkeypatch.setattr(orchestrator, "analyze_image", analyze)
    reply = await ex.analyze_receipt(1, "/tmp/khong-can-file.jpg")
    assert "125.000đ - Hoá đơn Highlands Coffee [Ăn uống]" in reply and "nháp" in reply
    row = fake.rows[1]
    assert row["draft"] is True and row["source"] == "receipt"

    class Empty:
        text = '{"amount_vnd": 0}'

    async def empty(instruction, path):
        return Empty()

    monkeypatch.setattr(orchestrator, "analyze_image", empty)
    assert "không thấy tổng tiền" in await ex.analyze_receipt(1, "/tmp/x.jpg")
    assert ex.is_receipt_caption("hoá đơn ăn trưa") and not ex.is_receipt_caption("cô gái 20")


@pytest.mark.asyncio
async def test_zalo_anh_hoa_don_khong_viet_prompt(fake, monkeypatch, tmp_path):
    from services import channel_image_service as cis

    async def receipt(user_id, path):
        return f"đã đọc {user_id}"

    async def no_prompt(*args, **kwargs):
        raise AssertionError("ảnh hoá đơn không được đi luồng viết prompt")

    recorded = []

    async def record(user_id, user_text, reply, kind="chat", **kwargs):
        recorded.append((user_id, user_text, reply, kind))
        return True

    monkeypatch.setattr(cis.config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(cis.expense_service, "analyze_receipt", receipt)
    monkeypatch.setattr(cis.orchestrator, "analyze_image", no_prompt)
    monkeypatch.setattr(cis.conversation_log, "record_exchange", record)
    messages, _ = await cis.handle_channel_image(-5, b"jpg", "image/jpeg", "bill", "m1")
    assert messages == ["đã đọc -5"] and not list(tmp_path.iterdir())
    # Câu trả lời hoá đơn cũng vào lịch sử (kind expense) cho câu hỏi nối tiếp.
    assert recorded == [(-5, "[Gửi ảnh] bill", "đã đọc -5", "expense")]


@pytest.mark.asyncio
async def test_ban_tin_sang_dong_chi_tieu_rieng_tung_nguoi(fake, monkeypatch):
    now = dt.datetime.now(ex.VN_TZ)
    await ex.handle_command("/chitieu", "cafe 45k", -5)
    section = next(s for s in morning_brief.sections() if s.key == "expenses")
    settings = morning_brief.BriefSettings(on=True)
    member = morning_brief.BriefContext(-5, "zalo", now, settings, False)
    assert (await section.build(member)).startswith("💸 Chi tiêu tháng")
    owner = morning_brief.BriefContext(1, "telegram", now, settings, True)
    assert await section.build(owner) is None  # chủ bot chưa ghi khoản nào
    monkeypatch.setattr(ex.config, "MORNING_BRIEF_EXPENSES", False)
    assert await section.build(member) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "nhắc anh 9h mai trả 500k",
        "nhắc em mai đóng tiền điện 1tr2",
        "mai nhắc anh chuyển 2tr cho mẹ",
        "ghi chú: mượn Hùng 500k",
        "nhớ giúp anh tiền nhà 5tr",
        "nhac anh 9h mai tra 500k",  # gõ không dấu
    ],
)
async def test_cau_nhac_viec_ghi_chu_co_so_tien_khong_bi_ghi_thanh_khoan_chi(fake, text):
    # Câu nhắc việc/ghi chú/nhờ nhớ kèm số tiền phải đi tiếp tới router tool.
    assert ex.parse_expense(text, today=TODAY) is None
    assert await ex.maybe_handle_chat(1, text) is None
    assert not fake.rows


@pytest.mark.asyncio
async def test_khoan_chi_thuong_van_ghi_sau_khi_chan_cau_nhac(fake):
    assert (await ex.maybe_handle_chat(1, "cafe 45k")).startswith("💸")
    # "ghi lại chi tiêu ..." vẫn là khoản chi (không phải ghi chú).
    assert ex.parse_expense("ghi lại chi tiêu ăn trưa 60k", today=TODAY).amount == 60_000
    assert ex.parse_expense("nhạc cụ 500k", today=TODAY).amount == 500_000  # nhạc != nhắc


def test_hom_qua_viet_hoa_dau_cau_van_tinh_dung_ngay():
    # Bàn phím điện thoại tự viết hoa chữ đầu câu.
    parsed = ex.parse_expense("Hôm qua ăn phở 50k", today=TODAY)
    assert parsed == ex.ParsedExpense(50_000, "Ăn phở", "an_uong", TODAY - dt.timedelta(1))
    kia = ex.parse_expense("Hôm kia đổ xăng 80k", today=TODAY)
    assert kia is not None and kia.spent_on == TODAY - dt.timedelta(2) and kia.note == "Đổ xăng"
    assert ex.parse_expense("Hôm nay cafe 45k", today=TODAY).note == "Cafe"


@pytest.mark.parametrize(
    ("raw", "amount"),
    [
        (125000, 125_000),
        (125000.0, 125_000),  # JSON số thực: không được nhân 10
        ('"125.000"', 125_000),
        ('"125,000.00"', 125_000),  # không được nhân 100
        ('"125.000,00"', 125_000),
        ('"125.000đ"', 125_000),
        ('"125000"', 125_000),
        ("true", None),
        ('"abc"', None),
        ("0", None),
    ],
)
def test_hoa_don_doc_tong_tien_dung_kieu_so(raw, amount):
    parsed = ex.parse_receipt_json(f'{{"amount_vnd": {raw}}}', TODAY)
    assert (parsed.amount if parsed else None) == amount


@pytest.mark.parametrize(
    ("text", "amount", "note"),
    [
        ("tiền điện 1 triệu 2", 1_200_000, "Tiền điện"),
        ("học phí 2 triệu 500", 2_500_000, "Học phí"),
        ("tiền nhà 3 triệu rưỡi", 3_500_000, "Tiền nhà"),
        ("tiền nhà 3tr rưỡi", 3_500_000, "Tiền nhà"),
        ("sửa xe 1 triệu 200 nghìn", 1_200_000, "Sửa xe"),
        ("tiền điện 1 triệu 2 nghìn", 1_002_000, "Tiền điện"),
        ("học phí 2 triệu 3 tháng", 2_000_000, "Học phí 3 tháng"),  # "3 tháng" không phải phần lẻ
        ("taxi 1m5", 1_500_000, "Taxi"),
    ],
)
def test_tien_noi_kieu_1_trieu_2_va_ruoi(text, amount, note):
    parsed = ex.parse_expense(text, today=TODAY)
    assert parsed is not None and (parsed.amount, parsed.note) == (amount, note)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "thần số học ngày 18/09/1988",  # từng bị ghi thành khoản chi 1.988đ
        "than so hoc 18-09-1988",
        "xem tử vi 18.9.1988",
        "tử vi tuổi 1990",
        "sinh năm 1988",
        "tu vi 1995 nam",
    ],
)
async def test_ngay_sinh_va_nam_sinh_khong_phai_tien(fake, text):
    assert ex.parse_expense(text, today=TODAY) is None, text
    assert await ex.maybe_handle_chat(1, text) is None and not fake.rows


def test_khoan_chi_co_ngay_van_doc_dung_so_tien():
    assert ex.parse_expense("18/10 ăn phở 50k", today=TODAY).amount == 50_000
    assert ex.parse_expense("gửi xe 2000", today=TODAY).amount == 2_000
    assert ex.parse_expense("xem tử vi 200k", today=TODAY).amount == 200_000
    # /chitieu: số trong ngày tháng không bị hiểu là nghìn đồng
    assert ex.parse_expense("18/10 cafe 45", today=TODAY, strict=False).amount == 45_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["con trai anh giờ cao 1m7 rồi", "bơi 500m", "chạy bộ 5k sáng nay", "nay chạy 10k mệt quá"],
)
async def test_chieu_cao_quang_duong_khong_phai_tien(fake, text):
    assert ex.parse_expense(text, today=TODAY) is None
    assert await ex.maybe_handle_chat(1, text) is None and not fake.rows
