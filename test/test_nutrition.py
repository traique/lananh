"""Nhật ký ăn uống /an (services/nutrition_service.py): đọc món/bữa/số phần bằng code,
kcal tra bảng assets/thuc_pham.yaml, câu tự nhiên chỉ khi NUTRITION_NL=1 và không nuốt
câu nhắc việc/chi tiêu/chat."""

import datetime as dt

import pytest

from core import database as db
from services import channel_command_service, nutrition_service as n, reply_audience

NOON = dt.datetime(2026, 10, 26, 12, 30, tzinfo=n.VN_TZ)
MORNING = dt.datetime(2026, 10, 26, 9, 0, tzinfo=n.VN_TZ)


class FakeMeals:
    def __init__(self):
        self.rows: list[dict] = []
        self.next_id = 1

    def install(self, monkeypatch):
        monkeypatch.setattr(db, "meal_add", self.add)
        monkeypatch.setattr(db, "meal_count_since", self.count)
        monkeypatch.setattr(db, "meal_day", self.day)
        monkeypatch.setattr(db, "meal_day_totals", self.totals)
        monkeypatch.setattr(db, "meal_delete", self.delete)

    async def add(self, uid, **kw):
        for row in self.rows:
            if kw["event_key"] and row["event_key"] == kw["event_key"]:
                return row["id"], False
        row = {"id": self.next_id, "uid": uid, **kw}
        self.next_id += 1
        self.rows.append(row)
        return row["id"], True

    async def count(self, uid, within):
        return sum(1 for r in self.rows if r["uid"] == uid)

    async def day(self, uid, day, limit=40):
        return [r for r in self.rows if r["uid"] == uid and r["eaten_on"] == day][:limit]

    async def totals(self, uid, start, end):
        days: dict[str, dict] = {}
        for r in self.rows:
            if r["uid"] != uid or not start <= r["eaten_on"] < end:
                continue
            d = days.setdefault(
                r["eaten_on"], {"eaten_on": r["eaten_on"], "kcal": 0, "items": 0, "unknown": 0}
            )
            d["items"] += 1
            if r["kcal"] is None:
                d["unknown"] += 1
            else:
                d["kcal"] += r["kcal"]
        return [days[k] for k in sorted(days)]

    async def delete(self, uid, meal_id):
        for r in self.rows:
            if r["uid"] == uid and r["id"] == meal_id:
                self.rows.remove(r)
                return r
        return None


@pytest.fixture
def meals(monkeypatch):
    fake = FakeMeals()
    fake.install(monkeypatch)
    monkeypatch.delenv("NUTRITION_NL", raising=False)
    monkeypatch.delenv("NUTRITION_AI_ESTIMATE", raising=False)
    return fake


# ─── Đọc món ────────────────────────────────────────────────────────────────


def _items(parsed):
    return [(i.text, i.portion, i.food.name if i.food else None, i.kcal) for i in parsed.items]


def test_bang_mon_doc_duoc_va_du_mon_pho_bien():
    assert n.food_count() >= 100
    assert n.match_food("phở bò tái").name == "Phở bò"
    assert n.match_food("bun bo hue").name == "Bún bò Huế"  # không dấu vẫn khớp
    # Khớp dài nhất, câu có dấu so bản còn dấu: "cá kho" không lẫn "cà phê".
    assert n.match_food("cá kho").name == "Cá kho"
    assert n.match_food("trà sữa").name == "Trà sữa trân châu"
    assert n.match_food("bánh đúc") is None


def test_doc_mon_bua_so_phan_va_ngay():
    parsed = n.parse_meal("2 bát cơm, cá kho tối", now=NOON)
    assert parsed.meal == "toi" and parsed.eaten_on == NOON.date()
    assert _items(parsed) == [("Cơm", 2.0, "Cơm trắng", 400), ("Cá kho", 1.0, "Cá kho", 250)]
    parsed = n.parse_meal("hôm qua bún chả trưa", now=NOON)
    assert parsed.eaten_on == dt.date(2026, 10, 25) and parsed.meal == "trua"
    assert _items(n.parse_meal("nửa tô phở", now=NOON)) == [("Phở", 0.5, "Phở bò", 225)]
    assert _items(n.parse_meal("trà sữa x2", now=NOON))[0][1:] == (2.0, "Trà sữa trân châu", 900)
    assert _items(n.parse_meal("1 tô rưỡi bún bò", now=NOON))[0][1] == 1.5
    # Bữa không nói thì đoán theo giờ VN.
    assert n.parse_meal("bánh mì", now=MORNING).meal == "sang"
    assert n.parse_meal("sáng: bánh mì trứng + cà phê sữa", now=NOON).meal == "sang"


def test_chu_giong_so_khong_thanh_so_phan():
    # "hải sản" (fold = "hai san"), "nấm" (fold = "nam") không phải 2 phần/5 phần.
    assert _items(n.parse_meal("hải sản nướng", now=NOON)) == [("Hải sản nướng", 1.0, None, None)]
    assert _items(n.parse_meal("nấm xào", now=NOON))[0][1] == 1.0
    assert _items(n.parse_meal("hai bát cơm", now=NOON))[0][1] == 2.0


# ─── Lệnh /an ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_ghi_xem_hom_nay_tuan_va_xoa(meals):
    reply = (await n.handle_command("/an", "phở bò sáng", 42))[0]
    assert "#1 Phở bò (1 tô) - ~450 kcal" in reply and "ước lượng" in reply
    assert "/an xoa 1" in reply
    reply = (await n.handle_command("/an", "bánh đúc, 2 bát cơm tối", 42))[0]
    assert "#2 Bánh đúc - chưa có số kcal" in reply and "chỉ ghi tên" in reply
    assert "#3 Cơm trắng (2 × 1 bát) - ~400 kcal" in reply
    assert meals.rows[1]["kcal"] is None and meals.rows[1]["kcal_source"] == "none"

    today = (await n.handle_command("/an", "", 42))[0]
    assert "Bữa sáng:" in today and "Bữa tối:" in today
    assert "Tổng ~850 kcal (chưa tính 1 món không có số)." in today
    week = (await n.handle_command("/an", "tuan", 42))[0]
    assert "~850 kcal (3 món, 1 món chưa có số)" in week

    assert "Đã xoá món #2 Bánh đúc" in (await n.handle_command("/an", "xoa 2", 42))[0]
    assert "Không thấy món #2" in (await n.handle_command("/an", "xoa 2", 42))[0]
    # Dữ liệu tách theo người: Zalo -5 không thấy/xoá được món của chủ bot.
    assert "chưa ghi bữa nào" in (await n.handle_command("/an", "", -5))[0]
    assert "Không thấy món #1" in (await n.handle_command("/an", "xoa 1", -5))[0]


@pytest.mark.asyncio
async def test_an_qua_kenh_zalo_dung_cach_xung_ho_va_co_trong_help(meals):
    with reply_audience.use(reply_audience.ReplyAudience(address_form="chị")):
        outputs, _ = await channel_command_service.maybe_handle_command(-5, "/an", False, "zalo")
    assert "Hôm nay chị chưa ghi bữa nào" in outputs[0]
    assert "/an phở bò sáng" in channel_command_service.HELP_MEMBER
    assert "/noi on|off|auto" in channel_command_service.HELP_MEMBER


@pytest.mark.asyncio
async def test_tran_mon_moi_ngay(meals, monkeypatch):
    async def many(uid, within):
        return n.MAX_PER_DAY

    monkeypatch.setattr(db, "meal_count_since", many)
    assert (await n.handle_command("/an", "phở", 42))[0].startswith("❌ Đã ghi 40 món")


@pytest.mark.asyncio
async def test_uoc_luong_ai_chi_khi_bat_va_qua_bounded(meals, monkeypatch):
    from ai import orchestrator

    calls = []

    async def utility_json(prompt):
        calls.append(prompt)
        return {"kcal": 320}

    monkeypatch.setattr(orchestrator, "utility_json", utility_json)
    await n.handle_command("/an", "bánh đúc", 42)
    assert calls == [] and meals.rows[-1]["kcal"] is None  # mặc định không gọi AI
    monkeypatch.setenv("NUTRITION_AI_ESTIMATE", "1")
    reply = (await n.handle_command("/an", "2 bánh đúc", 42))[0]
    assert len(calls) == 1 and "bánh đúc" in calls[0].lower()
    assert meals.rows[-1]["kcal"] == 640 and meals.rows[-1]["kcal_source"] == "ai"
    assert "AI ước lượng" in reply

    async def nonsense(prompt):
        return {"kcal": 99999}

    monkeypatch.setattr(orchestrator, "utility_json", nonsense)
    await n.handle_command("/an", "đồ lạ", 42)
    assert meals.rows[-1]["kcal"] is None  # số vô lý -> không có kcal


# ─── Câu tự nhiên ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "trưa nay anh ăn bún chả",
        "sáng nay ăn phở",
        "ghi ăn bánh đúc",
        "ghi bữa trưa: cơm tấm",
        "trua nay anh an bun cha",
        "trưa nay ăn canh chua",
        "tối qua em ăn cơm gà",
    ],
)
def test_cau_tu_nhien_ro_rang(text):
    assert n.parse_natural(text, now=NOON) is not None


@pytest.mark.parametrize(
    "text",
    [
        "trưa nay ăn gì",
        "trưa nay ăn gì?",
        "trưa nay anh ăn với khách",  # "khách" không phải món -> chat AI
        "tối nay anh ăn lẩu",  # 12h30 mà nói tối nay: kế hoạch
        "tối nay định ăn lẩu",
        "trưa nay ăn phở 50k",  # có số tiền: để chi tiêu ghi
        "nhắc anh trưa nay ăn thuốc",  # bài học exp-1: câu nhắc việc
        "nhớ giúp anh sáng nay ăn phở",
        "ghi ân thầy cô",
        "ghi an banh duc",  # không dấu + món lạ: không chắc
        "trưa nay ăn bún đậu ngon không",
        "hôm nay anh ăn chay",
        "cafe 45k",
    ],
)
def test_cau_khong_phai_ghi_bua_an(text):
    assert n.parse_natural(text, now=NOON) is None


@pytest.mark.asyncio
async def test_cau_tu_nhien_mac_dinh_tat(meals, monkeypatch):
    assert await n.maybe_handle_chat(42, "trưa nay anh ăn bún chả") is None
    assert meals.rows == []
    monkeypatch.setenv("NUTRITION_NL", "1")
    monkeypatch.setattr(n, "parse_natural", lambda text: n.parse_meal("bún chả trưa", now=NOON))
    reply = await n.maybe_handle_chat(42, "trưa nay anh ăn bún chả")
    assert "#1 Bún chả" in reply and meals.rows[0]["source"] == "chat"


@pytest.mark.asyncio
async def test_cau_nhac_viec_khong_bi_nuot_khi_bat_nl(meals, monkeypatch):
    monkeypatch.setenv("NUTRITION_NL", "1")
    assert await n.maybe_handle_chat(42, "nhắc anh 12h trưa nay ăn cơm với khách") is None
    assert await n.maybe_handle_chat(42, "trưa nay ăn gì ngon em") is None
    assert meals.rows == []


def test_tin_thoai_cau_ghi_bua_an_bi_chan_khi_bat_nl(monkeypatch):
    from services import voice_service

    monkeypatch.setattr(n, "parse_natural", lambda text: n.parse_meal("phở", now=NOON))
    assert voice_service.write_statement_kind("sáng nay ăn phở") is None  # NL đang tắt
    monkeypatch.setenv("NUTRITION_NL", "1")
    assert voice_service.write_statement_kind("sáng nay ăn phở") == "meal"
    assert "bữa ăn" in voice_service.write_blocked_reply("meal", owner=True)
