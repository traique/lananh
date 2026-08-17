import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.channel_chat_service import split_for_zalo


def test_split_for_zalo_keeps_short_message():
    assert split_for_zalo("xin chào", limit=20) == ["xin chào"]


def test_split_for_zalo_prefers_paragraph_boundaries():
    text = "Đoạn thứ nhất.\n\nĐoạn thứ hai dài hơn."
    chunks = split_for_zalo(text, limit=20)
    assert chunks == ["Đoạn thứ nhất.", "Đoạn thứ hai dài", "hơn."]
    assert " ".join(chunks).replace(". ", ". ")


def test_split_for_zalo_empty_message():
    assert split_for_zalo("   ") == []
