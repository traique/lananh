"""Unit test cho services/tools.py: router JSON + thực thi tool, kết quả trung thực
(ToolOutcome - không bao giờ None im lặng), giờ nhắc tính bằng code."""

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import official_client, orchestrator  # noqa: E402
from core import database as db  # noqa: E402
from services import tool_registry as reg  # noqa: E402
from services import tools, web_search  # noqa: E402

VN = tools._VN_TZ


class _FrozenDatetime(dt.datetime):
    frozen = dt.datetime(2026, 10, 26, 8, 30, tzinfo=VN)  # Thứ Hai

    @classmethod
    def now(cls, tz=None):
        return cls.frozen if tz is None else cls.frozen.astimezone(tz)


@pytest.fixture(autouse=True)
def _no_history_no_chain(monkeypatch):
    """Không đọc DB lịch sử, router dự phòng qua provider-chain mặc định lỗi."""

    async def no_history(user_id, turns=3):
        return ""

    async def chain_down(prompt, **kwargs):
        raise RuntimeError("provider-chain lỗi mô phỏng")

    monkeypatch.setattr(web_search, "recent_history_text", no_history)
    monkeypatch.setattr(orchestrator, "ask", chain_down)


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(tools, "datetime", _FrozenDatetime)
    return _FrozenDatetime.frozen


@pytest.fixture
def reminders(monkeypatch):
    """Ghi lại reminders 1 lần / chuỗi lặp được tạo (không DB)."""
    calls = {"once": [], "series": []}

    async def add_reminder(uid, message, due_at, **delivery):
        calls["once"].append((uid, message, due_at, delivery.get("event_key")))
        return due_at

    async def create_series(uid, message, **kw):
        calls["series"].append((uid, message, kw))
        return 7, True

    async def zero(uid):
        return 0

    monkeypatch.setattr(db, "add_reminder", add_reminder)
    monkeypatch.setattr(db, "reminder_create_series", create_series)
    monkeypatch.setattr(db, "reminder_count_pending_once", zero)
    monkeypatch.setattr(db, "reminder_count_series", zero)
    return calls


def _router(monkeypatch, data):
    async def fake(prompt):
        fake.prompts.append(prompt)
        return data

    fake.prompts = []
    monkeypatch.setattr(official_client, "generate_utility_json", fake)
    return fake


@pytest.mark.asyncio
async def test_save_note_duoc_route_va_thuc_thi(monkeypatch):
    calls = []

    async def fake_add_note(uid, content, **delivery):
        calls.append(("add_note", uid, content))

    monkeypatch.setattr(db, "add_note", fake_add_note)
    _router(monkeypatch, {"tool": "save_note", "args": {"content": "Tuần sau họp đối tác Nhật"}})

    outcome = await tools.maybe_run_tool(1, "ghi chú giúp anh tuần sau họp đối tác Nhật")

    assert calls == [("add_note", 1, "Tuần sau họp đối tác Nhật")]
    assert outcome.status == reg.STATUS_OK and outcome.saved
    assert "Tuần sau họp đối tác Nhật" in outcome.grounding()
    assert "ĐÃ được lưu thật" in outcome.grounding()


@pytest.mark.asyncio
async def test_tool_none_khong_chay_handler_nao(monkeypatch):
    _router(monkeypatch, {"tool": "none", "args": {}})

    outcome = await tools.maybe_run_tool(1, "nhớ mua rau về ăn tối nhé em")

    assert outcome.status == reg.STATUS_NONE and outcome.router_ran
    assert outcome.grounding() == ""  # không có ý định ghi -> không chèn gì


@pytest.mark.asyncio
async def test_loi_router_khong_raise(monkeypatch):
    async def fake_raise(prompt):
        raise RuntimeError("mô phỏng lỗi API")

    monkeypatch.setattr(official_client, "generate_utility_json", fake_raise)

    outcome = await tools.maybe_run_tool(1, "ghi chú vài chữ bất kỳ")

    # Không raise; không có ý định ghi rõ ràng -> không ép câu "chưa lưu được".
    assert outcome.status == reg.STATUS_UNAVAILABLE and not outcome.not_saved_hard


@pytest.mark.asyncio
async def test_router_hong_ma_co_y_dinh_nhac_thi_bao_chua_luu(monkeypatch, reminders):
    """Router None, nhắc không đọc được giờ bằng code, provider-chain cũng lỗi ->
    unavailable + cờ báo cứng (lượt chat nối "⚠️ chưa lưu được")."""
    _router(monkeypatch, None)

    outcome = await tools.maybe_run_tool(1, "nhắc anh gọi mẹ nhé")

    assert outcome.status == reg.STATUS_UNAVAILABLE and outcome.intent == "reminder"
    assert outcome.not_saved_hard
    assert "CHƯA lưu được nhắc việc" in outcome.grounding()
    assert reminders["once"] == []


@pytest.mark.asyncio
async def test_router_hong_van_dat_duoc_nhac_bang_code(monkeypatch, reminders, frozen):
    _router(monkeypatch, None)

    outcome = await tools.maybe_run_tool(1, "nhắc anh 8h tối mai gọi mẹ")

    assert outcome.status == reg.STATUS_OK and outcome.saved
    assert reminders["once"][0][1] == "Gọi mẹ"
    assert reminders["once"][0][2] == dt.datetime(2026, 10, 27, 20, 0, tzinfo=VN)


@pytest.mark.asyncio
@pytest.mark.parametrize("router_data", [None, {}])
@pytest.mark.parametrize(
    "text",
    [
        "bỏ nhắc uống thuốc 7h sáng mỗi ngày",
        "huỷ nhắc 8h tối mai gọi mẹ",
        "hôm qua em nhắc anh 8h rồi mà",
        "đừng nhắc anh 8h tối mai nữa",
        "huy nhac 8h toi mai goi me",  # gõ không dấu
    ],
)
async def test_router_hong_khong_dat_nhac_cho_cau_huy_hoac_ke_chuyen(
    monkeypatch, reminders, frozen, router_data, text
):
    """Đường code tất định chỉ dành cho câu ĐẶT nhắc: câu huỷ/kể chuyện đã qua không
    được thành nhắc mới khi router hỏng (chain cũng hỏng -> báo chưa lưu được)."""
    _router(monkeypatch, router_data)

    outcome = await tools.maybe_run_tool(1, text)

    assert reminders["once"] == [] and reminders["series"] == []
    assert outcome.status == reg.STATUS_UNAVAILABLE and not outcome.saved


@pytest.mark.asyncio
async def test_router_provider_chain_du_phong(monkeypatch, reminders, frozen):
    _router(monkeypatch, None)

    async def chain(prompt, **kwargs):
        class R:
            text = '{"tool": "set_reminder", "args": {"message": "gọi mẹ", "when": "8h tối mai"}}'

        return R()

    monkeypatch.setattr(orchestrator, "ask", chain)
    # Câu không đọc được bằng code (không có giờ) nhưng chain trả tool hợp lệ.
    outcome = await tools.maybe_run_tool(1, "nhắc anh việc gọi mẹ nha em")

    assert outcome.status == reg.STATUS_OK
    assert reminders["once"][0][2] == dt.datetime(2026, 10, 27, 20, 0, tzinfo=VN)


@pytest.mark.asyncio
async def test_set_reminder_gio_da_qua_khong_noi_ngay_thi_sang_mai(monkeypatch, reminders, frozen):
    """Thay cho sàn "+1 phút" cũ: 7h sáng nói lúc 8h30 -> 7h sáng MAI, không nhắc ngay."""
    _router(
        monkeypatch, {"tool": "set_reminder", "args": {"message": "uống thuốc", "when": "7h sáng"}}
    )

    outcome = await tools.maybe_run_tool(1, "nhắc anh 7h sáng uống thuốc")

    assert reminders["once"][0][2] == dt.datetime(2026, 10, 27, 7, 0, tzinfo=VN)
    assert outcome.confirmation_key == "07:00|7h|7 giờ"  # các cách viết giờ chấp nhận
    assert "Thứ Ba 27/10" in outcome.confirmation
    assert "1 lần" in outcome.text and "KHÔNG nói là sẽ nhắc mỗi ngày" in outcome.text


@pytest.mark.asyncio
async def test_set_reminder_dung_ngay_router_khi_code_khong_doc_duoc_ngay(
    monkeypatch, reminders, frozen
):
    """Router chép giờ vào "when" nhưng để ngày ở "date" (hoặc "when" có chữ ngày code
    chưa hiểu): ghép ngày của router, giờ vẫn do code đọc; câu xác nhận phải có ngày."""
    cases = [
        ({"when": "8h sáng", "date": "2026-11-15"}, dt.datetime(2026, 11, 15, 8, 0, tzinfo=VN)),
        ({"when": "tuần sau 9h", "date": "2026-11-02"}, dt.datetime(2026, 11, 2, 9, 0, tzinfo=VN)),
        # Ngày router = hôm nay nhưng giờ đã qua: code vẫn lăn sang mai như cũ.
        ({"when": "7h sáng", "date": "2026-10-26"}, dt.datetime(2026, 10, 27, 7, 0, tzinfo=VN)),
    ]
    for args, due in cases:
        _router(monkeypatch, {"tool": "set_reminder", "args": {"message": "họp", **args}})
        outcome = await tools.maybe_run_tool(1, "nhắc anh họp " + args["when"])
        assert outcome.saved and reminders["once"][-1][2] == due, args
    assert outcome.confirmation_key == "07:00|7h|7 giờ"  # nhắc ngày mai: chỉ soát giờ

    _router(monkeypatch, {"tool": "set_reminder", "args": {"message": "họp", **cases[0][0]}})
    far = await tools.maybe_run_tool(1, "nhắc anh họp 8h sáng ngày 15/11")
    assert far.confirmation_key.split("&")[1].split("|")[:2] == ["15/11", "ngày 15"]

    # Không có ngày từ router: hỏi lại, không lặng lẽ đặt ngày mai.
    _router(
        monkeypatch, {"tool": "set_reminder", "args": {"message": "họp", "when": "tuần sau 9h"}}
    )
    unknown = await tools.maybe_run_tool(1, "nhắc anh tuần sau 9h họp")
    assert unknown.status == reg.STATUS_INVALID and "ngày" in unknown.text
    assert len(reminders["once"]) == 4


@pytest.mark.asyncio
async def test_set_reminder_phut_am_hoac_ngay_da_qua_thi_hoi_lai(monkeypatch, reminders, frozen):
    for args in (
        {"message": "uống thuốc", "in_minutes": -5},
        {"message": "họp", "date": "2026-10-25", "time": "09:00"},
        {"message": "họp", "date": "2028-01-01", "time": "09:00"},
        {"message": "họp"},
    ):
        _router(monkeypatch, {"tool": "set_reminder", "args": args})
        outcome = await tools.maybe_run_tool(1, "nhắc anh họp")
        assert outcome.status == reg.STATUS_INVALID, args
        assert "CHƯA lưu" in outcome.grounding() and not outcome.not_saved_hard
    assert reminders["once"] == []


@pytest.mark.asyncio
async def test_set_reminder_code_tinh_gio_khong_tin_so_phut_cua_llm(monkeypatch, reminders, frozen):
    # LLM tính sai in_minutes=5 nhưng chép đúng "8h sáng mai": code tính lại.
    _router(
        monkeypatch,
        {
            "tool": "set_reminder",
            "args": {"message": "họp", "when": "8h sáng mai", "in_minutes": 5},
        },
    )
    await tools.maybe_run_tool(1, "nhắc anh 8h sáng mai họp")
    assert reminders["once"][0][2] == dt.datetime(2026, 10, 27, 8, 0, tzinfo=VN)

    # Thiếu "when": đọc lại câu người dùng.
    _router(monkeypatch, {"tool": "set_reminder", "args": {"message": "gọi mẹ"}})
    await tools.maybe_run_tool(1, "thứ 6 tuần sau 9h nhắc anh gọi mẹ")
    assert reminders["once"][1][2] == dt.datetime(2026, 11, 6, 9, 0, tzinfo=VN)


@pytest.mark.asyncio
async def test_set_reminder_lap_lai_va_nhieu_muc(monkeypatch, reminders, frozen):
    from services.reminder_delivery import NotificationTarget, notification_target

    _router(
        monkeypatch,
        {"tool": "set_reminder", "args": {"message": "uống thuốc", "when": "7h sáng mỗi ngày"}},
    )
    outcome = await tools.maybe_run_tool(1, "nhắc anh 7h sáng mỗi ngày uống thuốc")
    assert outcome.saved and "hằng ngày" in outcome.confirmation
    uid, message, kw = reminders["series"][0]
    assert (uid, message) == (1, "uống thuốc")
    assert kw["recurrence"] == {"type": "daily", "at": "07:00"}
    assert kw["first"][0][0] == dt.datetime(2026, 10, 27, 7, 0, tzinfo=VN)

    _router(
        monkeypatch,
        {
            "tool": "set_reminder",
            "args": {
                "items": [
                    {"message": "uống thuốc", "when": "9h tối nay"},
                    {"message": "họp", "when": "10h sáng mai"},
                ]
            },
        },
    )
    with notification_target(NotificationTarget("telegram", "1", event_key="telegram:55")):
        outcome = await tools.maybe_run_tool(
            1, "nhắc anh 9h tối nay uống thuốc và 10h sáng mai họp"
        )
    keys = [call[3] for call in reminders["once"]]
    assert keys == ["telegram:55:1", "telegram:55:2"]  # mỗi mục 1 event_key riêng
    assert outcome.saved and outcome.confirmation.count("Đã đặt nhắc") == 2


@pytest.mark.asyncio
async def test_add_reminder_loi_db_thi_failed(monkeypatch, reminders):
    async def broken(*args, **kwargs):
        raise ValueError("Thiếu tài khoản Zalo nhận nhắc việc.")

    monkeypatch.setattr(db, "add_reminder", broken)
    _router(monkeypatch, {"tool": "set_reminder", "args": {"message": "x", "when": "30 phút nữa"}})

    outcome = await tools.maybe_run_tool(1, "nhắc anh 30 phút nữa uống thuốc")

    assert outcome.status == reg.STATUS_FAILED and outcome.not_saved_hard
    assert "CHƯA lưu được nhắc việc" in outcome.grounding()


@pytest.mark.asyncio
async def test_tham_so_la_khong_gay_typeerror(monkeypatch):
    async def fake_get_facts(uid):
        return [("ten", "Trai"), ("danh_muc_dau_tu", "FPT, HPG"), ("mau_thich", "xanh")]

    monkeypatch.setattr(db, "get_facts", fake_get_facts)
    _router(monkeypatch, {"tool": "get_portfolio", "args": {"user_id": 999, "loai": "tat_ca"}})

    outcome = await tools.maybe_run_tool(1, "danh mục của anh gồm gì nhỉ")

    assert outcome.status == reg.STATUS_OK
    assert "FPT, HPG" in outcome.text
    assert "mau_thich" not in outcome.text  # không liên quan danh mục -> không lẫn vào


@pytest.mark.asyncio
async def test_list_notes_khong_bi_coi_la_chua_luu(monkeypatch):
    async def notes(uid, limit=10):
        return [("mua sữa", dt.datetime(2026, 10, 25, 9, 0))]

    monkeypatch.setattr(db, "get_notes", notes)
    _router(monkeypatch, {"tool": "list_notes", "args": {}})

    outcome = await tools.maybe_run_tool(1, "em ghi chú gì rồi")

    assert outcome.status == reg.STATUS_OK and not outcome.not_saved_hard
    assert "CHƯA" not in outcome.grounding()


def test_y_dinh_ghi_so_tren_chu_co_dau():
    assert tools.side_effect_intent("nhắc anh 8h mai họp") == "reminder"
    assert tools.side_effect_intent("Bài nhạc anh gửi hay ghê") == ""  # nhạc != nhắc
    assert tools.side_effect_intent("ghi chú giúp anh mua sữa") == "note"
    assert tools.side_effect_intent("huỷ nhắc gọi mẹ") == "reminder"
    assert tools.side_effect_intent("hôm qua em ghi chú gì") == ""
    assert tools.side_effect_intent("nhac anh 8h goi me") == "reminder"  # gõ không dấu


@pytest.mark.asyncio
async def test_router_none_cho_cau_ke_chuyen_chi_chen_dong_mem(monkeypatch):
    _router(monkeypatch, {"tool": "none", "args": {}})

    outcome = await tools.maybe_run_tool(1, "hôm qua em nhắc anh rồi mà anh quên")

    assert outcome.status == reg.STATUS_NONE and not outcome.not_saved_hard
    assert "không được nói là đã ghi chú" in outcome.grounding()


@pytest.mark.asyncio
async def test_luot_chi_doc_khong_chay_tool_ghi(monkeypatch, reminders):
    _router(monkeypatch, {"tool": "set_reminder", "args": {"message": "x", "when": "30 phút nữa"}})

    outcome = await tools.maybe_run_tool(
        1, "nhắc anh 30 phút nữa uống thuốc", allow_side_effects=False
    )

    assert outcome.status == reg.STATUS_BLOCKED and outcome.not_saved_hard
    assert "chỉ đọc" in outcome.text
    assert reminders["once"] == []


@pytest.mark.asyncio
async def test_doc_noi_dung_ngoai_thi_chan_tool_ghi(monkeypatch):
    called = []

    async def handler(user_id, **kwargs):
        called.append(user_id)
        return "ok"

    spec = reg.ToolSpec("ghi_thu", "ghi", reg.object_schema({}), handler, side_effect=True)
    with reg.taint_scope():
        reg.mark_tainted()
        outcome = await tools.execute(spec, 1, {})
    assert outcome.status == reg.STATUS_BLOCKED and not called
    assert not reg.is_tainted()  # hết phạm vi lượt thì sạch lại


@pytest.mark.asyncio
async def test_rag_search_chi_cho_chu_bot_va_admin(monkeypatch):
    member_prompt = tools._build_router_prompt("tài liệu nói gì", role=reg.ROLE_MEMBER)
    owner_prompt = tools._build_router_prompt("tài liệu nói gì", role=reg.ROLE_OWNER)
    assert '"rag_search"' not in member_prompt and '"rag_search"' in owner_prompt

    _router(monkeypatch, {"tool": "rag_search", "args": {"query": "DCA"}})
    outcome = await tools.maybe_run_tool(-5, "trong tài liệu của anh nói gì về DCA")
    assert outcome.status == reg.STATUS_NONE  # thành viên Zalo: router không được chọn tool này


@pytest.mark.asyncio
async def test_lich_su_hoi_thoai_vao_prompt_router(monkeypatch, reminders, frozen):
    async def history(user_id, turns=3):
        assert user_id == -9  # đúng người gọi (Zalo tách Telegram)
        return "user: mai anh phải nộp hồ sơ thuế\nmodel: dạ"

    monkeypatch.setattr(web_search, "recent_history_text", history)
    router = _router(monkeypatch, {"tool": "set_reminder", "args": {"message": "nộp hồ sơ thuế"}})

    await tools.maybe_run_tool(-9, "nhắc anh việc đó lúc 8h tối")

    assert "nộp hồ sơ thuế" in router.prompts[0]
    assert "CHỈ để hiểu tham chiếu" in router.prompts[0]


@pytest.mark.asyncio
async def test_read_link_chi_doc_link_trong_tin_nhan(monkeypatch):
    from core import untrusted
    from services import web_reader

    async def read(url):
        return "Nội dung trang."

    monkeypatch.setattr(web_reader, "read_url", read)
    token = tools._turn_text.set("tóm tắt giúp anh https://vnexpress.net/a nhé")
    try:
        ok = await tools._tool_read_link(1, url="https://vnexpress.net/a")
        blocked = await tools._tool_read_link(1, url="https://evil.example/?q=bi-mat")
    finally:
        tools._turn_text.reset(token)
    assert untrusted.contains_untrusted(ok.text)
    assert blocked.status == reg.STATUS_INVALID


@pytest.mark.asyncio
async def test_get_weather_khong_noi_dia_diem_dung_mac_dinh(monkeypatch):
    from services import weather

    calls = []

    async def fake_get_weather(location="", days=1):
        calls.append((location, days))
        return {"location": weather.DEFAULT_LABEL, "source": "Open-Meteo", "now": {}, "daily": []}

    async def home(user_id):
        return "Huế" if user_id == -3 else ""  # chỉ user -3 đã đặt thành phố riêng

    from services import weather_home

    monkeypatch.setattr(weather, "get_weather", fake_get_weather)
    _router(monkeypatch, {"tool": "get_weather", "args": {"location": "", "days": 2}})
    monkeypatch.setattr(weather_home, "get", home)

    outcome = await tools.maybe_run_tool(1, "mai trời có mưa không em")

    assert calls == [("", 2)]
    assert "TP. Hồ Chí Minh" in outcome.text
    await tools.maybe_run_tool(-3, "mai trời có mưa không em")
    assert calls[-1] == ("Huế", 2)


@pytest.mark.asyncio
async def test_get_weather_loi_tra_thong_bao_khong_raise(monkeypatch):
    from services import weather

    async def fake_get_weather(location="", days=1):
        raise weather.WeatherError("mô phỏng lỗi")

    monkeypatch.setattr(weather, "get_weather", fake_get_weather)

    result = await tools._tool_get_weather(1, location="Huế", days=1)

    assert "KHÔNG tự bịa" in result


@pytest.mark.asyncio
async def test_lunar_calendar_duong_sang_am():
    result = await tools._tool_lunar_calendar(1, date="2026-10-21")
    assert "ngày 12 tháng 9 năm Bính Ngọ" in result
    assert "Hàn lộ" in result


@pytest.mark.asyncio
async def test_lunar_calendar_am_sang_duong_co_nam():
    result = await tools._tool_lunar_calendar(1, lunar_day=15, lunar_month=8, lunar_year=2026)
    assert result.startswith("Ngày 15 tháng 8 năm Bính Ngọ âm lịch")
    assert "Thứ Sáu 25/09/2026" in result


@pytest.mark.asyncio
async def test_lunar_calendar_khong_noi_nam_lay_lan_sap_toi():
    from services import lunar

    result = await tools._tool_lunar_calendar(1, lunar_day=1, lunar_month=1)
    today = dt.datetime.now(tools._VN_TZ).date()
    tet = lunar.next_tet(today)
    assert f"{tet:%d/%m/%Y}" in result


@pytest.mark.asyncio
async def test_lunar_calendar_ngay_30_thang_thieu_va_nhuan_khong_ton_tai():
    # Tháng 2 âm năm 2026 chỉ có 29 ngày (mùng 1 tháng 2 là 19/03, mùng 1 tháng 3 là 17/04).
    result = await tools._tool_lunar_calendar(1, lunar_day=30, lunar_month=2, lunar_year=2026)
    assert "chỉ có 29 ngày" in result
    result = await tools._tool_lunar_calendar(
        1, lunar_day=1, lunar_month=3, lunar_year=2025, leap=True
    )
    assert "không có tháng 3 nhuận" in result
    assert "không hợp lệ" in await tools._tool_lunar_calendar(1, date="31/02/2026")


def test_cau_hoi_chi_tieu_va_ngay_nho_qua_router():
    assert tools._looks_like_tool_intent("tháng này anh tiêu bao nhiêu rồi em")
    assert tools._looks_like_tool_intent("nhà mình sắp có giỗ ai không em")


def test_cau_hoi_thoi_tiet_luon_qua_router():
    # web_search bỏ qua câu hỏi thời tiết -> router phải chạy dù không trúng từ khoá.
    assert tools._looks_like_tool_intent("trời nóng quá, mai Đà Lạt trời có lạnh không")


def test_persona_cam_xac_nhan_viec_chua_luu():
    """chat_skill.yaml: chỉ xác nhận đã lưu khi có khối [Kết quả tool ...] của lượt này."""
    from core import config

    for variant in config.PERSONA_VARIANTS:
        persona = config.load_chat_skill(force_reload=True, variant=variant)
        assert "[Kết quả tool ...]" in persona and "[HỆ THỐNG] CHƯA lưu" in persona


@pytest.mark.asyncio
async def test_exclude_scope_an_read_link_khoi_router_va_link_khong_keo_router(monkeypatch):
    """run_chat_turn tự đọc link (services/chat_context.py) và ẩn read_link: router
    không thấy/không chạy được tool này, slug URL không tự kéo 1 lượt router."""
    router = _router(monkeypatch, {"tool": "read_link", "args": {"url": "https://x.vn/nhac-a"}})

    with tools.exclude_scope({"read_link"}):
        skipped = await tools.maybe_run_tool(1, "xem https://x.vn/nhac-a")
        assert skipped.status == reg.STATUS_NONE and router.prompts == []
        chosen = await tools.maybe_run_tool(1, "thời tiết trong link này https://x.vn/nhac-a")
    assert chosen.status == reg.STATUS_NONE and chosen.router_ran  # chọn tool bị ẩn = none
    assert router.prompts and '"read_link"' not in router.prompts[-1]

    # Ngoài phạm vi: link vẫn là lý do gọi router và read_link có trong danh sách.
    router.prompts.clear()
    await tools.maybe_run_tool(1, "xem https://x.vn/a")
    assert router.prompts and '"read_link"' in router.prompts[-1]


def test_router_prompt_co_remember_fact_va_save_note_khong_nhan_nho_giup():
    prompt = tools._build_router_prompt("nhớ giúp anh là sinh nhật mẹ 2/9")
    assert '"remember_fact"' in prompt
    save_note_line = next(line for line in prompt.splitlines() if '"save_note"' in line)
    assert "nhớ giúp" not in save_note_line


# ─── Golden set tier A: bộ lọc từ khoá trước router (không gọi AI) ───────────
# Câu cần tool PHẢI đi tiếp tới router; câu chuyện phiếm KHÔNG được tốn 1 lượt router.
_ROUTER_PHRASINGS = [
    "nhắc anh 8h tối gọi mẹ",
    "nhớ giùm anh mai đóng tiền điện",
    "ghi chú: mật khẩu wifi nhà ngoại là 123",
    "ghi lại giúp anh số tài khoản này",
    "đặt lịch họp 3h chiều thứ 6",
    "hẹn giờ 30 phút nữa nhắc anh tắt bếp",
    "remind me to call Hoa tomorrow",
    "mai Hà Nội có mưa không em",
    "thời tiết Đà Nẵng cuối tuần thế nào",
    "nhiệt độ Sài Gòn bây giờ bao nhiêu",
    "dự báo bão tuần này",
    "hôm nay ngày âm bao nhiêu",
    "rằm tháng 7 năm nay vào thứ mấy",
    "mùng 1 tháng sau là ngày nào dương lịch",
    "tết năm nay còn bao nhiêu ngày",
    "ngày mai có phải ngày hoàng đạo không",
    "giỗ ông nội năm nay rơi vào hôm nào",
    "sinh nhật vợ anh là ngày bao nhiêu nhỉ",
    "tháng này anh tiêu bao nhiêu rồi",
    "tuần trước xài hết bao nhiêu tiền ăn uống",
    "danh mục của anh đang lãi hay lỗ",
    "lần trước em nói gì về quỹ ETF",
    "hôm trước anh hỏi em về bảo hiểm gì ấy nhỉ",
    "trong tài liệu hợp đồng có điều khoản phạt không",
    "đọc giúp anh https://vnexpress.net/bai-viet-123.html",
]
_SMALL_TALK = [
    "chào em",
    "em ăn cơm chưa",
    "kể chuyện cười đi",
    "giải thích lạm phát là gì",
    "dịch câu này sang tiếng Anh: tôi yêu Việt Nam",
    "viết giúp anh bài thơ về mùa thu",
    "em nghĩ sao về trí tuệ nhân tạo",
    "cách nấu phở bò",
]


@pytest.mark.parametrize("text", _ROUTER_PHRASINGS)
def test_cau_can_tool_luon_toi_router(text):
    assert tools._looks_like_tool_intent(text)


@pytest.mark.parametrize("text", _SMALL_TALK)
def test_chuyen_phiem_khong_ton_luot_router(text):
    assert not tools._looks_like_tool_intent(text)
    assert tools.side_effect_intent(text) == ""


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("nhắc anh 8h tối gọi mẹ", "reminder"),
        ("đặt lịch họp 3h chiều thứ 6", "reminder"),
        ("remind me to call Hoa tomorrow", "reminder"),
        ("ghi chú: mật khẩu wifi nhà ngoại là 123", "note"),
        ("ghi lại giúp anh số tài khoản này", "note"),
    ],
)
def test_y_dinh_ghi_du_lieu(text, intent):
    assert tools.side_effect_intent(text) == intent
