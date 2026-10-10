"""Unit test cho services/market_page.py."""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import orchestrator  # noqa: E402
from channels import facebook_commands  # noqa: E402
from core import config  # noqa: E402
from core import database as db  # noqa: E402
from services import facebook_page_service, market_page  # noqa: E402

VN = ZoneInfo("Asia/Ho_Chi_Minh")


class FakeSettingsStore:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """Mọi test dùng kho settings trong RAM và coi dữ liệu là phiên hôm nay;
    test nào cần kiểm tra ngày phiên/kho riêng thì tự patch lại."""
    fake = FakeSettingsStore()
    monkeypatch.setattr(db, "get_setting", fake.get)
    monkeypatch.setattr(db, "set_setting", fake.set)
    monkeypatch.setattr(market_page, "_is_current_session", lambda report_date: True)
    return fake


@pytest.fixture
def store(monkeypatch):
    store = FakeSettingsStore()
    monkeypatch.setattr(db, "get_setting", store.get)
    monkeypatch.setattr(db, "set_setting", store.set)
    return store


def _bars(closes, *, volume=1000.0):
    return [
        {
            "ts": 1_700_000_000 + i * 86400,
            "date": f"2026-09-{i + 1:02d}",
            "high": c + 5,
            "low": c - 5,
            "close": c,
            "volume": volume,
        }
        for i, c in enumerate(closes)
    ]


# ─── Dữ liệu DNSE & chỉ báo ─────────────────────────────────────────────────


def test_parse_bars_scales_stocks_but_not_indexes_and_drops_bad_closes():
    body = {
        "t": [1_700_086_400, 1_700_000_000, 1_700_172_800],
        "h": [26.0, 25.0, 27.0],
        "l": [24.0, 23.0, 25.0],
        "c": [25.5, 24.0, 0],
        "v": [200, 100, 300],
    }

    stock = market_page._parse_bars("FPT", body)
    index = market_page._parse_bars("VNINDEX", body)

    assert [b["close"] for b in stock] == [24000.0, 25500.0]  # sắp xếp theo thời gian, bỏ close=0
    assert [b["close"] for b in index] == [24.0, 25.5]
    assert stock[0]["high"] == 25000.0 and stock[0]["volume"] == 100.0


def test_parse_bars_tolerates_missing_high_low_volume():
    bars = market_page._parse_bars("VNINDEX", {"t": [1_700_000_000], "c": [1200.0]})
    assert bars[0]["high"] == bars[0]["low"] == 1200.0
    assert bars[0]["volume"] == 0.0


def test_rsi_all_gains_is_100_and_needs_enough_history():
    rising = [float(x) for x in range(1, 20)]
    assert market_page._rsi(rising) == 100.0
    assert market_page._rsi(rising[:14]) is None


def test_rsi_mixed_matches_hand_calculation():
    # 14 biến động: 7 lần +2, 7 lần -1 -> gain 14, loss 7 -> RS 2 -> RSI 66.67
    values = [100.0]
    for step in [2, -1] * 7:
        values.append(values[-1] + step)
    assert market_page._rsi(values) == pytest.approx(100 - 100 / 3)


def test_ema_of_constant_series_is_that_constant():
    assert market_page._ema([50.0] * 30, 12) == pytest.approx(50.0)
    assert market_page._ema([50.0] * 5, 12) is None


def test_indicators_flat_series():
    out = market_page._indicators("VNINDEX", _bars([1000.0] * 30))

    assert out["change_pct"] == 0.0
    assert out["ma20"] == 1000.0
    assert out["bb_upper"] == out["bb_lower"] == 1000.0
    assert out["above_ma20"] is False
    assert out["ma50"] is None  # chưa đủ 50 phiên
    assert out["close_position"] == 0.5
    assert out["vol_ratio"] == 1.0


def test_build_report_requires_vnindex():
    assert market_page._build_report({"VNINDEX": [], "FPT": _bars([100.0] * 25)}) is None


def test_build_report_breadth_and_movers():
    rows = {
        "VNINDEX": _bars([1200.0, 1210.0]),
        "VN30": _bars([1300.0, 1290.0]),  # chỉ số: không tính vào độ rộng
        "AAA": _bars([10.0, 11.0]),
        "BBB": _bars([10.0, 9.0]),
        "CCC": _bars([10.0, 10.0]),
        "DDD": _bars([10.0]),  # chưa có phiên trước: không có % thay đổi
    }

    report = market_page._build_report(rows)

    assert report["report_date"] == "2026-09-02"
    assert (report["advancers"], report["decliners"], report["unchanged"]) == (1, 1, 1)
    assert [m["symbol"] for m in report["gainers"]][0] == "AAA"
    assert [m["symbol"] for m in report["losers"]][0] == "BBB"
    assert "DDD" not in {m["symbol"] for m in report["gainers"] + report["losers"]}
    # mã giảm không được lọt vào danh sách tăng và ngược lại, dù chưa đủ 5 mã mỗi chiều
    assert [m["symbol"] for m in report["gainers"]] == ["AAA"]
    assert [m["symbol"] for m in report["losers"]] == ["BBB"]


def test_stock_prompt_contains_the_computed_numbers():
    report = market_page._build_report(
        {"VNINDEX": _bars([1200.0, 1212.0]), "AAA": _bars([10.0, 11.0])}
    )

    prompt = market_page._stock_prompt(report)

    assert "VN-Index đóng cửa 1212.0 điểm (+1.0%)" in prompt
    assert "Khối lượng khớp 0,0 triệu" in prompt or "triệu cổ phiếu" in prompt
    assert "AAA (+10.0%" in prompt


def test_chart_config_pads_the_y_axis_around_the_data():
    config = market_page._chart_config(_bars([1000.0, 1100.0]), "2026-09-02")

    y = config["options"]["scales"]["y"]
    assert y["min"] == 995 and y["max"] == 1106
    assert config["options"]["plugins"]["title"]["text"].endswith("02/09/2026")
    assert config["data"]["labels"] == ["01/09", "02/09"]  # ngày/tháng kiểu Việt Nam


# ─── Tin CafeF ──────────────────────────────────────────────────────────────


def _entry(title, summary="", *, published=None, link="https://cafef.vn/a.chn"):
    return market_page._Entry(title, summary, link, published)


def test_pick_entry_prefers_most_keywords_among_todays_articles():
    today = date(2026, 10, 9)
    morning = datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc)  # 08:00 VN
    yesterday = morning - timedelta(days=1)
    entries = [
        _entry("Tin nhẹ", published=morning),
        _entry("VN-Index bùng nổ, khối ngoại mua ròng", published=morning),
        _entry("VN-Index thanh khoản kỷ lục, khối ngoại", published=yesterday),
    ]

    assert market_page._pick_entry(entries, today).title == entries[1].title


def test_pick_entry_falls_back_to_first_in_feed_when_nothing_is_from_today():
    old = datetime(2026, 10, 1, tzinfo=timezone.utc)
    entries = [_entry("Bài A", published=old), _entry("VN-Index tăng mạnh", published=old)]

    assert market_page._pick_entry(entries, date(2026, 10, 9)).title == "Bài A"
    assert market_page._pick_entry([], date(2026, 10, 9)) is None


def test_pick_entry_uses_vietnam_calendar_day():
    # 18:30 UTC ngày 8 = 01:30 ngày 9 giờ VN
    late = datetime(2026, 10, 8, 18, 30, tzinfo=timezone.utc)

    assert market_page._pick_entry([_entry("Bài", published=late)], date(2026, 10, 9)) is not None


def test_parse_entries_strips_html_from_summary():
    feed = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title><item>'
        b"<title>  VN-Index   tang </title><link>https://cafef.vn/a.chn</link>"
        b"<description>&lt;b&gt;Noi dung&lt;/b&gt; ngan</description>"
        b"<pubDate>Fri, 09 Oct 2026 01:00:00 +0000</pubDate></item></channel></rss>"
    )

    (entry,) = market_page._parse_entries(feed)

    assert entry.title == "VN-Index tang"
    assert entry.summary == "Noi dung ngan"
    assert entry.published == datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc)


def test_clean_news_text_strips_lead_in_stars_and_soft_line_breaks():
    raw = "Dưới đây là bài viết:\n\n**TIÊU ĐỀ**\nDòng một\nvẫn cùng đoạn.\n\nĐoạn hai\n- ý nhỏ"

    assert market_page._clean_news_text(raw) == "TIÊU ĐỀ Dòng một vẫn cùng đoạn.\n\nĐoạn hai\n- ý nhỏ"


def test_parse_article_skips_ad_image_and_returns_clean_text():
    html = """<div class="detail-content">
      <img src="https://cdn.x/ads/banner_300x250.jpg">
      <img data-src="/photo/real.jpg">
      <p>Nội dung <b>chính</b></p><figcaption>Ảnh 1. chú thích</figcaption>
      <script>track()</script>
    </div>"""

    text, image = market_page._parse_article(html, "https://cafef.vn/bai.chn")

    assert image == "https://cafef.vn/photo/real.jpg"
    assert text == "Nội dung chính"


def test_parse_article_falls_back_to_og_image_and_handles_missing_body():
    html = (
        '<head><meta property="og:image" content="https://cdn.x/og.jpg"></head>'
        '<div class="detail-content"><p>Chữ</p></div>'
    )

    assert market_page._parse_article(html, "https://cafef.vn/a")[1] == "https://cdn.x/og.jpg"
    assert market_page._parse_article("<p>không có</p>", "https://cafef.vn/a") == ("", None)


# ─── Lịch ───────────────────────────────────────────────────────────────────


def test_next_slot_orders_the_daily_slots():
    friday = lambda h, m: datetime(2026, 10, 9, h, m, tzinfo=VN)  # noqa: E731

    assert market_page._next_slot(friday(8, 0)) == (friday(8, 30), "news")
    assert market_page._next_slot(friday(8, 30)) == (friday(15, 20), "stock")
    saturday_news = datetime(2026, 10, 10, 8, 30, tzinfo=VN)
    assert market_page._next_slot(friday(15, 20)) == (saturday_news, "news")


def test_next_slot_skips_stock_report_on_weekend_but_keeps_news():
    friday_evening = datetime(2026, 10, 9, 16, 0, tzinfo=VN)
    saturday_news = datetime(2026, 10, 10, 8, 30, tzinfo=VN)
    sunday_after_news = datetime(2026, 10, 11, 9, 0, tzinfo=VN)
    monday_news = datetime(2026, 10, 12, 8, 30, tzinfo=VN)

    assert market_page._next_slot(friday_evening) == (saturday_news, "news")
    assert market_page._next_slot(sunday_after_news) == (monday_news, "news")


# ─── Page riêng không lẫn với Shopee ────────────────────────────────────────


def test_market_page_is_hidden_from_default_page_list(monkeypatch):
    for name in ("FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_ID_2", "FACEBOOK_PAGE_ID_MARKET"):
        monkeypatch.setenv(name, "1")
    for name in (
        "FACEBOOK_PAGE_ACCESS_TOKEN",
        "FACEBOOK_PAGE_ACCESS_TOKEN_2",
        "FACEBOOK_PAGE_ACCESS_TOKEN_MARKET",
    ):
        monkeypatch.setenv(name, "t")

    assert facebook_page_service.configured_page_keys() == ["default", "2"]
    assert facebook_page_service.configured_page_keys(include_dedicated=True) == [
        "default",
        "2",
        "MARKET",
    ]


def test_settings_for_market_page_read_the_market_env_names(monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID_MARKET", "page-m")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN_MARKET", "tok-m")

    page_id, token, _ = facebook_page_service._settings(facebook_page_service.MARKET_PAGE_KEY)

    assert (page_id, token) == ("page-m", "tok-m")


# ─── run_once ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_posts_each_slot_only_once(store, monkeypatch):
    calls = []

    async def fake_job(dry_run=False):
        calls.append(1)
        return "nội dung"

    monkeypatch.setitem(market_page._JOBS, "stock", fake_job)
    when = datetime(2026, 10, 9, 8, 45, tzinfo=VN)

    assert await market_page.run_once("stock", when) is True
    assert await market_page.run_once("stock", when) is False
    assert len(calls) == 1
    assert await market_page.run_once("stock", when, force=True) is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_run_once_does_not_mark_slot_when_nothing_was_posted(store, monkeypatch):
    async def fake_job(dry_run=False):
        return None

    monkeypatch.setitem(market_page._JOBS, "news", fake_job)

    assert await market_page.run_once("news", datetime(2026, 10, 9, 8, 30, tzinfo=VN)) is False
    assert list(store.data) == ["market_page:last:news"]  # chỉ ghi trạng thái, không đánh dấu slot


@pytest.mark.asyncio
async def test_run_once_marks_slot_when_facebook_outcome_is_uncertain(store, monkeypatch):
    async def fake_job(dry_run=False):
        raise facebook_page_service.FacebookPublicationUncertain("không rõ")

    monkeypatch.setitem(market_page._JOBS, "stock", fake_job)
    when = datetime(2026, 10, 9, 15, 20, tzinfo=VN)

    assert await market_page.run_once("stock", when) is True
    assert await market_page.run_once("stock", when) is False  # không đăng lại


# ─── Job đầu-cuối với dịch vụ ngoài giả lập ─────────────────────────────────


def _published(post_id="page_post"):
    return SimpleNamespace(post_id=post_id)


@pytest.mark.asyncio
async def test_stock_report_publishes_chart_and_text_to_market_page(monkeypatch):
    published = []

    async def fake_bars():
        return {"VNINDEX": _bars([1200.0, 1212.0]), "AAA": _bars([10.0, 11.0])}

    async def fake_ask(prompt):
        assert "1212.0" in prompt
        return SimpleNamespace(text="**📌 Phiên tăng điểm**\n" + "Nhận định chi tiết. " * 10)

    async def fake_chart(bars, report_date):
        return b"png-bytes"

    async def fake_publish(content, media, page_key="default", **_):
        published.append((content, media, page_key))
        return _published()

    monkeypatch.setattr(market_page, "_fetch_all_bars", fake_bars)
    monkeypatch.setattr(orchestrator, "ask", fake_ask)
    monkeypatch.setattr(market_page, "_render_chart", fake_chart)
    monkeypatch.setattr(market_page, "publish_page_post", fake_publish)

    result = await market_page._post_stock_report()

    content, media, page_key = published[0]
    assert result == content
    assert "*" not in content and content.startswith("📌")
    assert "Nguồn: dữ liệu giá và khối lượng từ DNSE" in content and "02/09/2026" in content
    assert "không phải khuyến nghị đầu tư" in content
    assert content.rstrip().endswith("#chungkhoanvietnam")
    assert media == [("image/png", b"png-bytes")]
    assert page_key == "MARKET"


@pytest.mark.asyncio
async def test_stock_report_is_skipped_without_vnindex_or_with_garbage_ai_output(monkeypatch):
    publish_calls = []

    async def fake_publish(*args, **kwargs):
        publish_calls.append(1)

    async def no_vnindex():
        return {"VNINDEX": [], "AAA": _bars([10.0, 11.0])}

    async def with_vnindex():
        return {"VNINDEX": _bars([1200.0, 1212.0])}

    async def short_ask(prompt):
        return SimpleNamespace(text="ok")

    monkeypatch.setattr(market_page, "publish_page_post", fake_publish)
    monkeypatch.setattr(orchestrator, "ask", short_ask)

    monkeypatch.setattr(market_page, "_fetch_all_bars", no_vnindex)
    assert await market_page._post_stock_report() is None

    monkeypatch.setattr(market_page, "_fetch_all_bars", with_vnindex)
    assert await market_page._post_stock_report() is None
    assert publish_calls == []


def _news_fakes(monkeypatch, *, comment_error=None):
    now = datetime.now(timezone.utc)
    stamp = f"{now:%a, %d %b %Y %H:%M:%S} +0000"
    items = "".join(
        f"<item><title>{title}</title><link>https://cafef.vn/bai-{i}.chn</link>"
        f"<description>{summary}</description><pubDate>{stamp}</pubDate></item>"
        for i, (title, summary) in enumerate(
            [
                ("VN-Index bùng nổ", "khối ngoại mua ròng"),
                ("Lãi suất liên ngân hàng giảm", "tin vĩ mô"),
                ("Doanh nghiệp chia cổ tức", "cổ tức tiền mặt"),
            ],
            start=1,
        )
    )
    feed = (
        f"<rss version='2.0'><channel><title>x</title>{items}</channel></rss>"
    ).encode()
    article = (
        '<div class="detail-content"><img src="https://cdn.x/p.jpg"><p>Thân bài chi tiết</p></div>'
    )
    calls = SimpleNamespace(publish=[], comments=[], prompts=[])

    async def fake_get(url):
        if url.endswith(".rss"):
            return SimpleNamespace(content=feed, text="", headers={})
        if url.endswith(".jpg"):
            return SimpleNamespace(
                content=b"jpg-bytes", text="", headers={"content-type": "image/jpeg"}
            )
        return SimpleNamespace(content=b"", text=article, headers={})

    async def fake_ask(prompt):
        calls.prompts.append(prompt)
        if comment_error and "tóm lược" in prompt:
            raise comment_error
        return SimpleNamespace(text="Nội dung đã tổng hợp khá dài để vượt ngưỡng tối thiểu. " * 3)

    async def fake_publish(content, media, page_key="default", **_):
        calls.publish.append((content, media, page_key))
        return _published("page_post")

    async def fake_comment(post_id, message, page_key="default"):
        calls.comments.append((post_id, message, page_key))
        return "c1"

    monkeypatch.setattr(market_page, "_get", fake_get)
    monkeypatch.setattr(orchestrator, "ask", fake_ask)
    monkeypatch.setattr(market_page, "publish_page_post", fake_publish)
    monkeypatch.setattr(market_page, "post_comment", fake_comment)
    return calls


@pytest.mark.asyncio
async def test_news_posts_digest_with_article_image_then_comments_rewrite(monkeypatch):
    calls = _news_fakes(monkeypatch)

    result = await market_page._post_news()

    (content, media, page_key), = calls.publish
    assert result == content
    assert page_key == "MARKET"
    assert media == [("image/jpeg", b"jpg-bytes")]
    assert "Nguồn: CafeF (cafef.vn)" in content and "không phải khuyến nghị đầu tư" in content
    assert "VN-Index bùng nổ" in calls.prompts[0]
    post_id, comment, comment_page = calls.comments[0]
    assert post_id == "page_post" and comment_page == "MARKET"
    assert "Đọc bài gốc trên CafeF: VN-Index bùng nổ" in comment
    assert "https://cafef.vn/bai-1.chn" in comment
    assert "không phải khuyến nghị đầu tư" in comment
    assert "Thân bài chi tiết" in calls.prompts[1]


@pytest.mark.asyncio
async def test_news_comment_failure_does_not_fail_the_job(monkeypatch):
    calls = _news_fakes(monkeypatch, comment_error=RuntimeError("AI hết quota"))

    assert await market_page._post_news()
    assert len(calls.publish) == 1 and calls.comments == []


# ─── Tuân thủ: không khuyến nghị, có nguồn + miễn trừ ───────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Nhà đầu tư nên mua cổ phiếu ngân hàng",
        "Khuyến nghị bán VIC ở vùng này",
        "Đề xuất tăng tỷ trọng cổ phiếu lên 70%",
        "Giá mục tiêu 120.000 đồng",
        "Phân bổ tỷ trọng tiền mặt 30%",
    ],
)
def test_investment_advice_is_detected(text):
    assert market_page._has_investment_advice(text)


@pytest.mark.parametrize(
    "text",
    [
        "Khối ngoại bán ròng 500 tỷ, áp lực chốt lời gia tăng",
        "VN-Index đóng cửa trên MA20, thanh khoản cải thiện",
        "Nếu giữ được MA50 thì thị trường có thể hồi phục",
    ],
)
def test_neutral_market_commentary_is_not_flagged(text):
    assert not market_page._has_investment_advice(text)


def test_prompts_forbid_advice_and_demand_sources():
    report = market_page._build_report({"VNINDEX": _bars([1200.0, 1212.0])})

    stock = market_page._stock_prompt(report)
    digest = market_page._digest_prompt([_entry("Tin A", "tóm tắt")])
    rewrite = market_page._rewrite_prompt("Nội dung bài")

    assert "KHÔNG khuyến nghị mua/bán/nắm giữ" in stock and "PHIÊN 02/09/2026" in stock
    assert "tỷ lệ phân bổ" not in stock
    for prompt in (stock, digest, rewrite):
        assert "PHONG CÁCH VIẾT" in prompt and "theo dữ liệu được cung cấp" in prompt  # nằm trong danh sách cấm
    assert "tóm lược" not in digest  # fake AI trong test phân biệt prompt comment bằng từ này
    assert "TUYỆT ĐỐI KHÔNG đưa ra khuyến nghị mua/bán/nắm giữ" in digest and "theo CafeF" in digest
    assert "bỏ phần đó" in rewrite


@pytest.mark.asyncio
async def test_stock_report_with_trade_advice_is_not_published(monkeypatch):
    publish_calls = []

    async def fake_bars():
        return {"VNINDEX": _bars([1200.0, 1212.0])}

    async def advice_ask(prompt):
        return SimpleNamespace(text="📌 Phiên tăng\n" + "Nhà đầu tư nên mua thêm cổ phiếu. " * 5)

    async def fake_publish(*args, **kwargs):
        publish_calls.append(1)

    monkeypatch.setattr(market_page, "_fetch_all_bars", fake_bars)
    monkeypatch.setattr(orchestrator, "ask", advice_ask)
    monkeypatch.setattr(market_page, "publish_page_post", fake_publish)

    assert await market_page._post_stock_report() is None
    assert publish_calls == []


@pytest.mark.asyncio
async def test_news_digest_with_trade_advice_is_not_published(monkeypatch):
    calls = _news_fakes(monkeypatch)

    async def advice_ask(prompt):
        return SimpleNamespace(text="Hôm nay khuyến nghị mua cổ phiếu thép, giá mục tiêu cao. " * 3)

    monkeypatch.setattr(orchestrator, "ask", advice_ask)

    assert await market_page._post_news() is None
    assert calls.publish == []


@pytest.mark.asyncio
async def test_news_comment_with_trade_advice_is_skipped_but_post_stays(monkeypatch):
    calls = _news_fakes(monkeypatch)

    async def ask(prompt):
        if "tóm lược" in prompt:
            return SimpleNamespace(text="SSI khuyến nghị mua cổ phiếu HPG, giá mục tiêu 30.000.")
        return SimpleNamespace(text="Bản tin đã tổng hợp khá dài để vượt ngưỡng tối thiểu. " * 3)

    monkeypatch.setattr(orchestrator, "ask", ask)

    assert await market_page._post_news()
    assert len(calls.publish) == 1 and calls.comments == []


# ─── Chế độ xem thử, chạy thủ công, giờ đăng ───────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_returns_text_without_chart_or_publish(monkeypatch):
    async def fake_bars():
        return {"VNINDEX": _bars([1200.0, 1212.0])}

    async def fake_ask(prompt):
        return SimpleNamespace(text="📌 Phiên tăng điểm\n" + "Nhận định chi tiết. " * 10)

    async def boom(*args, **kwargs):
        raise AssertionError("xem thử không được tạo biểu đồ hay đăng bài")

    monkeypatch.setattr(market_page, "_fetch_all_bars", fake_bars)
    monkeypatch.setattr(orchestrator, "ask", fake_ask)
    monkeypatch.setattr(market_page, "_render_chart", boom)
    monkeypatch.setattr(market_page, "publish_page_post", boom)

    text = await market_page._post_stock_report(dry_run=True)

    assert "không phải khuyến nghị đầu tư" in text and "DNSE" in text


@pytest.mark.asyncio
async def test_news_dry_run_does_not_publish_or_comment(monkeypatch):
    calls = _news_fakes(monkeypatch)

    text = await market_page._post_news(dry_run=True)

    assert "Nguồn: CafeF" in text
    assert calls.publish == [] and calls.comments == []


@pytest.mark.asyncio
async def test_run_manual_preview_does_not_need_page_config_or_record_status(store, monkeypatch):
    async def fake_job(dry_run=False):
        return "nội dung xem thử" if dry_run else "đã đăng"

    monkeypatch.setitem(market_page._JOBS, "stock", fake_job)
    monkeypatch.delenv("FACEBOOK_PAGE_ID_MARKET", raising=False)

    assert await market_page.run_manual("stock", publish=False) == "nội dung xem thử"
    assert store.data == {}


@pytest.mark.asyncio
async def test_run_manual_publish_requires_page_and_records_status(store, monkeypatch):
    async def fake_job(dry_run=False):
        return "đã đăng"

    monkeypatch.setitem(market_page._JOBS, "stock", fake_job)
    monkeypatch.delenv("FACEBOOK_PAGE_ID_MARKET", raising=False)
    monkeypatch.delenv("FACEBOOK_PAGE_ACCESS_TOKEN_MARKET", raising=False)

    with pytest.raises(market_page.MarketPageError):
        await market_page.run_manual("stock", publish=True)

    monkeypatch.setenv("FACEBOOK_PAGE_ID_MARKET", "page-m")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN_MARKET", "tok-m")
    assert await market_page.run_manual("stock", publish=True) == "đã đăng"
    assert store.data["market_page:last:stock"].endswith("đã đăng")


@pytest.mark.asyncio
async def test_run_manual_rejects_unknown_job_and_overlapping_runs():
    with pytest.raises(market_page.MarketPageError):
        await market_page.run_manual("khac", publish=False)

    async with market_page._run_lock:
        with pytest.raises(market_page.MarketPageError):
            await market_page.run_manual("stock", publish=False)


@pytest.mark.asyncio
async def test_failed_scheduled_run_is_recorded_for_admin(store, monkeypatch):
    async def fake_job(dry_run=False):
        raise ValueError("hỏng")

    monkeypatch.setitem(market_page._JOBS, "news", fake_job)

    with pytest.raises(ValueError):
        await market_page.run_once("news", datetime(2026, 10, 9, 8, 30, tzinfo=VN))

    assert "lỗi ValueError" in store.data["market_page:last:news"]


def test_schedule_times_come_from_config_with_fallback_on_bad_values(monkeypatch):
    monkeypatch.setattr(config, "MARKET_STOCK_TIMES_VN", "9:05, 14:30")
    monkeypatch.setattr(config, "MARKET_NEWS_TIMES_VN", "khong-phai-gio")

    slots = {(job, f"{at:%H:%M}") for job, at, _ in market_page._schedule()}

    assert slots == {("stock", "09:05"), ("stock", "14:30"), ("news", "08:30")}


def test_schedule_defaults_and_weekday_rules():
    rows = market_page._schedule()

    assert [(job, f"{at:%H:%M}", len(days)) for job, at, days in rows] == [
        ("news", "08:30", 7),
        ("stock", "15:20", 5),
    ]


@pytest.mark.asyncio
async def test_status_reports_schedule_next_slot_and_last_runs(store, monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID_MARKET", "page-m")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN_MARKET", "tok-m")
    store.data["market_page:last:stock"] = "09/10 08:45 — đã đăng"

    info = await market_page.status()

    assert info["configured"] is True and info["busy"] is False
    assert [row["time"] for row in info["schedule"]] == ["08:30", "15:20"]
    assert info["next"]["job"] in {"news", "stock"}
    assert info["last"] == {"stock": "09/10 08:45 — đã đăng", "news": None}


# ─── Lệnh /fb_market ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fb_market_defaults_to_preview(monkeypatch):
    seen = []

    async def fake_run(job, *, publish):
        seen.append((job, publish))
        return "bài xem thử"

    monkeypatch.setattr(market_page, "run_manual", fake_run)

    result = await facebook_commands.maybe_handle_facebook_command("acc", "/fb_market stock")

    assert seen == [("stock", False)]
    assert "XEM THỬ" in result.messages[0] and "/fb_market stock dang" in result.messages[0]


@pytest.mark.asyncio
async def test_fb_market_dang_publishes(monkeypatch):
    seen = []

    async def fake_run(job, *, publish):
        seen.append((job, publish))
        return "bài đã đăng"

    monkeypatch.setattr(market_page, "run_manual", fake_run)

    result = await facebook_commands.maybe_handle_facebook_command("acc", "/fb_market NEWS dang")

    assert seen == [("news", True)]
    assert result.messages[0].startswith("✅ Đã đăng news")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/fb_market", "/fb_market abc", "/fb_market stock xoa"])
async def test_fb_market_bad_syntax_shows_usage_without_running(monkeypatch, text):
    async def boom(*args, **kwargs):
        raise AssertionError("không được chạy job khi sai cú pháp")

    monkeypatch.setattr(market_page, "run_manual", boom)

    result = await facebook_commands.maybe_handle_facebook_command("acc", text)

    assert result.messages[0].startswith("Cú pháp: /fb_market")


@pytest.mark.asyncio
async def test_fb_market_reports_operational_errors(monkeypatch):
    async def not_configured(job, *, publish):
        raise market_page.MarketPageError("Chưa cấu hình page")

    async def uncertain(job, *, publish):
        raise facebook_page_service.FacebookPublicationUncertain("không rõ")

    monkeypatch.setattr(market_page, "run_manual", not_configured)
    result = await facebook_commands.maybe_handle_facebook_command("acc", "/fb_market stock dang")
    assert result.messages == ["❌ Chưa cấu hình page"]

    monkeypatch.setattr(market_page, "run_manual", uncertain)
    result = await facebook_commands.maybe_handle_facebook_command("acc", "/fb_market stock dang")
    assert "Chưa rõ Facebook đã tạo bài" in result.messages[0]


# ─── Các lỗi đã sửa sau review ──────────────────────────────────────────────


def test_volume_over_one_billion_uses_vietnamese_separators():
    bars = _bars([1200.0, 1212.0], volume=1_234_500_000.0)
    prompt = market_page._stock_prompt(market_page._build_report({"VNINDEX": bars}))
    assert "Khối lượng khớp 1.234,5 triệu" in prompt


def test_breadth_is_labelled_as_tracked_group_not_whole_market():
    report = market_page._build_report(
        {"VNINDEX": _bars([1200.0, 1212.0]), "AAA": _bars([10.0, 11.0]), "BBB": _bars([5.0, 4.0])}
    )
    prompt = market_page._stock_prompt(report)
    assert "Trong nhóm 2 cổ phiếu hệ thống theo dõi (KHÔNG phải toàn thị trường)" in prompt
    assert "vốn hóa lớn" not in prompt


def test_rsi_uses_wilder_smoothing_after_the_first_window():
    values = [100.0]
    for step in [2, -1] * 7 + [-3, -3]:
        values.append(values[-1] + step)
    gain, loss = 14 / 14, 7 / 14
    for delta in (-3, -3):
        gain, loss = gain * 13 / 14, (loss * 13 + 3) / 14
    assert market_page._rsi(values) == pytest.approx(100 - 100 / (1 + gain / loss))


@pytest.mark.parametrize(
    "text",
    [
        "Nhà đầu tư có thể canh mua khi VN-Index về MA20",
        "Nên cân nhắc mua vào quanh 1.250 điểm",
        "Chốt lời một phần danh mục",
        "Ưu tiên giải ngân nhóm ngân hàng",
        "Vùng mua hợp lý quanh 1.240 điểm",
        "Hạ tỷ trọng về mức an toàn",
    ],
)
def test_softer_trade_advice_is_now_detected(text):
    assert market_page._has_investment_advice(text)


@pytest.mark.parametrize(
    "text",
    [
        "Nhà đầu tư bán tháo cuối phiên",
        "Dòng tiền tiếp tục giải ngân vào nhóm thép",
        "Khối ngoại có thể bán ròng tiếp",
        "Lực mua vào cuối phiên giúp chỉ số hồi",
        "Ngân hàng đẩy mạnh giải ngân tín dụng",
    ],
)
def test_market_descriptions_are_still_allowed(text):
    assert not market_page._has_investment_advice(text)


@pytest.mark.asyncio
async def test_ai_gets_one_retry_when_text_contains_advice(monkeypatch):
    answers = iter([
        "📌 Phiên tăng\n" + "Nhà đầu tư nên mua thêm. " * 5,
        "📌 Phiên tăng điểm\n" + "Chỉ số giữ trên MA20. " * 8,
    ])
    prompts = []

    async def ask(prompt):
        prompts.append(prompt)
        return SimpleNamespace(text=next(answers))

    async def fake_bars():
        return {"VNINDEX": _bars([1200.0, 1212.0])}

    async def fake_publish(*args, **kwargs):
        return _published()

    async def no_chart(*args):
        return None

    monkeypatch.setattr(orchestrator, "ask", ask)
    monkeypatch.setattr(market_page, "_fetch_all_bars", fake_bars)
    monkeypatch.setattr(market_page, "_render_chart", no_chart)
    monkeypatch.setattr(market_page, "publish_page_post", fake_publish)

    assert "giữ trên MA20" in await market_page._post_stock_report()
    assert "LƯU Ý" in prompts[1]


def _stock_fakes(monkeypatch, published):
    async def fake_bars():
        return {"VNINDEX": _bars([1200.0, 1212.0])}

    async def fake_ask(prompt):
        return SimpleNamespace(text="📌 Phiên tăng điểm\n" + "Nhận định chi tiết. " * 10)

    async def no_chart(*args):
        return None

    async def fake_publish(content, media, page_key="default", **_):
        published.append(content)
        return _published()

    monkeypatch.setattr(market_page, "_fetch_all_bars", fake_bars)
    monkeypatch.setattr(orchestrator, "ask", fake_ask)
    monkeypatch.setattr(market_page, "_render_chart", no_chart)
    monkeypatch.setattr(market_page, "publish_page_post", fake_publish)


@pytest.mark.asyncio
async def test_holiday_or_stale_data_is_not_posted_and_will_be_retried(monkeypatch):
    published = []
    _stock_fakes(monkeypatch, published)
    monkeypatch.setattr(market_page, "_today", lambda: date(2026, 9, 3))
    monkeypatch.setattr(
        market_page, "_is_current_session",
        lambda report_date: report_date == market_page._today().isoformat(),
    )

    with pytest.raises(market_page.MarketSkip) as exc:
        await market_page._post_stock_report()

    assert exc.value.retry is True and "02/09/2026" in str(exc.value)
    assert published == []
    # Xem thử vẫn chạy được để kiểm tra nội dung.
    assert await market_page._post_stock_report(dry_run=True)


@pytest.mark.asyncio
async def test_same_session_is_never_posted_twice_even_manually(store, monkeypatch):
    published = []
    _stock_fakes(monkeypatch, published)
    monkeypatch.setenv("FACEBOOK_PAGE_ID_MARKET", "page-m")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN_MARKET", "tok-m")

    assert await market_page.run_manual("stock", publish=True)
    # Lượt theo lịch sau đó: phiên đã đăng -> slot xong, không đăng lần 2.
    assert await market_page.run_once("stock", datetime(2026, 9, 2, 15, 20, tzinfo=VN)) is True
    with pytest.raises(market_page.MarketPageError, match="đã được đăng"):
        await market_page.run_manual("stock", publish=True)
    assert len(published) == 1
    assert "đã được đăng" in store.data["market_page:last:stock"]


@pytest.mark.asyncio
async def test_chart_is_branded_like_zalo_posts(monkeypatch):
    import io

    from PIL import Image

    media_seen = []
    png = io.BytesIO()
    Image.new("RGB", (800, 450), "white").save(png, "PNG")

    async def chart(*args):
        return png.getvalue()

    async def fake_publish(content, media, page_key="default", **_):
        media_seen.extend(media)
        return _published()

    _stock_fakes(monkeypatch, [])
    monkeypatch.setattr(market_page, "_render_chart", chart)
    monkeypatch.setattr(market_page, "publish_page_post", fake_publish)

    await market_page._post_stock_report()

    mime, body = media_seen[0]
    assert mime == "image/jpeg" and Image.open(io.BytesIO(body)).size[0] > 800  # có khung


@pytest.mark.asyncio
async def test_news_skips_when_feed_has_no_fresh_items(monkeypatch):
    calls = _news_fakes(monkeypatch)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    monkeypatch.setattr(
        market_page, "_parse_entries",
        lambda feed: [_entry(f"Tin {i}", published=old) for i in range(5)],
    )

    with pytest.raises(market_page.MarketSkip, match="24 giờ"):
        await market_page._post_news()
    assert calls.publish == []


@pytest.mark.asyncio
async def test_news_with_same_items_as_last_digest_is_not_reposted(monkeypatch):
    calls = _news_fakes(monkeypatch)

    assert await market_page._post_news()
    with pytest.raises(market_page.MarketSkip, match="tin mới"):
        await market_page._post_news()
    assert len(calls.publish) == 1


@pytest.mark.asyncio
async def test_news_image_can_be_turned_off(monkeypatch):
    calls = _news_fakes(monkeypatch)
    monkeypatch.setenv("MARKET_NEWS_IMAGE", "none")

    await market_page._post_news()

    assert calls.publish[0][1] == []


def test_comment_prompt_is_a_short_summary_not_a_full_rewrite():
    prompt = market_page._rewrite_prompt("Nội dung")
    assert "80-120 từ" in prompt and "không chép lại câu chữ" in prompt


# ─── Lịch: chạy bù sau restart, thử lại khi lỗi ─────────────────────────────


def test_due_slots_cover_catchup_window_only():
    at_1540 = datetime(2026, 10, 9, 15, 40, tzinfo=VN)  # thứ Sáu
    assert ("stock", datetime(2026, 10, 9, 15, 20, tzinfo=VN)) in market_page._due_slots(at_1540)
    assert market_page._due_slots(datetime(2026, 10, 9, 18, 0, tzinfo=VN)) == []
    saturday = datetime(2026, 10, 10, 15, 30, tzinfo=VN)
    assert all(job != "stock" for job, _ in market_page._due_slots(saturday))


@pytest.mark.asyncio
async def test_tick_retries_failed_slot_after_gap_until_max_attempts(store, monkeypatch):
    runs = []

    async def flaky(dry_run=False):
        runs.append(1)
        return None  # AI lỗi -> chưa đăng

    monkeypatch.setitem(market_page._JOBS, "stock", flaky)
    monkeypatch.setattr(market_page, "_attempts", {})
    monkeypatch.setattr(market_page, "_schedule", lambda: (("stock", market_page.time(15, 20), range(5)),))
    base = datetime(2026, 10, 9, 15, 25, tzinfo=VN)

    await market_page._tick(base)
    await market_page._tick(base + timedelta(minutes=5))  # chưa đủ 10 phút: không chạy
    assert len(runs) == 1
    for minutes in (10, 20, 30, 40, 50):
        await market_page._tick(base + timedelta(minutes=minutes))
    assert len(runs) == 4  # tối đa 4 lần


@pytest.mark.asyncio
async def test_tick_runs_missed_slot_after_restart_and_stops_once_done(store, monkeypatch):
    runs = []

    async def ok(dry_run=False):
        runs.append(1)
        return "đã đăng"

    monkeypatch.setitem(market_page._JOBS, "stock", ok)
    monkeypatch.setattr(market_page, "_attempts", {})
    monkeypatch.setattr(market_page, "_schedule", lambda: (("stock", market_page.time(15, 20), range(5)),))

    await market_page._tick(datetime(2026, 10, 9, 15, 47, tzinfo=VN))  # vừa khởi động lại
    await market_page._tick(datetime(2026, 10, 9, 16, 30, tzinfo=VN))
    assert len(runs) == 1
