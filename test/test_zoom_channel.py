import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import zoom  # noqa: E402
from core import config, database as db  # noqa: E402


def _sign(ts: str, body: bytes, secret: bytes = b"secret123") -> str:
    return "v0=" + hmac.new(secret, f"v0:{ts}:{body.decode()}".encode(), hashlib.sha256).hexdigest()


def test_verify_webhook_signature_ok(monkeypatch):
    monkeypatch.setattr(config, "ZOOM_SECRET_TOKEN", "secret123")
    body = b'{"event":"chat_message"}'
    ts = str(int(time.time()))
    assert zoom.verify_webhook_signature(_sign(ts, body), ts, body) is True


def test_verify_webhook_signature_utf8_body(monkeypatch):
    monkeypatch.setattr(config, "ZOOM_SECRET_TOKEN", "secret123")
    body = '{"cmd":"chào em"}'.encode()
    ts = str(int(time.time()))
    assert zoom.verify_webhook_signature(_sign(ts, body), ts, body) is True
    assert zoom.verify_webhook_signature(_sign(ts, body), ts, b"\xff\xfe") is False


@pytest.mark.parametrize("offset", [-301, 301, -3600, 86400])
def test_verify_webhook_signature_rejects_stale_or_future_timestamp(monkeypatch, offset):
    # Chữ ký đúng nhưng timestamp lệch > 5 phút => replay, phải từ chối.
    monkeypatch.setattr(config, "ZOOM_SECRET_TOKEN", "secret123")
    body = b'{"event":"chat_message"}'
    now = 1_800_000_000
    ts = str(now + offset)
    assert zoom.verify_webhook_signature(_sign(ts, body), ts, body, now=now) is False


def test_verify_webhook_signature_accepts_small_skew_and_milliseconds(monkeypatch):
    monkeypatch.setattr(config, "ZOOM_SECRET_TOKEN", "secret123")
    body = b"{}"
    now = 1_800_000_000
    for ts in (str(now - 299), str(now + 299), str(now * 1000 + 1234)):
        assert zoom.verify_webhook_signature(_sign(ts, body), ts, body, now=now) is True
    stale_ms = str((now - 600) * 1000)
    assert zoom.verify_webhook_signature(_sign(stale_ms, body), stale_ms, body, now=now) is False


@pytest.mark.parametrize(
    "ts", ["", "abc", "-1700000000", "1.7e9", " 1800000000", "9" * 5000, "١٢٣"]
)
def test_verify_webhook_signature_rejects_malformed_timestamp(monkeypatch, ts):
    monkeypatch.setattr(config, "ZOOM_SECRET_TOKEN", "secret123")
    body = b"{}"
    signature = (
        "v0="
        + hmac.new(b"secret123", b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    )
    assert zoom.verify_webhook_signature(signature, ts, body, now=1_800_000_000) is False


async def test_zoom_webhook_rejects_replayed_request_but_keeps_url_validation(monkeypatch):
    import web

    monkeypatch.setattr(web.config, "ZOOM_ENABLED", True)
    monkeypatch.setattr(web.config, "ZOOM_SECRET_TOKEN", "secret123")
    enqueue = AsyncMock()
    monkeypatch.setattr(web.webhook_inbox, "enqueue", enqueue)

    def request(payload: dict, headers: dict) -> SimpleNamespace:
        raw = json.dumps(payload).encode()
        return SimpleNamespace(
            headers=headers, body=AsyncMock(return_value=raw), json=AsyncMock(return_value=payload)
        )

    payload = {
        "event": "bot_notification",
        "payload": {"messageId": "m1", "userJid": "u", "cmd": "hi"},
    }
    raw = json.dumps(payload).encode()
    old_ts = str(int(time.time()) - 3600)
    replay = request(
        payload, {"x-zm-signature": _sign(old_ts, raw), "x-zm-request-timestamp": old_ts}
    )
    assert (await web.zoom_webhook(replay)).status_code == 403
    enqueue.assert_not_awaited()

    fresh_ts = str(int(time.time()))
    fresh = request(
        payload, {"x-zm-signature": _sign(fresh_ts, raw), "x-zm-request-timestamp": fresh_ts}
    )
    assert (await web.zoom_webhook(fresh)).status_code == 200
    enqueue.assert_awaited_once()

    # Bước Validate trên Marketplace không có chữ ký/timestamp - vẫn chạy như cũ.
    validation = request({"event": "endpoint.url_validation", "payload": {"plainToken": "pt"}}, {})
    result = await web.zoom_webhook(validation)
    assert result["plainToken"] == "pt"
    assert result["encryptedToken"] == hmac.new(b"secret123", b"pt", hashlib.sha256).hexdigest()


def test_verify_webhook_signature_wrong(monkeypatch):
    monkeypatch.setattr(config, "ZOOM_SECRET_TOKEN", "secret123")
    assert zoom.verify_webhook_signature("v0=bad", "123", b"{}") is False


def test_verify_webhook_signature_no_token(monkeypatch):
    monkeypatch.setattr(config, "ZOOM_SECRET_TOKEN", "")
    assert zoom.verify_webhook_signature("v0=x", "123", b"{}") is False


def test_verify_webhook_token_legacy(monkeypatch):
    monkeypatch.setattr(config, "ZOOM_VERIFICATION_TOKEN", "abc")
    assert zoom.verify_webhook_token("abc") is True
    assert zoom.verify_webhook_token("wrong") is False


def test_build_url_validation_response(monkeypatch):
    monkeypatch.setattr(config, "ZOOM_SECRET_TOKEN", "secret123")
    result = zoom.build_url_validation_response("plain-token-xyz")
    assert result["plainToken"] == "plain-token-xyz"
    expected = hmac.new(b"secret123", b"plain-token-xyz", hashlib.sha256).hexdigest()
    assert result["encryptedToken"] == expected


def test_split_message_short_text_no_split():
    assert zoom._split_message("hello", 100) == ["hello"]


def test_split_message_splits_long_text():
    text = "a" * 50 + "\n\n" + "b" * 50
    chunks = zoom._split_message(text, 60)
    assert len(chunks) >= 2
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_to_zoom_markdown_converts_header_and_bold():
    converted = zoom._to_zoom_markdown("## Tiêu đề\n**đậm** và ~~gạch~~")
    assert converted == "*Tiêu đề*\n*đậm* và ~gạch~"


def test_to_zoom_markdown_converts_table_row():
    converted = zoom._to_zoom_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
    assert converted == "• A — B\n• 1 — 2"


def test_parse_event_extracts_fields():
    payload = {
        "payload": {
            "userJid": "user@zoom",
            "toJid": "bot@zoom",
            "cmd": "xin chào",
            "messageId": "msg-1",
        }
    }
    event = zoom.parse_event(payload)
    assert event is not None
    assert event.sender_jid == "user@zoom"
    assert event.text == "xin chào"
    assert event.event_id == "msg-1"
    assert event.reply_jid == "user@zoom"  # 1:1, không phải channel


def test_parse_event_channel_reply_jid_is_to_jid():
    payload = {
        "payload": {
            "userJid": "user@zoom",
            "toJid": "channel@zoom",
            "channelName": "team-general",
            "cmd": "xin chào",
            "messageId": "msg-2",
        }
    }
    event = zoom.parse_event(payload)
    assert event is not None
    assert event.reply_jid == "channel@zoom"  # kênh nhóm -> trả lời về channel


def test_parse_event_missing_required_field_returns_none():
    assert zoom.parse_event({"payload": {"userJid": "u"}}) is None
    assert zoom.parse_event({}) is None


@pytest.mark.asyncio
async def test_zoom_pairing_round_trip(monkeypatch):
    store: dict[str, str] = {}

    async def fake_get_setting(key):
        return store.get(key)

    async def fake_set_setting(key, value):
        store[key] = value

    monkeypatch.setattr(db, "get_setting", fake_get_setting)
    monkeypatch.setattr(db, "set_setting", fake_set_setting)

    assert await db.zoom_get_pairing() is None

    await db.zoom_set_pairing("user@zoom", "Anh Tuấn")
    assert await db.zoom_get_pairing() == ("user@zoom", "Anh Tuấn")

    await db.zoom_clear_pairing()
    assert await db.zoom_get_pairing() is None
