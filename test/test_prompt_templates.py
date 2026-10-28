import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _prompt_render import read_string_constant as _read_string_constant  # noqa: E402
from services.prompt_identity import resolve_prompt_identity  # noqa: E402


def test_text_prompt_template_has_identity_and_realism_controls():
    template = _read_string_constant("services/prompt_templates.py", "TEXT_PROMPT_INSTRUCTION_BASE")
    identity = resolve_prompt_identity("cô gái 20 đứng dưới mưa")
    rendered = template.format(
        identity_lock_block=f"{identity.identity_lock}\n\n",
        identity_rule=identity.identity_rule,
        subject_phrase=identity.subject_phrase,
        subject_rule=identity.subject_rule,
        user_desc="cô gái 20 đứng dưới mưa",
    )

    # Identity priority ở đây do {identity_rule} (IDENTITY_RULE_LOCK trong
    # services/prompt_identity.py) đảm nhiệm - không còn tiêu đề chữ hoa
    # "IDENTITY PRIORITY"/"REALISM CHECK" như bản cũ, khác với template ảnh
    # tham chiếu IMAGE_ANALYZE_INSTRUCTION_BASE (test riêng bên dưới).
    assert "higher priority than hairstyle" in rendered
    assert "ALWAYS place the exact lock text" in rendered
    assert "FRAMING IS MANDATORY" in rendered
    assert "POSE MUST BE GEOMETRICALLY PRECISE" in rendered
    assert "CAMERA AND LENS MUST MATCH THE SHOT" in rendered
    assert "Output ONLY" in rendered
    assert "{identity_lock_block}" not in rendered


def test_image_prompt_template_has_reference_analysis_controls():
    template = _read_string_constant(
        "services/prompt_templates.py", "IMAGE_ANALYZE_INSTRUCTION_BASE"
    )
    identity = resolve_prompt_identity("cô gái 20")
    rendered = template.format(
        identity_lock_block=f"{identity.identity_lock}\n\n",
        identity_rule=identity.identity_rule,
        subject_phrase=identity.subject_phrase,
        subject_rule=identity.subject_rule,
    )

    assert "SOURCE PRIORITY" in rendered
    assert "IDENTITY PRIORITY" in rendered
    assert "Do not invent hidden anatomy" in rendered
    assert "CAMERA AND LENS MUST BE INFERRED" in rendered
    assert "REALISM CHECK" in rendered
    assert "{subject_phrase}" not in rendered


# Prompt thật bot từng trả cho ảnh cô gái đạp xe dưới cây hoa giấy, caption "đính kèm mặt
# tôi": chép nguyên khuôn mẫu cũ, không tả gì của ảnh.
COPIED_PROMPT = (
    "Identity lock: the attached reference photo is the sole source of the subject's "
    "identity.\n\nPhotographic portrait of the subject from the attached reference image "
    "in a simple outdoor park setting. Vertical 9:16 portrait orientation, three-quarter "
    "body shot. Her hair follows the requested hairstyle naturally. She wears simple "
    "everyday clothing appropriate to the described setting, with realistic fabric texture."
)
GOOD_PROMPT = (
    "Photographic portrait of the subject from the attached reference image sitting on a "
    "white city bicycle on a paved garden path under a blooming magenta bougainvillea tree. "
    "Vertical 4:5 portrait orientation, three-quarter body shot from knee level upward. "
    "Both hands grip the mint-green handlebar grips at waist height; her long dark hair is "
    "tied in a high ponytail. She wears a white ruffled off-shoulder top and high-waisted "
    "light-blue wide-leg jeans; a plastic cup of iced matcha sits on the rear rack."
)


def test_bat_duoc_prompt_chep_khuon_mau():
    from services.prompt_templates import copies_template

    assert copies_template(COPIED_PROMPT)
    assert copies_template("Photographic portrait of her [doing what, where].")
    assert not copies_template(GOOD_PROMPT)


async def test_prompt_chep_khuon_mau_thi_hoi_lai_mot_lan(monkeypatch):
    from types import SimpleNamespace

    from services import photo_prompt
    from services.prompt_templates import TEMPLATE_RETRY_NOTE

    seen = []

    async def analyze(instruction, path):
        seen.append(instruction)
        return SimpleNamespace(text=COPIED_PROMPT if len(seen) == 1 else GOOD_PROMPT)

    monkeypatch.setattr(photo_prompt.orchestrator, "analyze_image", analyze)
    response = await photo_prompt.generate("PHÂN TÍCH ẢNH", "/x.jpg")
    assert response.text == GOOD_PROMPT
    assert seen == ["PHÂN TÍCH ẢNH", "PHÂN TÍCH ẢNH" + TEMPLATE_RETRY_NOTE]

    async def good(instruction, path):
        seen.append(instruction)
        return SimpleNamespace(text=GOOD_PROMPT)

    seen.clear()
    monkeypatch.setattr(photo_prompt.orchestrator, "analyze_image", good)
    response = await photo_prompt.generate("PHÂN TÍCH ẢNH", "/x.jpg")
    assert seen == ["PHÂN TÍCH ẢNH"] and response.text == GOOD_PROMPT  # tốt rồi: không hỏi lại
