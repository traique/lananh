"""Unit test cho services/morning_news.py."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import orchestrator  # noqa: E402
from channels import zalo_repository, zalo_session, zoom  # noqa: E402
from core import config, database as db  # noqa: E402
from services import morning_news, web_reader  # noqa: E402


class FakeSettingsStore:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    store = FakeSettingsStore()
    monkeypatch.setattr(db, "get_setting", store.get)
    monkeypatch.setattr(db, "set_setting", store.set)
    monkeypatch.setattr(config, "MORNING_NEWS_RSS_FEEDS", ["https://example.com/rss1.xml"])
    # Mặc định chưa ai bật bản tin cá nhân gộp tin; bản tin dùng chung trong RAM
    # không được rò từ test này sang test khác.
    monkeypatch.setattr(morning_news, "_absorbed_channels", lambda: _async_return(set()))
    morning_news.reset_shared_state()
    yield
    morning_news.reset_shared_state()


# ─── build_digest ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_digest_tra_ve_none_khi_chua_cau_hinh_feed(monkeypatch):
    monkeypatch.setattr(config, "MORNING_NEWS_RSS_FEEDS", [])
    assert await morning_news.build_digest() is None


@pytest.mark.asyncio
async def test_build_digest_goi_ai_tong_hop_tu_rss(monkeypatch):
    async def fake_read_rss(url: str) -> str:
        return "[Feed: Kinh doanh]\n1. Tin A\n2. Tin B"

    async def fake_ask(prompt: str):
        assert "Tin A" in prompt
        return SimpleNamespace(
            text="Thị trường sáng nay có 2 tin đáng chú ý: Tin A và Tin B, cả hai đều liên quan tới kinh doanh."
        )

    monkeypatch.setattr(web_reader, "read_rss", fake_read_rss)
    monkeypatch.setattr(orchestrator, "ask", fake_ask)

    digest = await morning_news.build_digest()

    assert "BẢN TIN SÁNG" in digest
    assert "Thị trường sáng nay có 2 tin đáng chú ý" in digest


@pytest.mark.asyncio
async def test_build_digest_danh_so_link_va_luu_de_doc_bai(monkeypatch):
    from services import article_reader

    async def fake_read_rss(url: str) -> str:
        return (
            "[Feed: VnExpress]\n1. Tin A\nTóm tắt A\nhttps://vnexpress.net/a.html\n\n"
            "2. Tin B\nTóm tắt B\nhttps://vnexpress.net/b.html"
        )

    async def fake_ask(prompt: str):
        return SimpleNamespace(
            text="Sáng nay có hai tin đáng chú ý về kinh doanh và thị trường trong nước, "
            "anh xem nhanh bên dưới nhé.\n"
            "IMPORTANT_IDS: F1I2, F1I1"
        )

    monkeypatch.setattr(web_reader, "read_rss", fake_read_rss)
    monkeypatch.setattr(orchestrator, "ask", fake_ask)

    digest = await morning_news.build_digest()

    assert "1. Tin B\n   https://vnexpress.net/b.html" in digest
    assert "2. Tin A\n   https://vnexpress.net/a.html" in digest
    assert '"đọc bài 1"' in digest
    saved = await db.get_setting(article_reader.LAST_LINKS_KEY)
    assert '"url": "https://vnexpress.net/b.html"' in saved


@pytest.mark.asyncio
async def test_build_digest_bo_qua_feed_loi_van_tong_hop_feed_con_lai(monkeypatch):
    monkeypatch.setattr(
        config, "MORNING_NEWS_RSS_FEEDS", ["https://a.example/rss", "https://b.example/rss"]
    )

    async def fake_read_rss(url: str) -> str:
        if url == "https://a.example/rss":
            raise web_reader.WebReaderError("feed lỗi")
        return "[Feed: B]\n1. Tin B"

    async def fake_ask(prompt: str):
        return SimpleNamespace(
            text="Bản tin sáng nay chỉ có 1 tin từ nguồn B: cập nhật tình hình kinh doanh mới nhất."
        )

    monkeypatch.setattr(web_reader, "read_rss", fake_read_rss)
    monkeypatch.setattr(orchestrator, "ask", fake_ask)

    digest = await morning_news.build_digest()

    assert "Bản tin sáng nay chỉ có 1 tin từ nguồn B" in digest


@pytest.mark.asyncio
async def test_build_digest_tra_ve_none_khi_tat_ca_feed_loi(monkeypatch):
    async def fake_read_rss(url: str) -> str:
        raise web_reader.WebReaderError("feed lỗi")

    monkeypatch.setattr(web_reader, "read_rss", fake_read_rss)

    assert await morning_news.build_digest() is None


@pytest.mark.asyncio
async def test_build_digest_bo_qua_khi_model_tra_loi_qua_ngan(monkeypatch):
    # Feed đọc được thật (feed_texts không rỗng) nhưng model lại trả lời
    # kiểu "không có tin" bất thường - phải coi là lỗi tổng hợp, KHÔNG gửi
    # bản tin vô nghĩa, dù bản thân feed hoàn toàn không lỗi.
    async def fake_read_rss(url: str) -> str:
        return "[Feed: Kinh doanh]\n1. Tin A thật sự tồn tại\nTóm tắt A\nhttps://a"

    async def fake_ask(prompt: str):
        return SimpleNamespace(text="Không có tin nào hết")

    monkeypatch.setattr(web_reader, "read_rss", fake_read_rss)
    monkeypatch.setattr(orchestrator, "ask", fake_ask)

    assert await morning_news.build_digest() is None


# ─── run_once: gửi Zalo + Zoom, guard idempotent ───────────────────────────


@pytest.mark.asyncio
async def test_run_once_gui_ca_zalo_va_zoom(monkeypatch):
    monkeypatch.setattr(morning_news, "build_digest", lambda: _async_return("nội dung bản tin"))
    monkeypatch.setattr(zalo_session, "load_controller", lambda: _async_return("controller-1"))
    monkeypatch.setattr(
        zalo_repository, "resolve_active_account_id", lambda: _async_return("zalo-bot")
    )

    enqueued = {}

    async def fake_enqueue(account_id, recipient_id, content):
        enqueued["args"] = (account_id, recipient_id, content)

    monkeypatch.setattr(zalo_repository, "enqueue_outbox", fake_enqueue)
    monkeypatch.setattr(db, "zoom_get_pairing", lambda: _async_return(("jid-1", "Chủ bot")))

    sent_zoom = {}

    async def fake_send_message(to_jid, text, **kwargs):
        sent_zoom["args"] = (to_jid, text)

    monkeypatch.setattr(zoom, "send_message", fake_send_message)

    result = await morning_news.run_once(force=True)

    assert result.content == "nội dung bản tin"
    assert result.sent_zalo is True
    assert result.sent_zoom is True
    assert enqueued["args"] == ("zalo-bot", "controller-1", "nội dung bản tin")
    assert sent_zoom["args"] == ("jid-1", "nội dung bản tin")


@pytest.mark.asyncio
async def test_run_once_bao_dung_that_bai_khi_khong_gui_duoc_kenh_nao(monkeypatch):
    # Bug đã xảy ra thật: build_digest() thành công (có content) nhưng CẢ 2
    # kênh gửi thất bại - kết quả PHẢI phản ánh đúng là chưa gửi được gì, để
    # /bantinsang không báo "✅ Đã gửi" trong khi thực ra không có gì tới nơi.
    monkeypatch.setattr(morning_news, "build_digest", lambda: _async_return("nội dung"))
    monkeypatch.setattr(zalo_session, "load_controller", lambda: _async_return(""))
    monkeypatch.setattr(db, "zoom_get_pairing", lambda: _async_return(None))

    result = await morning_news.run_once(force=True)

    assert result.content == "nội dung"
    assert result.sent_zalo is False
    assert result.sent_zoom is False


@pytest.mark.asyncio
async def test_run_once_chua_pair_kenh_nao_thi_khong_danh_dau_da_gui(monkeypatch):
    monkeypatch.setattr(morning_news, "build_digest", lambda: _async_return("nội dung"))
    monkeypatch.setattr(zalo_session, "load_controller", lambda: _async_return(""))
    monkeypatch.setattr(db, "zoom_get_pairing", lambda: _async_return(None))

    from datetime import datetime

    await morning_news.run_once(force=True)

    # Guard "đã gửi hôm nay" không được set vì chẳng gửi được kênh nào - lượt
    # sau (khi đã pair) vẫn phải thử gửi lại, không bị coi là "đã xong".
    now = datetime.now(morning_news._VN_TZ)
    assert await morning_news._already_sent_today(now) is False


@pytest.mark.asyncio
async def test_run_once_khong_gui_lai_trong_ngay_neu_da_gui_roi(monkeypatch):
    calls = {"n": 0}

    async def fake_build_digest():
        calls["n"] += 1
        return "nội dung"

    monkeypatch.setattr(morning_news, "build_digest", fake_build_digest)
    monkeypatch.setattr(zalo_session, "load_controller", lambda: _async_return("controller-1"))
    monkeypatch.setattr(
        zalo_repository, "resolve_active_account_id", lambda: _async_return("zalo-bot")
    )
    monkeypatch.setattr(zalo_repository, "enqueue_outbox", lambda *a, **k: _async_return(None))
    monkeypatch.setattr(db, "zoom_get_pairing", lambda: _async_return(None))

    first = await morning_news.run_once(force=False)
    second = await morning_news.run_once(force=False)

    assert first.content == "nội dung"
    assert first.sent_zalo is True
    assert second.content is None  # đã gửi hôm nay rồi, không gửi/tổng hợp lại
    assert calls["n"] == 1


async def _async_return(value):
    return value


# ─── Dùng chung cho bản tin sáng cá nhân ────────────────────────────────────

_TWO_ITEMS = (
    "[Feed: VnExpress]\n1. Tin A\nTóm tắt A\nhttps://vnexpress.net/a.html\n\n"
    "2. Tin B\nTóm tắt B\nhttps://vnexpress.net/b.html"
)


@pytest.mark.asyncio
async def test_shared_digest_tong_hop_mot_lan_moi_ngay(monkeypatch):
    calls = {"n": 0}

    async def fake_build_digest():
        calls["n"] += 1
        return f"bản tin {calls['n']}"

    monkeypatch.setattr(morning_news, "build_digest", fake_build_digest)
    assert await morning_news.shared_digest() == "bản tin 1"
    assert await morning_news.shared_digest() == "bản tin 1"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_headlines_tat_dinh_khong_goi_ai_khong_ghi_de_doc_bai(monkeypatch):
    from services import article_reader

    reads = []

    async def fake_read_rss(url):
        reads.append(url)
        return _TWO_ITEMS

    async def no_ai(prompt):
        raise AssertionError("tin cho bản tin cá nhân không được gọi AI")

    monkeypatch.setattr(web_reader, "read_rss", fake_read_rss)
    monkeypatch.setattr(orchestrator, "ask", no_ai)
    items, from_digest = await morning_news.today_headlines()
    again, _ = await morning_news.today_headlines()

    assert [i.title for i in items] == ["Tin A", "Tin B"] and from_digest is False
    assert again == items and len(reads) == 1  # cache trong ngày
    assert await db.get_setting(article_reader.LAST_LINKS_KEY) is None
    text = morning_news.format_headlines(items, from_digest)
    assert "1. Tin A (VnExpress)\n   https://vnexpress.net/a.html" in text
    assert "Gửi lại link" in text and "đọc bài" not in text


@pytest.mark.asyncio
async def test_headlines_dung_lai_danh_sach_doc_bai_cua_ban_tin_ai(monkeypatch):
    async def fake_read_rss(url):
        return _TWO_ITEMS

    async def fake_ask(prompt):
        return SimpleNamespace(
            text="Sáng nay có hai tin đáng chú ý về kinh doanh và thị trường trong nước, "
            "anh xem nhanh bên dưới nhé.\nIMPORTANT_IDS: F1I2"
        )

    monkeypatch.setattr(web_reader, "read_rss", fake_read_rss)
    monkeypatch.setattr(orchestrator, "ask", fake_ask)
    await morning_news.build_digest()
    items, from_digest = await morning_news.today_headlines()
    assert from_digest is True and items[0].title == "Tin B"
    assert '"đọc bài 1"' in morning_news.format_headlines(items, from_digest)


def test_is_due_khung_gui_bu(monkeypatch):
    from datetime import datetime

    from core import config as cfg

    monkeypatch.setattr(cfg, "MORNING_NEWS_HOUR_VN", 8)
    tz = morning_news._VN_TZ
    assert morning_news.is_due(datetime(2026, 5, 4, 7, 59, tzinfo=tz)) is False
    assert morning_news.is_due(datetime(2026, 5, 4, 8, 0, tzinfo=tz)) is True
    assert morning_news.is_due(datetime(2026, 5, 4, 10, 59, tzinfo=tz)) is True
    assert morning_news.is_due(datetime(2026, 5, 4, 11, 0, tzinfo=tz)) is False


# ─── Bản tin 28/10/2026: số bịa, chức danh bịa, tiêu đề dính giờ, dòng trống ──


@pytest.mark.asyncio
async def test_bo_so_va_chuc_danh_khong_co_trong_rss(monkeypatch):
    feed = (
        "[Feed: VnExpress]\n"
        "1. Giá vàng đảo chiều tăng 1 triệu đồng (Wed, 28 Oct 2026 09:10:00 +0700)\n"
        "Vàng miếng SJC lên 142 triệu đồng mỗi lượng; thế giới vượt 4.060 USD một ounce.\n"
        "https://vnexpress.net/vang.html\n\n"
        "2. Ông Trump nêu điều kiện chấp nhận kết quả bầu cử giữa kỳ\n"
        "Tổng thống Mỹ Donald Trump nói sẽ chấp nhận nếu bầu cử công bằng.\n"
        "https://vnexpress.net/trump.html"
    )
    prompts = []

    async def fake_read_rss(url: str) -> str:
        return feed

    async def fake_ask(prompt: str):
        prompts.append(prompt)
        return SimpleNamespace(
            text=(
                "📈 KINH TẾ & THỊ TRƯỜNG\n"
                "SJC lên 142 triệu đồng mỗi lượng, thế giới vượt 4,060 USD. "
                "VN-Index giảm 35 điểm trong phiên sáng.\n"
                "🌍 THẾ GIỚI\n"
                "Tại Mỹ, cựu Tổng thống Donald Trump nêu điều kiện chấp nhận kết quả bầu cử.\n"
                "💻 CÔNG NGHỆ & KHOA HỌC\n"
                "Redmi bán 120.000 máy trong 5 phút.\n"
                "IMPORTANT_IDS: F1I1,F1I2"
            )
        )

    async def no_article(url):
        raise morning_news.article_reader.ArticleError("không đọc được")

    monkeypatch.setattr(web_reader, "read_rss", fake_read_rss)
    monkeypatch.setattr(orchestrator, "ask", fake_ask)
    monkeypatch.setattr(morning_news.article_reader, "read_article", no_article)
    digest = await morning_news.build_digest("on_demand")

    assert "SJC lên 142 triệu đồng mỗi lượng, thế giới vượt 4,060 USD." in digest
    assert "35 điểm" not in digest  # số không có trong RSS: bỏ cả câu
    assert "CÔNG NGHỆ" not in digest and "120.000" not in digest  # mục rỗng: bỏ luôn tiêu đề
    assert "Tại Mỹ, Tổng thống Donald Trump" in digest and "cựu" not in digest
    assert "\n\n🌍 THẾ GIỚI\n" in digest  # tiêu đề mục cách đoạn trước 1 dòng trống
    assert "Hôm nay là " in prompts[-1] and 'không gọi ai là "cựu ..."' in prompts[-1]
    assert "mỗi tin một sự kiện khác nhau" in prompts[0]  # lượt chọn tin


def test_tieu_de_tuoi_tre_khong_dinh_gio_dang():
    items = morning_news._parse_feed_items(
        "[Feed: Tuổi Trẻ]\n1. Giá vàng hôm nay 28-10: SJC cũng đi lên (10/28/2026 8:33:00 AM)\n"
        "Tóm tắt\nhttps://tuoitre.vn/vang.htm\n\n2. Apple ra iPhone (bản Pro)\nhttps://tuoitre.vn/ip.htm",
        1,
        "Tuổi Trẻ",
    )
    assert [i.title for i in items] == [
        "Giá vàng hôm nay 28-10: SJC cũng đi lên",
        "Apple ra iPhone (bản Pro)",  # ngoặc không phải ngày đăng: giữ
    ]


def test_bo_tieu_de_khi_gop_vao_ban_tin_ca_nhan():
    assert morning_news.without_header("☀️ BẢN TIN SÁNG — 28/10/2026\n\nNội dung") == "Nội dung"
    assert morning_news.without_header("Nội dung khác") == "Nội dung khác"


@pytest.mark.asyncio
async def test_tom_tat_tu_noi_dung_bai_khong_chi_tieu_de(monkeypatch):
    """Tiêu đề + mô tả RSS ngắn cho ra bản tin na ná tiêu đề: lượt 1 chọn tin, code đọc
    phần đầu từng bài, lượt 2 tóm tắt nội dung và ghi "(bài N)" khớp "đọc bài N"."""
    from services import article_reader

    feed = (
        "[Feed: CafeF]\n"
        "1. DHG báo lãi quý III thấp nhất 15 năm\nMô tả RSS ngắn.\nhttps://cafef.vn/dhg.chn\n\n"
        "2. Bảo Minh mua lại trái phiếu\nMô tả RSS B.\nhttps://cafef.vn/bm.chn\n\n"
        "3. Tin C\nhttps://cafef.vn/c.chn"
    )
    prompts, read = [], []

    async def fake_read_rss(url: str) -> str:
        return feed

    async def fake_read_article(url):
        read.append(url)
        if url.endswith("bm.chn"):
            raise article_reader.ArticleError("bị chặn")
        return article_reader.Article(
            url=url,
            sapo="Dược Hậu Giang lãi sau thuế 61 tỷ đồng quý III, giảm 52% so với cùng kỳ.",
            paragraphs=["Doanh thu thuần đạt 1.020 tỷ đồng.", "Đoạn dài " * 400],
        )

    async def fake_ask(prompt: str):
        prompts.append(prompt)
        if len(prompts) == 1:
            return SimpleNamespace(text="IMPORTANT_IDS: F1I1,F1I2")
        return SimpleNamespace(
            text="📈 KINH TẾ & THỊ TRƯỜNG\nDược Hậu Giang lãi 61 tỷ đồng quý III, giảm 52% "
            "so với cùng kỳ dù doanh thu 1.020 tỷ đồng (bài 1). Bảo Minh mua lại trái phiếu "
            "trước hạn (bài 2)."
        )

    monkeypatch.setattr(web_reader, "read_rss", fake_read_rss)
    monkeypatch.setattr(orchestrator, "ask", fake_ask)
    monkeypatch.setattr(article_reader, "read_article", fake_read_article)
    digest = await morning_news.build_digest("on_demand")

    assert len(prompts) == 2 and "CHỈ trả đúng 1 dòng" in prompts[0]
    # Bài model chọn đọc trước; dưới 5 bài thì bù theo thứ tự RSS như mục link cũ.
    assert read == ["https://cafef.vn/dhg.chn", "https://cafef.vn/bm.chn", "https://cafef.vn/c.chn"]
    summary = prompts[1]
    assert "Bài 1 - DHG báo lãi quý III thấp nhất 15 năm (CafeF)" in summary
    assert "giảm 52% so với cùng kỳ" in summary  # nội dung bài, không chỉ tiêu đề
    assert "Bài 2 - Bảo Minh mua lại trái phiếu (CafeF)\nMô tả RSS B." in summary  # đọc lỗi
    assert "Đoạn dài " * 180 not in summary  # mỗi bài chỉ lấy phần đầu (~1.500 ký tự)
    assert '"(bài 3)"' in summary and "KHÔNG chép lại hay diễn đạt lại tiêu đề" in summary
    assert "giảm 52%" in digest and "(bài 2)" in digest  # số có trong bài: giữ
    assert "1. DHG báo lãi quý III thấp nhất 15 năm\n   https://cafef.vn/dhg.chn" in digest


@pytest.mark.asyncio
async def test_luot_chon_tin_loi_thi_chon_tat_dinh(monkeypatch):
    from services import article_reader

    async def fake_read_rss(url: str) -> str:
        return "[Feed: VnExpress]\n1. Tin A\nhttps://vnexpress.net/a.html"

    calls = []

    async def fake_ask(prompt: str):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("provider lỗi")
        return SimpleNamespace(
            text="Tin A kể một chuyện cụ thể lấy từ thân bài, nêu rõ ai làm gì và vì sao "
            "đáng chú ý với người đọc sáng nay (bài 1)."
        )

    async def fake_read_article(url):
        return article_reader.Article(url=url, sapo="Thân bài tin A.")

    monkeypatch.setattr(web_reader, "read_rss", fake_read_rss)
    monkeypatch.setattr(orchestrator, "ask", fake_ask)
    monkeypatch.setattr(article_reader, "read_article", fake_read_article)
    digest = await morning_news.build_digest()
    assert "Bài 1 - Tin A (VnExpress)\nThân bài tin A." in calls[1]
    assert "1. Tin A\n   https://vnexpress.net/a.html" in digest
