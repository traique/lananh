import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels.contracts import ZaloGroupMessageRequest, ZaloMessageRequest  # noqa: E402
from core.text_normalize import nfc  # noqa: E402

# "ở" gõ dưới dạng NFD: o + U+031B (móc/horn) + U+0309 (hook above) - mô
# phỏng text vào từ bàn phím/macOS hoặc copy-paste PDF.
_NFD_SAMPLE = "Ph\u006f\u031b\u0309 b\u00f2 t\u00e1i"  # "Phở bò tái" ở dạng NFD
_NFC_SAMPLE = unicodedata.normalize("NFC", _NFD_SAMPLE)


def test_nfc_normalizes_nfd_input():
    assert unicodedata.is_normalized("NFC", _NFD_SAMPLE) is False
    result = nfc(_NFD_SAMPLE)
    assert result == _NFC_SAMPLE
    assert unicodedata.is_normalized("NFC", result) is True


def test_nfc_handles_none_and_empty():
    assert nfc(None) == ""
    assert nfc("") == ""


def test_nfc_idempotent_on_already_nfc_text():
    assert nfc(_NFC_SAMPLE) == _NFC_SAMPLE


def test_nfc_does_not_strip_diacritics():
    # Không được ASCII-hoá - chỉ đổi cách biểu diễn Unicode, không đổi nghĩa.
    assert "ở" in nfc(_NFD_SAMPLE)
    assert "Phở bò tái" == nfc(_NFD_SAMPLE)


def test_zalo_message_request_normalizes_text_to_nfc():
    payload = ZaloMessageRequest(
        account_id="acc1",
        sender_id="u1",
        sender_name="Nguyen Van A",
        conversation_id="c1",
        message_id="m1",
        text=_NFD_SAMPLE,
    )
    assert payload.text == _NFC_SAMPLE
    assert unicodedata.is_normalized("NFC", payload.text)


def test_zalo_group_message_request_normalizes_text_to_nfc():
    payload = ZaloGroupMessageRequest(
        account_id="acc1",
        group_id="g1",
        message_id="m1",
        sender_id="u1",
        sender_name="",
        text=_NFD_SAMPLE,
        sent_at_ms=1,
    )
    assert payload.text == _NFC_SAMPLE
