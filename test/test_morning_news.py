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
    yield


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

    assert "TIN TỨC BUỔI SÁNG" in digest
    assert "Thị trường sáng nay có 2 tin đáng chú ý" in digest


@pytest.mark.asyncio
async def test_build_digest_bo_qua_feed_loi_van_tong_hop_feed_con_lai(monkeypatch):
    monkeypatch.setattr(config, "MORNING_NEWS_RSS_FEEDS", ["https://a.example/rss", "https://b.example/rss"])

    async def fake_read_rss(url: str) -> str:
        if url == "https://a.example/rss":
            raise web_reader.WebReaderError("feed lỗi")
        return "[Feed: B]\n1. Tin B"

    async def fake_ask(prompt: str):
        return SimpleNamespace(text="Bản tin sáng nay chỉ có 1 tin từ nguồn B: cập nhật tình hình kinh doanh mới nhất.")

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

    assert result == "nội dung bản tin"
    assert enqueued["args"] == ("zalo-bot", "controller-1", "nội dung bản tin")
    assert sent_zoom["args"] == ("jid-1", "nội dung bản tin")


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
    monkeypatch.setattr(zalo_repository, "enqueue_outbox", lambda *a, **k: _async_return(None))
    monkeypatch.setattr(db, "zoom_get_pairing", lambda: _async_return(None))

    first = await morning_news.run_once(force=False)
    second = await morning_news.run_once(force=False)

    assert first == "nội dung"
    assert second is None  # đã gửi hôm nay rồi, không gửi/tổng hợp lại
    assert calls["n"] == 1


async def _async_return(value):
    return value
