"""Định tuyến tin nhắn Telegram theo loại (bot_app.build_application, filter PTB
thật): tin thoại, tài liệu, vị trí (bỏ cập nhật live location), catch-all cuối
cho sticker/video/file lạ; handler vị trí và câu trả lời tin chưa hỗ trợ."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram import Update, User

from core import config
from services import media_text, weather, weather_chat


@pytest.fixture(scope="module")
def routing():
    import bot_app

    old = config.TELEGRAM_TOKEN
    config.TELEGRAM_TOKEN = "123:ABC"
    try:
        app = bot_app.build_application()
    finally:
        config.TELEGRAM_TOKEN = old
    # CommandHandler cần username của bot (bình thường có sau initialize()/get_me).
    app.bot._bot_user = User(id=1, is_bot=True, first_name="Lan Anh", username="lananh_bot")
    handlers = app.handlers[0]

    def first(message: dict, *, edited: bool = False) -> str | None:
        base = {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "A"},
            **message,
        }
        if edited:
            base["edit_date"] = 1
        update = Update.de_json(
            {"update_id": 1, "edited_message" if edited else "message": base}, app.bot
        )
        for handler in handlers:
            if handler.check_update(update):
                return getattr(handler.callback, "__name__", str(handler.callback))
        return None

    return first


_FILE = {"file_id": "x", "file_unique_id": "y"}


def test_moi_loai_tin_vao_dung_handler(routing):
    sticker = {**_FILE, "width": 1, "height": 1, "is_animated": False, "is_video": False}
    assert routing({"sticker": {**sticker, "type": "regular"}}) == "unsupported_msg"
    assert routing({"voice": {**_FILE, "duration": 3}}) == "voice_msg"
    assert routing({"audio": {**_FILE, "duration": 3}}) == "voice_msg"
    pdf = {**_FILE, "mime_type": "application/pdf", "file_name": "a.pdf"}
    assert routing({"document": pdf}) == "document_msg"
    md = {**_FILE, "mime_type": "application/octet-stream", "file_name": "ghi-chu.md"}
    assert routing({"document": md}) == "document_msg"
    zip_file = {**_FILE, "mime_type": "application/zip", "file_name": "a.zip"}
    assert routing({"document": zip_file}) == "unsupported_msg"
    image = {**_FILE, "mime_type": "image/png", "file_name": "a.png"}
    assert routing({"document": image}) == "photo_msg"
    video = {**_FILE, "width": 1, "height": 1, "duration": 2}
    assert routing({"video": video}) == "unsupported_msg"
    assert routing({"text": "chào em"}) == "chat_msg"
    command = {"text": "/xyz", "entities": [{"type": "bot_command", "offset": 0, "length": 4}]}
    assert routing(command) == "unknown_cmd"


def test_vi_tri_tra_loi_mot_lan_khong_theo_live_location(routing):
    location = {"latitude": 10.77, "longitude": 106.70}
    assert routing({"location": location}) == "location_msg"
    live = {"location": {**location, "live_period": 900}}
    assert routing(live, edited=True) is None


def test_tin_dich_vu_bi_bo_qua(routing):
    joined = {"new_chat_members": [{"id": 5, "is_bot": False, "first_name": "B"}]}
    assert routing(joined) is None


# ─── Thời tiết theo toạ độ ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_thoi_tiet_theo_toa_do_khong_geocoding(monkeypatch):
    import datetime as dt

    seen = {}

    async def at(lat, lon, label, days=1):
        seen.update(lat=lat, lon=lon, label=label, days=days)
        return {
            "location": label,
            "source": "Open-Meteo",
            "now": {
                "description": "nhiều mây",
                "temperature_c": 31,
                "feels_like_c": 35,
                "humidity_percent": 70,
                "wind_kmh": 9,
            },
            "daily": [
                {
                    "date": "2026-10-26",
                    "weekday": "Thứ Hai",
                    "description": "mưa rào",
                    "min_c": 25,
                    "max_c": 32,
                    "rain_chance_percent": 80,
                    "uv_max": 9,
                }
            ],
        }

    async def no_air(*args, **kwargs):
        raise weather.WeatherError("không có AQI")

    async def geocode_must_not_run(*args, **kwargs):
        raise AssertionError("tin vị trí không được geocoding")

    monkeypatch.setattr(weather, "get_weather_at", at)
    monkeypatch.setattr(weather, "get_air_quality_at", no_air)
    monkeypatch.setattr(weather, "_geocode", geocode_must_not_run)
    text = await weather_chat.answer_coordinates(
        10.77, 106.7, "Chợ Bến Thành (10.770, 106.700)", today=dt.date(2026, 10, 26)
    )
    assert seen == {
        "lat": 10.77,
        "lon": 106.7,
        "label": "Chợ Bến Thành (10.770, 106.700)",
        "days": 2,
    }
    assert text.startswith("🌤 Thời tiết Chợ Bến Thành")
    assert "Hôm nay 26/10: mưa rào, 25-32°C, khả năng mưa 80% ☔" in text
    assert "❌" in await weather_chat.answer_coordinates(200, 0, "x")


@pytest.mark.asyncio
async def test_get_weather_at_dung_chung_cache(monkeypatch):
    calls = []

    async def open_meteo(label, lat, lon, days):
        calls.append((lat, lon, days))
        return {"location": label, "source": "Open-Meteo", "now": {}, "daily": []}

    monkeypatch.setattr(weather, "_open_meteo", open_meteo)
    weather._forecast_cache.clear()
    first = await weather.get_weather_at(1.23456, 2.34567, "A", 2)
    second = await weather.get_weather_at(1.2346, 2.3457, "B", 2)
    assert calls == [(1.23456, 2.34567, 2)]
    assert first["location"] == "A" and second["location"] == "B"
    weather._forecast_cache.clear()


# ─── Handler vị trí + tin chưa hỗ trợ ────────────────────────────────────────


class _Message(SimpleNamespace):
    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def _update(user_id=42, **fields):
    message = _Message(replies=[], **fields)
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, username="u"),
        effective_chat=SimpleNamespace(id=user_id),
    )


async def _typing(**kwargs):
    return None


_CONTEXT = SimpleNamespace(bot=SimpleNamespace(send_chat_action=_typing))


@pytest.mark.asyncio
async def test_handler_vi_tri_luu_lich_su(monkeypatch):
    from handlers import unsupported

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    recorded, sent = [], []

    async def coords(lat, lon, label):
        return f"🌤 Thời tiết {label}"

    async def record(user_id, user_text, reply, kind="chat", **kwargs):
        recorded.append((user_text, kind))
        return True

    async def reply_long(message, text):
        sent.append(text)

    monkeypatch.setattr(unsupported.weather_chat, "answer_coordinates", coords)
    monkeypatch.setattr(unsupported.conversation_log, "record_exchange", record)
    monkeypatch.setattr(unsupported.common, "reply_long_text", reply_long)
    update = _update(
        location=SimpleNamespace(latitude=21.0285, longitude=105.8542),
        venue=SimpleNamespace(title="Hồ Gươm"),
    )
    await unsupported.location_msg(update, _CONTEXT)
    assert sent == ["🌤 Thời tiết Hồ Gươm (21.029, 105.854)"]
    assert recorded == [("[Gửi vị trí] Hồ Gươm (21.029, 105.854)", "weather")]


@pytest.mark.asyncio
async def test_tin_chua_ho_tro_tra_loi_giong_lan_anh_nguoi_la_im_lang(monkeypatch):
    from handlers import unsupported

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    sticker = _update(sticker=object())
    await unsupported.unsupported_msg(sticker, _CONTEXT)
    assert sticker.message.replies[0].startswith("😆 Sticker dễ thương")
    video = _update(sticker=None, video=object())
    await unsupported.unsupported_msg(video, _CONTEXT)
    assert "chưa xem được video" in video.message.replies[0]
    assert "tin thoại" in video.message.replies[0]
    stranger = _update(user_id=7, sticker=object())
    await unsupported.unsupported_msg(stranger, _CONTEXT)
    assert stranger.message.replies == []


def test_cau_chua_ho_tro_theo_cach_goi():
    text = media_text.unsupported_text("document", address_form="chị")
    assert "PDF, TXT/MD, Word" in text and text.endswith("nha chị.")
    assert "anh" not in media_text.unsupported_text("other")
