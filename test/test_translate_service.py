import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402
from services import translate_service  # noqa: E402


def test_parse_explicit_direction_recognizes_aliases():
    assert translate_service.parse_explicit_direction("ja>vi") == "ja_vi"
    assert translate_service.parse_explicit_direction("VI>JA") == "vi_ja"
    assert translate_service.parse_explicit_direction("jp-vn") == "ja_vi"
    assert translate_service.parse_explicit_direction("xin chào") is None


def test_detect_direction_japanese_text():
    assert translate_service.detect_direction("お世話になります。") == "ja_vi"


def test_detect_direction_vietnamese_text():
    assert translate_service.detect_direction("Xin chào anh, em đã hiểu rồi ạ") == "vi_ja"


def test_direction_label():
    assert translate_service.direction_label("ja_vi") == "Tiếng Nhật → Tiếng Việt"
    assert translate_service.direction_label("vi_ja") == "Tiếng Việt → Tiếng Nhật"


def test_reference_guide_missing_file_is_fail_open(monkeypatch, tmp_path):
    translate_service._reference_guide.cache_clear()
    monkeypatch.setattr(config, "TRANSLATE_REFERENCE_PATH", str(tmp_path / "khong-ton-tai.txt"))
    assert translate_service.reference_loaded() is False
    prompt = translate_service.build_translation_prompt("xin chào", "vi_ja")
    assert "TÀI LIỆU THAM CHIẾU" not in prompt
    translate_service._reference_guide.cache_clear()


def test_reference_guide_loaded_when_file_exists(monkeypatch, tmp_path):
    translate_service._reference_guide.cache_clear()
    ref_file = tmp_path / "reference.txt"
    ref_file.write_text("RC = Root Cause, giữ nguyên viết tắt.", encoding="utf-8")
    monkeypatch.setattr(config, "TRANSLATE_REFERENCE_PATH", str(ref_file))
    assert translate_service.reference_loaded() is True
    prompt = translate_service.build_translation_prompt("xin chào", "vi_ja")
    assert "RC = Root Cause" in prompt
    translate_service._reference_guide.cache_clear()


@pytest.mark.asyncio
async def test_translate_empty_text_raises_value_error():
    with pytest.raises(ValueError):
        await translate_service.translate("   ")


@pytest.mark.asyncio
async def test_translate_uses_orchestrator_ask_and_auto_detects_direction(monkeypatch):
    captured = {}

    class FakeResponse:
        text = "Hello, understood."
        used_fallback = False

    async def fake_ask(prompt, **kwargs):
        captured["prompt"] = prompt
        return FakeResponse()

    monkeypatch.setattr(translate_service.orchestrator, "ask", fake_ask)

    result, direction, response = await translate_service.translate("了解しました。")
    assert direction == "ja_vi"
    assert result == "Hello, understood."
    assert "了解しました" in captured["prompt"]
    assert response.used_fallback is False


@pytest.mark.asyncio
async def test_translate_respects_explicit_direction(monkeypatch):
    class FakeResponse:
        text = "了解しました"
        used_fallback = True

    async def fake_ask(prompt, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(translate_service.orchestrator, "ask", fake_ask)

    result, direction, response = await translate_service.translate(
        "đã hiểu rồi, cảm ơn nhé", direction="vi_ja"
    )
    assert direction == "vi_ja"
    assert result == "了解しました"
    assert response.used_fallback is True
