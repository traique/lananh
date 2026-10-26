"""services/attention.py (/chuy): mục cắm thêm được, mục lỗi không làm hỏng cả tin,
đúng người xem đúng dữ liệu, câu tự nhiên không bắt nhầm."""

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from core import config
from core import database as db
from services import attention, error_digest, ngay_nho
from services import notify_hub, reply_audience

VN = attention.VN_TZ
OWNER = 777
NOW = dt.datetime(2026, 10, 26, 9, 0, tzinfo=VN)


@pytest.fixture
def world(monkeypatch):
    """Dữ liệu giả theo user_id cho các mục có sẵn."""
    state = SimpleNamespace(
        reminders={
            OWNER: [(NOW.replace(hour=14), "gọi khách")],
            -5: [(NOW.replace(hour=20), "uống thuốc")],
        },
        log={
            OWNER: [
                ("stop", "HPG:25000", "2026-10-26"),
                ("event", "FPT:DIV:2026-10-26", "2026-10-23"),
            ]
        },
        events={-5: [{"title": "đi khám", "event_date": NOW.date(), "status": "pending"}]},
        fb_pending=2,
        queued={OWNER: 3},
    )
    monkeypatch.setattr(config, "ALLOWED_USER_ID", OWNER)
    monkeypatch.setattr(attention, "_now", lambda: NOW)

    async def reminders(uid, start, end, limit=10):
        return [r for r in state.reminders.get(uid, []) if start <= r[0] < end]

    async def log_since(uid, since):
        return state.log.get(uid, [])

    async def price_alerts(uid):
        return []

    async def events(uid, limit=20):
        return state.events.get(uid, [])

    async def count_pending(uid):
        return state.queued.get(uid, 0)

    async def get_setting(key):
        return None

    async def ngay(uid, today):
        if uid != OWNER:
            return []
        item = ngay_nho.Item(1, "giỗ ông nội", "gio", {}, 3, today + dt.timedelta(days=2))
        return [item]

    async def fb_account():
        return "acc"

    async def fb_count(account_id):
        return state.fb_pending

    monkeypatch.setattr(db, "list_reminders_between", reminders)
    monkeypatch.setattr(db, "portfolio_alert_log_since", log_since)
    monkeypatch.setattr(db, "portfolio_list_price_alerts", price_alerts)
    monkeypatch.setattr(db, "list_user_events", events)
    monkeypatch.setattr(db, "notify_count_pending", count_pending)
    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(ngay_nho, "items", ngay)
    monkeypatch.setattr(attention, "_facebook_account", fb_account)
    monkeypatch.setattr("channels.facebook_repository.count_pending", fb_count)
    monkeypatch.setattr(notify_hub, "_started", True)
    # Mục hệ thống: không gọi quota/watchdog thật.
    monkeypatch.setattr(error_digest, "store", error_digest.DigestStore())
    monkeypatch.setattr("services.watchdog.enabled", lambda: False)

    async def no_burn(now=None):
        return []

    monkeypatch.setattr("services.quota_burn.snapshot", no_burn)
    return state


@pytest.mark.asyncio
async def test_chu_bot_thay_du_cac_muc(world):
    error_digest.store.add(
        "2026-10-26", error_digest.Entry("x", "KeyError", "services/a.py:f", 1, "ERROR", 1, 0, 0)
    )
    text = await attention.build(OWNER, "telegram", now=NOW)
    assert text.startswith("👀 Việc cần anh chú ý (09:00 26/10):")
    assert "14:00 gọi khách" in text and "giỗ ông nội" in text and "còn 2 ngày" in text
    assert (
        "HPG chạm/thủng stop 25.000" in text and "FPT: sự kiện quyền (cổ tức tiền) HÔM NAY" in text
    )
    assert "📬 Có 3 thông báo" in text
    assert "2 bài Facebook đang chờ duyệt" in text
    assert "KeyError" in text
    assert "uống thuốc" not in text and "đi khám" not in text  # dữ liệu người khác


@pytest.mark.asyncio
async def test_thanh_vien_zalo_chi_thay_du_lieu_cua_minh(world):
    error_digest.store.add(
        "2026-10-26", error_digest.Entry("x", "KeyError", "services/a.py:f", 1, "ERROR", 1, 0, 0)
    )
    text = await attention.build(-5, "zalo", is_owner=False, address_form="chị", now=NOW)
    assert "Việc cần chị chú ý" in text
    assert "20:00 uống thuốc" in text and "đi khám (hôm nay)" in text
    assert "gọi khách" not in text and "HPG" not in text
    assert "Facebook" not in text and "KeyError" not in text  # không thấy mục admin/chủ bot
    admin = await attention.build(-5, "zalo", is_owner=False, is_admin=True, now=NOW)
    assert "Facebook" in admin and "KeyError" not in admin


@pytest.mark.asyncio
async def test_khong_co_gi_thi_tra_cau_ngan(world):
    world.reminders, world.log, world.events, world.queued = {}, {}, {}, {}
    text = await attention.build(-9, "zalo", is_owner=False, address_form="cô", now=NOW)
    assert text == "✨ Hiện chưa có gì cần cô chú ý đâu, cháu vẫn canh giúp cô nha."


@pytest.mark.asyncio
async def test_muc_loi_hoac_cham_khong_lam_hong_ca_tin(world, monkeypatch):
    monkeypatch.setattr(attention, "_SECTIONS", dict(attention._SECTIONS))

    @attention.register_section("hong", 1, label="mục hỏng")
    async def broken(ctx):
        raise RuntimeError("x")

    @attention.register_section("cham", 2, timeout=0.01, label="mục chậm")
    async def slow(ctx):
        await asyncio.sleep(1)
        return "không bao giờ"

    @attention.register_section("viec_nen", 80, audience="owner")
    async def jobs(ctx):
        return "⚙️ 1 việc nền đang chạy"

    text = await attention.build(OWNER, "telegram", now=NOW)
    assert "gọi khách" in text and "⚙️ 1 việc nền đang chạy" in text
    assert "chưa đọc được: mục hỏng, mục chậm" in text
    member = await attention.build(-5, "zalo", is_owner=False, now=NOW)
    assert "việc nền" not in member
    with pytest.raises(ValueError):
        attention.register_section("x", 1, audience="moi nguoi")


@pytest.mark.parametrize(
    "text",
    [
        "có gì cần anh chú ý",
        "Có gì cần anh chú ý không em?",
        "hôm nay có gì cần lo",
        "Hôm nay có gì cần lo không?",
        "em ơi có việc gì cần anh xử lý không",
        "co gi can anh chu y",
    ],
)
def test_cau_tu_nhien_ro_rang(text):
    assert attention.is_attention_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "có gì cần chú ý khi mua nhà",
        "có gì cần lưu ý không",
        "hôm nay có gì vui",
        "cần chú ý gì",
        "có gì cần anh chú ý khi đi Đà Lạt tuần sau không, kể chi tiết giúp anh với nhé",
    ],
)
def test_cau_thuong_khong_bi_bat_nham(text):
    assert not attention.is_attention_question(text)


@pytest.mark.asyncio
async def test_lenh_chuy_va_cau_tu_nhien_tren_zalo(world):
    from services import channel_chat_service, channel_command_service

    audience = reply_audience.ReplyAudience(address_form="chị")
    with reply_audience.use(audience):
        outputs, _ = await channel_command_service.maybe_handle_command(-5, "/chuy", False, "zalo")
        assert "Việc cần chị chú ý" in outputs[0] and "uống thuốc" in outputs[0]
        quiet, _ = await channel_command_service.maybe_handle_command(-5, "/yenlang", False, "zalo")
        assert "Giờ yên lặng" in quiet[0]

    recorded = []

    async def record(uid, text, reply, kind, **kw):
        recorded.append((uid, kind))

    from services import conversation_log

    original = conversation_log.record_exchange
    conversation_log.record_exchange = record
    try:
        with reply_audience.use(audience):
            result = await channel_chat_service.handle_channel_text(
                -5, "hôm nay có gì cần chị chú ý không", False, "zalo"
            )
    finally:
        conversation_log.record_exchange = original
    assert "uống thuốc" in result.messages[0]
    assert recorded == [(-5, conversation_log.KIND_DAILY)]


def test_chuy_la_lenh_chi_doc_chay_ngoai_khoa_luot():
    from services import turn_control

    assert turn_control.is_readonly("/chuy")
    assert turn_control.is_readonly("/yenlang")
    assert not turn_control.is_readonly("/yenlang off")
