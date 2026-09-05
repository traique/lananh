import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _prompt_render import read_string_constant as _read_string_constant  # noqa: E402
from handlers.prompt_identity import resolve_prompt_identity  # noqa: E402


def test_text_prompt_template_has_identity_and_realism_controls():
    template = _read_string_constant("handlers/commands.py", "TEXT_PROMPT_INSTRUCTION_BASE")
    identity = resolve_prompt_identity("cô gái 20 đứng dưới mưa")
    rendered = template.format(
        identity_lock_block=f"{identity.identity_lock}\n\n",
        identity_rule=identity.identity_rule,
        subject_phrase=identity.subject_phrase,
        subject_rule=identity.subject_rule,
        user_desc="cô gái 20 đứng dưới mưa",
    )

    # Identity priority ở đây do {identity_rule} (IDENTITY_RULE_LOCK trong
    # handlers/prompt_identity.py) đảm nhiệm - không còn tiêu đề chữ hoa
    # "IDENTITY PRIORITY"/"REALISM CHECK" như bản cũ, khác với template ảnh
    # tham chiếu ở media_handler.py (test riêng bên dưới).
    assert "higher priority than hairstyle" in rendered
    assert "ALWAYS place the exact lock text" in rendered
    assert "FRAMING IS MANDATORY" in rendered
    assert "POSE MUST BE GEOMETRICALLY PRECISE" in rendered
    assert "CAMERA AND LENS MUST MATCH THE SHOT" in rendered
    assert "Output ONLY" in rendered
    assert "{identity_lock_block}" not in rendered


def test_image_prompt_template_has_reference_analysis_controls():
    template = _read_string_constant("handlers/media_handler.py", "IMAGE_ANALYZE_INSTRUCTION_BASE")
    identity = resolve_prompt_identity("cô gái 20")
    rendered = template.format(
        identity_lock_block=f"{identity.identity_lock}\n\n",
        identity_rule=identity.identity_rule,
        subject_phrase=identity.subject_phrase,
        subject_rule=identity.subject_rule,
    )

    assert "REFERENCE PRIORITY" in rendered
    assert "IDENTITY PRIORITY" in rendered
    assert "Do not invent hidden anatomy" in rendered
    assert "CAMERA AND LENS MUST BE INFERRED" in rendered
    assert "REALISM CHECK" in rendered
    assert "{subject_phrase}" not in rendered
