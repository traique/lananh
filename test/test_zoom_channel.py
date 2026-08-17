import hashlib
import hmac
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import zoom  # noqa: E402
from core import config, database as db  # noqa: E402


def test_verify_webhook_signature_ok(monkeypatch):
    monkeypatch.setattr(config, "ZOOM_SECRET_TOKEN", "secret123")
    body = b'{"event":"chat_message"}'
    ts = "1700000000"
    expected = "v0=" + hmac.new(
        b"secret123", f"v0:{ts}:{body.decode()}".encode(), hashlib.sha256
    ).hexdigest()
    assert zoom.verify_webhook_signature(expected, ts, body) is True


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
