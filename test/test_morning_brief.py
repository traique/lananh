"""Test services/morning_brief.py: mục cắm thêm được, nội dung theo người, lịch gửi."""

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from _vn_data_samples import FUEL_HTML, FX_XML, SJC_JSON, VANG_TODAY_JSON
from core import config
from core import database as db
from services import morning_brief as mb
from services import fuel_prices, morning_news, weather, weather_home
from services import vn_market_data as market

VN = ZoneInfo("Asia/Ho_Chi_Minh")
MONDAY = dt.datetime(2026, 10, 26, 7, 5, tzinfo=VN)  # ngày giao dịch
SUNDAY = dt.datetime(2026, 10, 25, 7, 5, tzinfo=VN)
OWNER = 42


def _hours(day: dt.date, rainy: dict[int, int]) -> list:
    base = dt.datetime.combine(day, dt.time())
    return [
        weather.HourPoint(base + dt.timedelta(hours=i), rainy.get(i, 10), 0.0, 30.0, 2)
        for i in range(48)
    ]


@pytest.fixture
def world(monkeypatch):
    """Nguồn dữ liệu giả cho mọi mục + kho settings trong RAM."""
    state = {
        "settings": {},
        "home": {},
        "aqi": 60,
        "reminders": [],
        "holdings": {OWNER: ["FPT"], -5: ["VCB"]},
        "notified": [],
        "outbox": [],
        "zoom": [],
        "headlines": 0,
        "digest": 0,
    }

    async def get_setting(key):
        return state["settings"].get(key)

    async def set_setting(key, value):
        state["settings"][key] = value

    async def list_prefix(prefix, limit=100):
        return sorted((k, v) for k, v in state["settings"].items() if k.startswith(prefix))[:limit]

    async def reminders(user_id, start, end, limit=10):
        return [
            (due, msg)
            for uid, due, msg in state["reminders"]
            if uid == user_id and start <= due < end
        ]

    async def home(user_id):
        return state["home"].get(user_id, "")

    async def fake_weather(location="", days=1):
        return {
            "location": location or weather.DEFAULT_LABEL,
            "source": "Open-Meteo",
            "now": {},
            "daily": [
                {"date": "2026-10-26", "weekday": "Thứ Hai", "description": "mưa rào",
                 "min_c": 25, "max_c": 32, "rain_chance_percent": 70, "uv_max": 9}
            ],
        }  # fmt: skip

    async def fake_hourly(location=""):
        return {
            "location": location,
            "source": "Open-Meteo",
            "hours": _hours(MONDAY.date(), {15: 80, 16: 70}),
        }

    async def fake_air(location=""):
        return {"location": location, "us_aqi": state["aqi"], "pm2_5": 40.0}

    async def gold():
        return market.parse_sjc_json(SJC_JSON)

    async def gold_live():
        if state.get("gold_live_down"):
            raise market.MarketDataError("vang.today lỗi")
        return market.parse_vang_today_json(VANG_TODAY_JSON)

    async def fuel():
        return fuel_prices.parse_fuel_html(FUEL_HTML)

    async def fx():
        return market.parse_fx_xml(FX_XML)

    async def holdings(user_id):
        return state["holdings"].get(user_id, [])

    async def report(user_id, *, digest=False):
        assert digest is True  # thành viên chỉ nhận bản digest, không action policy
        return f"📊 *Digest danh mục* của {user_id}"

    async def headlines(now=None):
        state["headlines"] += 1
        item = morning_news._NewsItem("F1I1", "Tin A", "https://vnexpress.net/a.html", "VnExpress")
        return [item], False

    async def shared(now=None):
        state["digest"] += 1
        return "☀️ BẢN TIN SÁNG — 26/10/2026\n\nTỔNG HỢP AI"

    async def notify(uid, text):
        state["notified"].append((uid, text))

    async def enqueue(account_id, recipient_id, content):
        if state.get("outbox_down"):
            raise RuntimeError("outbox down")
        state["outbox"].append((account_id, recipient_id, content))

    async def account():
        return "acc-1"

    async def zoom_pairing():
        return ("jid-owner", "Chủ bot")

    async def zoom_send(to_jid, text, **kw):
        state["zoom"].append((to_jid, text))

    async def users():
        return [
            SimpleNamespace(
                internal_user_id=-5,
                external_id="z-5",
                is_active=True,
                address_form=state.get("form_-5"),
            ),
            SimpleNamespace(
                internal_user_id=-6, external_id="z-6", is_active=False, address_form=None
            ),
        ]

    async def controller():
        return state.get("controller", "")

    async def resolve(external_id):
        return {"z-5": SimpleNamespace(internal_user_id=-5, is_active=True)}.get(external_id)

    from channels import zalo_repository, zalo_session, zalo_users, zoom
    from stock import calendar as trading_calendar
    from stock import portfolio

    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    monkeypatch.setattr(db, "list_settings_with_prefix", list_prefix)
    monkeypatch.setattr(db, "list_reminders_between", reminders)
    monkeypatch.setattr(db, "zoom_get_pairing", zoom_pairing)
    monkeypatch.setattr(weather_home, "get", home)
    monkeypatch.setattr(weather, "get_weather", fake_weather)
    monkeypatch.setattr(weather, "get_hourly", fake_hourly)
    monkeypatch.setattr(weather, "get_air_quality", fake_air)
    monkeypatch.setattr(market, "get_gold", gold)
    monkeypatch.setattr(market, "get_gold_live", gold_live)
    monkeypatch.setattr(fuel_prices, "get_fuel", fuel)
    monkeypatch.setattr(market, "get_fx", fx)
    monkeypatch.setattr(trading_calendar, "is_trading_day", lambda d: d.weekday() < 5)
    monkeypatch.setattr(portfolio, "list_holdings", holdings)
    monkeypatch.setattr(portfolio, "build_report", report)
    monkeypatch.setattr(morning_news, "today_headlines", headlines)
    monkeypatch.setattr(morning_news, "shared_digest", shared)
    monkeypatch.setattr(zalo_repository, "enqueue_outbox", enqueue)
    monkeypatch.setattr(zalo_repository, "resolve_active_account_id", account)
    monkeypatch.setattr(zoom, "send_message", zoom_send)
    monkeypatch.setattr(zalo_users, "list_users", users)
    monkeypatch.setattr(zalo_users, "resolve", resolve)
    monkeypatch.setattr(zalo_session, "load_controller", controller)
    monkeypatch.setattr(config, "ALLOWED_USER_ID", OWNER)
    monkeypatch.setattr(config, "ZOOM_ENABLED", True)
    monkeypatch.setattr(config, "MORNING_BRIEF_ENABLED", True)
    monkeypatch.setattr(config, "MORNING_BRIEF_ABSORB_NEWS", True)
    monkeypatch.setattr(config, "MORNING_BRIEF_AI_INTRO", False)
    monkeypatch.setattr(config, "MORNING_BRIEF_MAX_MEMBERS", 2)
    monkeypatch.setenv("ZALO_ENABLED", "true")
    monkeypatch.setenv("ZALO_BRIDGE_SECRET", "s")
    monkeypatch.delenv("ZALO_CONTROLLER_ID", raising=False)
    mb.set_notify_callback(notify)
    yield state
    mb.set_notify_callback(None)  # type: ignore[arg-type]


def test_parse_settings_mac_dinh_va_gia_tri_hong():
    s = mb.parse_settings(None)
    assert (s.on, s.hour, s.news) == (False, config.MORNING_BRIEF_DEFAULT_HOUR, None)
    assert (
        mb.parse_settings('{"on": true, "hour": 3, "news": "x"}').hour
        == config.MORNING_BRIEF_DEFAULT_HOUR
    )
    assert mb.parse_settings("không phải json").on is False
    assert not s.news_enabled("telegram")  # chủ bot đã chọn không nhận tin RSS qua Telegram
    assert s.news_enabled("zalo") and s.news_enabled("zoom")
    assert mb.parse_settings(mb.dump_settings(mb.BriefSettings(True, 6, True))) == mb.BriefSettings(
        True, 6, True
    )
    assert s.fuel is False  # giá xăng mặc định tắt
    assert mb.parse_settings(mb.dump_settings(mb.BriefSettings(True, 6, None, True))).fuel is True
    assert mb.parse_settings('{"on": true, "fuel": "yes"}').fuel is False


async def test_ban_tin_chu_bot_telegram(world):
    world["home"][OWNER] = "Hà Nội"
    world["reminders"].append((OWNER, MONDAY + dt.timedelta(hours=2), "họp team"))
    world["reminders"].append((OWNER, MONDAY + dt.timedelta(days=1), "mai mới nhắc"))
    text = await mb.build(OWNER, "telegram", now=MONDAY, settings=mb.BriefSettings(on=True))
    assert text.startswith("☀️ Chào buổi sáng anh! Bản tin Thứ Hai 26/10/2026")
    assert "🌤 Thời tiết Hà Nội: mưa rào, 25-32°C, khả năng mưa 70%" in text
    assert "Dễ mưa (từ 50%) khoảng 15h-17h" in text
    assert "😷" not in text  # AQI 60: không cảnh báo
    assert "📅 Âm lịch: ngày 17 tháng 9 năm Bính Ngọ" in text
    assert "Mùng 1 tháng 10" in text and "Ngày Nhà giáo Việt Nam" in text
    assert "⏰ Nhắc việc hôm nay:\n• 09:05 họp team" in text and "mai mới nhắc" not in text
    assert "🪙 Vàng SJC miếng 9999 mua 138,7 · bán 141,7 (▲0,5) triệu/lượng" in text
    assert "💱 USD Vietcombank" in text and "⛽" not in text  # xăng: mặc định tắt
    assert "Digest danh mục* của 42" in text
    assert "TIN ĐÁNG CHÚ Ý" not in text and world["headlines"] == 0  # Telegram: tắt tin
    # Thứ tự mục cố định theo order.
    assert (
        text.index("🌤") < text.index("📅") < text.index("⏰") < text.index("🪙") < text.index("📊")
    )


async def test_ban_tin_canh_bao_aqi_va_nghi_giao_dich(world):
    world["aqi"] = 160
    text = await mb.build(-5, "zalo", now=SUNDAY, settings=mb.BriefSettings(on=True))
    assert "😷 Không khí US AQI 160 (xấu)" in text
    assert "Mặc định TP.HCM" in text  # chưa đặt thành phố riêng
    assert "Digest danh mục" not in text  # Chủ Nhật không có phiên
    assert "📰 TIN ĐÁNG CHÚ Ý" in text and "https://vnexpress.net/a.html" in text
    assert world["digest"] == 0  # thành viên: tin tất định, không gọi AI


async def test_muc_cam_them_va_muc_loi_bi_bo(world, monkeypatch):
    monkeypatch.setattr(mb, "_SECTIONS", dict(mb._SECTIONS))

    @mb.register_section("ngay_nho", order=35)
    async def ngay_nho(ctx):
        return f"💝 Ngày nhớ của {ctx.user_id}"

    @mb.register_section("hong", order=36)
    async def hong(ctx):
        raise RuntimeError("mục lỗi")

    @mb.register_section("rong", order=37)
    async def rong(ctx):
        return None

    text = await mb.build(-5, "zalo", now=MONDAY, settings=mb.BriefSettings(on=True))
    assert "💝 Ngày nhớ của -5" in text
    assert text.index("📅") < text.index("💝") < text.index("🪙")
    assert [s.key for s in mb.sections()][:3] == ["weather", "lunar", "reminders"]


async def test_muc_qua_han_bi_bo(world, monkeypatch):
    monkeypatch.setattr(mb, "_SECTIONS", {})

    @mb.register_section("cham", order=1, timeout=0.01)
    async def cham(ctx):
        import asyncio

        await asyncio.sleep(1)
        return "không kịp"

    text = await mb.build(-5, "zalo", now=MONDAY, settings=mb.BriefSettings(on=True))
    assert "không kịp" not in text


def test_su_kien_sap_toi_gop_ram_va_le():
    assert mb.upcoming_events(dt.date(2026, 9, 20)) == [
        "Rằm tháng 8 - Tết Trung thu (Thứ Sáu 25/09, còn 5 ngày)"
    ]
    events = mb.upcoming_events(dt.date(2026, 11, 10))
    assert events[0].startswith("Ngày Nhà giáo Việt Nam (Thứ Sáu 20/11")  # sắp theo ngày
    assert events[1].startswith("Rằm tháng 10")


async def test_gop_tin_controller_zalo_va_zoom(world):
    world["controller"] = "z-5"
    zalo = await mb.build(-5, "zalo", now=MONDAY, settings=mb.BriefSettings(on=True))
    assert "TỔNG HỢP AI" in zalo and world["digest"] == 1 and world["headlines"] == 0
    preview = await mb.build(
        -5, "zalo", now=MONDAY, settings=mb.BriefSettings(on=True), preview=True
    )
    assert "TỔNG HỢP AI" not in preview and world["headlines"] == 1  # xem thử: không gọi AI
    zoom = await mb.build(OWNER, "zoom", now=MONDAY, settings=mb.BriefSettings(on=True))
    assert "TỔNG HỢP AI" in zoom


async def test_absorbed_news_channels(world):
    assert await mb.absorbed_news_channels() == set()
    world["controller"] = "z-5"
    await mb.save_settings("zalo", -5, mb.BriefSettings(on=True))
    await mb.save_settings("zoom", OWNER, mb.BriefSettings(on=True))
    assert await mb.absorbed_news_channels() == {"zalo", "zoom"}
    await mb.save_settings("zalo", -5, mb.BriefSettings(on=True, news=False))
    assert await mb.absorbed_news_channels() == {"zoom"}


async def test_absorbed_news_zalo_chi_khi_controller_thuc_su_nhan_ban_tin(world, monkeypatch):
    world["controller"] = "z-5"
    await mb.save_settings("zalo", -5, mb.BriefSettings(on=True))
    # Chủ bot tắt bản tin Zalo (0 = tắt): controller không nhận bản tin nên bản tin
    # RSS 8h phải đi đường cũ, không bị "gộp" vào bản tin không bao giờ gửi.
    monkeypatch.setattr(config, "MORNING_BRIEF_MAX_MEMBERS", 0)
    assert "zalo" not in await mb.absorbed_news_channels()
    # Trần cắt mất controller (dòng khác đứng trước theo thứ tự khoá).
    monkeypatch.setattr(config, "MORNING_BRIEF_MAX_MEMBERS", 1)
    await mb.save_settings("zalo", -4, mb.BriefSettings(on=True))
    assert [t.user_id for t in await mb.list_targets() if t.channel == "zalo"] == [-4]
    assert "zalo" not in await mb.absorbed_news_channels()
    monkeypatch.setattr(config, "MORNING_BRIEF_MAX_MEMBERS", 2)
    assert "zalo" in await mb.absorbed_news_channels()


async def test_run_due_gui_dung_kenh_mot_lan_moi_ngay(world):
    await mb.save_settings("telegram", OWNER, mb.BriefSettings(on=True, hour=7))
    await mb.save_settings("zalo", -5, mb.BriefSettings(on=True, hour=7))
    await mb.save_settings("zalo", -6, mb.BriefSettings(on=True, hour=7))  # đang bị khoá
    await mb.save_settings("zalo", -7, mb.BriefSettings(on=True, hour=9))  # chưa tới giờ

    assert await mb.run_due(MONDAY) == 2
    assert [uid for uid, _ in world["notified"]] == [OWNER]
    assert len(world["outbox"]) == 1
    account, recipient, content = world["outbox"][0]
    assert (account, recipient) == ("acc-1", "z-5")
    assert "Digest danh mục của -5" in content  # plain text cho Zalo (bỏ dấu *)
    assert world["settings"]["brief_sent:zalo:-5"] == "2026-10-26"
    assert "brief_sent:zalo:-6" not in world["settings"]

    assert await mb.run_due(MONDAY + dt.timedelta(minutes=15)) == 0  # 1 tin/ngày
    assert await mb.run_due(MONDAY + dt.timedelta(hours=4)) == 0  # quá khung gửi bù


async def test_zalo_ban_tin_goi_dung_cach_xung_ho_rieng(world):
    """Cách gọi riêng (/zalopair <id> <tên> cô, migrations/024) áp cả cho bản tin
    sáng - chạy nền ngoài lượt chat nên không có reply_audience."""
    world["form_-5"] = "cô"
    await mb.save_settings("zalo", -5, mb.BriefSettings(on=True, hour=7))
    assert await mb.run_due(MONDAY) == 1
    content = world["outbox"][0][2]
    assert content.startswith("☀️ Chào buổi sáng cô! Bản tin Thứ Hai 26/10/2026")
    assert "để cháu xem đúng nơi cô ở" in content
    assert " anh" not in content


async def test_ban_tin_xem_thu_tren_zalo_theo_nguoi_nhan(world):
    from services import daily_commands, reply_audience

    await mb.save_settings("zalo", -5, mb.BriefSettings(on=True, hour=7))
    with reply_audience.use(reply_audience.ReplyAudience(address_form="chị")):
        text = await daily_commands.bantin(-5, "xem", "zalo")
    assert text.startswith("☀️ Chào buổi sáng chị!")
    assert (await daily_commands.bantin(OWNER, "xem", "telegram")).startswith(
        "☀️ Chào buổi sáng anh!"
    )


async def test_nhieu_dong_zalo_da_tat_khong_che_chu_bot(world):
    for i in range(1, mb._LIST_LIMIT + 50):
        world["settings"][f"brief:zalo:-{i}"] = mb.dump_settings(mb.BriefSettings(on=False))
    await mb.save_settings("zoom", OWNER, mb.BriefSettings(on=True))
    await mb.save_settings("telegram", OWNER, mb.BriefSettings(on=True))
    assert {t.channel for t in await mb.list_targets()} == {"telegram", "zoom"}


async def test_zalo_danh_dau_truoc_khi_vao_outbox(world):
    await mb.save_settings("zalo", -5, mb.BriefSettings(on=True, hour=7))
    world["outbox_down"] = True
    assert await mb.run_due(MONDAY) == 0
    # Đã đánh dấu: lượt sau KHÔNG gửi lại (chấp nhận mất 1 tin, không gửi đôi).
    assert world["settings"]["brief_sent:zalo:-5"] == "2026-10-26"
    world["outbox_down"] = False
    assert await mb.run_due(MONDAY + dt.timedelta(minutes=15)) == 0


async def test_telegram_loi_gui_thi_thu_lai(world):
    await mb.save_settings("telegram", OWNER, mb.BriefSettings(on=True, hour=7))

    async def broken(uid, text):
        raise RuntimeError("telegram down")

    mb.set_notify_callback(broken)
    assert await mb.run_due(MONDAY) == 0
    assert "brief_sent:telegram:42" not in world["settings"]


async def test_zalo_tat_thi_khong_gui_zalo(world, monkeypatch):
    monkeypatch.setenv("ZALO_ENABLED", "false")
    await mb.save_settings("zalo", -5, mb.BriefSettings(on=True, hour=7))
    await mb.save_settings("zoom", OWNER, mb.BriefSettings(on=True, hour=7))
    assert await mb.run_due(MONDAY) == 1
    assert world["outbox"] == [] and world["zoom"][0][0] == "jid-owner"


async def test_set_enabled_bao_day_theo_cach_xung_ho(world, monkeypatch):
    from services import reply_audience

    monkeypatch.setattr(config, "MORNING_BRIEF_MAX_MEMBERS", 1)
    assert await mb.set_enabled("zalo", -5, True) is None
    with reply_audience.use(reply_audience.ReplyAudience(address_form="cô")):
        error = await mb.set_enabled("zalo", -7, True)
    assert "cô nhờ chủ bot nới giới hạn giúp cháu nha" in error


async def test_set_enabled_tran_thanh_vien_va_chi_chu_bot(world):
    assert await mb.set_enabled("zalo", -5, True) is None
    assert await mb.set_enabled("zalo", -6, True) is None
    error = await mb.set_enabled("zalo", -7, True)
    assert "đủ 2 người" in error
    assert await mb.set_enabled("zalo", -5, True, hour=6) is None  # người cũ đổi giờ vẫn được
    assert (await mb.get_settings("zalo", -5)).hour == 6
    assert "chỉ dành cho chủ bot" in await mb.set_enabled("telegram", 99, True)
    assert await mb.set_enabled("telegram", OWNER, True) is None


async def test_scheduler_bo_digest_khi_chu_bot_bat_ban_tin(world, monkeypatch):
    import scheduler

    built = []

    async def fake_build(user_id):
        built.append(user_id)
        return "digest"

    monkeypatch.setattr(scheduler, "_build_portfolio_digest", fake_build)
    monkeypatch.setattr(scheduler, "digest_due", lambda now, last: last is None)
    await mb.save_settings("telegram", OWNER, mb.BriefSettings(on=True))
    assert await scheduler.run_daily_digest_once(OWNER) is False
    assert built == [] and scheduler._DIGEST_LAST_SENT_KEY in world["settings"]


async def test_morning_news_bo_kenh_da_gop(world, monkeypatch):
    sent = {"zalo": 0, "zoom": 0}

    async def to_zalo(content):
        sent["zalo"] += 1
        return True

    async def to_zoom(content):
        sent["zoom"] += 1
        return True

    async def absorbed():
        return {"zalo"}

    monkeypatch.setattr(morning_news, "_send_to_zalo", to_zalo)
    monkeypatch.setattr(morning_news, "_send_to_zoom", to_zoom)
    monkeypatch.setattr(morning_news, "_absorbed_channels", absorbed)
    result = await morning_news.run_once()
    assert sent == {"zalo": 0, "zoom": 1} and result.sent_zoom and not result.sent_zalo
    assert world["settings"][morning_news._LAST_SENT_KEY]

    world["settings"].clear()

    async def both():
        return {"zalo", "zoom"}

    monkeypatch.setattr(morning_news, "_absorbed_channels", both)
    digest_calls = world["digest"]
    result = await morning_news.run_once()
    assert result.content is None and world["digest"] == digest_calls  # không gọi AI


async def test_ai_intro_mot_luot_chi_chu_bot(world, monkeypatch):
    from ai import orchestrator

    asked = []

    async def ask(prompt, **kw):
        asked.append(prompt)
        return SimpleNamespace(text="Chúc anh ngày mới thật vui nha!\nthừa")

    monkeypatch.setattr(orchestrator, "ask", ask)
    monkeypatch.setattr(config, "MORNING_BRIEF_AI_INTRO", True)
    text = await mb.build(OWNER, "telegram", now=MONDAY, settings=mb.BriefSettings(on=True))
    assert "\nChúc anh ngày mới thật vui nha!\n" in text and "thừa" not in text
    await mb.build(-5, "zalo", now=MONDAY, settings=mb.BriefSettings(on=True))
    assert len(asked) == 1


async def test_ai_intro_rao_noi_dung_ban_tin(world, monkeypatch):
    from ai import orchestrator
    from core import untrusted

    asked = []

    async def ask(prompt, **kw):
        asked.append(prompt)
        return SimpleNamespace(text="Chào anh!")

    monkeypatch.setattr(orchestrator, "ask", ask)
    monkeypatch.setattr(config, "MORNING_BRIEF_AI_INTRO", True)
    await mb.build(OWNER, "telegram", now=MONDAY, settings=mb.BriefSettings(on=True, news=True))
    # Tiêu đề RSS/nhắc việc là dữ liệu ngoài: phải nằm TRONG khối rào, sau chỉ dẫn.
    prompt = asked[0]
    assert untrusted.contains_untrusted(prompt)
    opened = prompt.index("<<<" + untrusted.OPEN_TAG)
    assert prompt.index("Tin A") > opened and prompt.index("Viết ĐÚNG 1 câu") < opened


async def test_muc_gia_xang_bat_rieng_va_vang_du_phong_sjc(world):
    settings = mb.BriefSettings(on=True, fuel=True)
    world["gold_live_down"] = True
    text = await mb.build(OWNER, "telegram", now=MONDAY, settings=settings)
    assert "⛽ Xăng dầu Petrolimex vùng 1: Xăng E5 RON 92 25.060" in text
    assert "🪙 Vàng SJC mua 148.000" in text  # vang.today lỗi -> bảng SJC
    assert text.index("🪙") < text.index("⛽") < text.index("📊")
