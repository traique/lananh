"""Shared identity configuration for text-to-image prompt generation."""

from dataclasses import dataclass
from typing import Literal

PromptIdentityMode = Literal["reference", "girl", "described"]

KEEP_FACE_KEYWORDS: tuple[str, ...] = (
    "giữ mặt",
    "giữ khuôn mặt",
    "mặt tôi",
    "mặt anh",
    "mặt em",
)
GIRL_KEYWORDS: tuple[str, ...] = ("cô gái 20", "gái 20")

# Khóa phụ: cộng dồn vào identity_lock bất kể đang ở mode mặt nào (reference/
# girl/described), vì "giữ trang phục"/"giữ sản phẩm" là một trục độc lập với
# việc chọn khóa mặt.
OUTFIT_KEYWORDS: tuple[str, ...] = ("giữ trang phục", "giữ đồ", "giữ quần áo", "giữ outfit")
PRODUCT_KEYWORDS: tuple[str, ...] = ("giữ sản phẩm", "giữ logo", "giữ bao bì", "giữ nhãn")

OUTFIT_LOCK = (
    "Outfit lock: keep the exact clothing style, cut, colour, material, pattern and "
    "accessories visible in the reference unchanged; only pose, setting and lighting "
    "may vary."
)

PRODUCT_LOCK = (
    "Product lock: keep the exact product shape, proportions, colour, material, "
    "packaging, logo, label, printed text and quantity visible in the reference "
    "unchanged; never invent or alter any product detail."
)

IDENTITY_LOCK_REFERENCE = (
    "Identity lock: the attached reference photo is the sole source of the subject's "
    "identity. Keep her exact facial geometry, facial proportions, skin tone and age "
    "presentation exactly as shown in that photo. Do not redesign, beautify, age or "
    "reinterpret the face, and do not smooth or airbrush the skin."
)

IDENTITY_LOCK_GIRL = (
    "Identity lock: every image shows the same adult Vietnamese woman, with an "
    "identical face and body build in every generation - do not redesign, "
    "beautify, age, slim down or add extra weight to her. She has a slender, "
    "average-height build with narrow shoulders and a slim waist and arms - not "
    "a stocky or heavier-set figure. She has a softly rounded oval face with "
    "moderate, naturally soft cheek definition - neither full or chubby nor "
    "sharply angular - tapering to a small, softly rounded chin and a "
    "well-defined, gently narrow jawline. Her medium almond-shaped eyes taper "
    "gently at the outer corner, with a subtle double eyelid crease sitting close "
    "to the lash line, warm brown irises and natural eyelashes, giving a warm "
    "gentle gaze rather than a wide-open doe-eyed look. Her eyebrows are "
    "naturally full with only a slight arch, following a fairly straight line "
    "set at a natural distance above the eyes - not thin or high-arched. "
    "She has a straight, narrow nasal bridge with a small softly rounded tip, "
    "and medium-full lips with a clearly defined cupid's bow, a slightly fuller "
    "lower lip and a natural rosy-mauve colour. Her skin is fair with a warm "
    "undertone and a smooth, luminous, dewy quality, rendered with authentic, "
    "natural skin texture: visible pores, subtle tonal variation and realistic "
    "specular highlights, never airbrushed or porcelain-smooth. This facial "
    "geometry, body build and skin character never change; only her hairstyle, "
    "expression, makeup, clothing, accessories, pose, lighting, camera, "
    "environment and photographic finish are variable attributes, free to "
    "change from image to image."
)

# Photo mode (media_handler.py/channel_image_service.py): the source photo shows a
# different face, so the rule must still override it - but it now only POINTS BACK
# to the Identity Lock block (already placed at the top of the output by rule 1)
# instead of repeating the full facial blueprint a second time, which used to
# generate the same ~70-word description twice in one prompt.
PHOTO_SUBJECT_PHRASE_GIRL = (
    "the same woman defined by the Identity Lock above, not the face visible in "
    "the source photo,"
)

# Text mode (/prompt): nothing to override, so no need to restate the face here -
# the identity lock block already sits right above with the full description.
TEXT_SUBJECT_PHRASE_GIRL = "the same woman described by the identity lock above,"

SUBJECT_PHRASE_REFERENCE = "the subject from the attached reference image"

TEXT_SUBJECT_PHRASE_DESCRIBED = (
    "a woman in her early 20s with long dark hair, a soft oval face, large natural "
    "almond-shaped eyes, medium-thin natural eyebrows, a delicate straight nose, "
    "soft medium-full lips and naturally textured fair skin,"
)

PHOTO_SUBJECT_RULE_DESCRIBED = (
    '2. CRITICAL - this prompt will be pasted as PLAIN TEXT with NO image attached. '
    'Therefore never point to a reference image. Instead describe the actual person '
    'visible in the image as a complete written identity blueprint: approximate age, '
    'ethnicity or facial character, face shape, eye geometry and eye colour when '
    'visually supported, eyelid structure, eyebrow shape, nose geometry, lip geometry, '
    'cheek structure, jawline, chin, skin tone and natural skin character, plus hair '
    'colour, length, texture and parting. Only state details supported by the image; '
    'when a detail is unclear, use a neutral description rather than inventing a precise '
    'measurement or feature. If no person is visible, skip the face description entirely.'
)

PHOTO_SUBJECT_RULE_GIRL = (
    '2. CRITICAL - the face is FIXED by the Identity Lock text already placed at the '
    'start of the prompt (rule 1) and has higher priority than every scene instruction. '
    'Name the subject only as "the same woman defined by the Identity Lock above" - do '
    'NOT restate the facial blueprint again elsewhere in the prompt, that would just '
    'duplicate the Identity Lock paragraph. Never describe or copy the face of the '
    'person in the source image. Use the source image only for pose, framing, outfit, '
    'accessories, setting, lighting, mood and photographic finish. Never let hairstyle, '
    'clothing, expression, camera angle or scene details alter the locked face.'
)

PHOTO_SUBJECT_RULE_REFERENCE = (
    '2. CRITICAL - the user will attach the reference photo together with this prompt, '
    'so that attachment is the sole identity source. Refer to the person as "the subject '
    'from the attached reference image" and state that the generated face must match the '
    'attached photo exactly. DO NOT invent concrete facial features such as eye colour, '
    'face shape, nose or lip shape when they are supplied by the attachment. Describe pose, '
    'expression, outfit, setting, lighting and camera only when they are not already '
    'determined by the reference.'
)

# Text-mode rules (used by /prompt: there is no source photo, so unlike the PHOTO_*
# rules above there is nothing to override - restating the locked face in "girl"
# mode would just duplicate the identity lock block that already sits right above it.
TEXT_SUBJECT_RULE_DESCRIBED = (
    '2. CRITICAL - this prompt will be pasted as plain text with no image attached. '
    'Never point to a reference image. If the user\'s description involves a person, '
    'fully specify that person in words inside the first sentence as a complete '
    'written identity blueprint: approximate age, ethnicity or facial character, '
    'face shape, eye shape and colour, eyebrow shape, nose shape, lip shape, jawline '
    'and chin, skin tone and texture, and hair colour, length, texture and parting - '
    'inventing plausible details that fit the description. If the description '
    'contains no person, skip the face description entirely.'
)

TEXT_SUBJECT_RULE_GIRL = (
    "2. CRITICAL - the face is fixed by the identity lock above and has higher "
    "priority than every scene instruction. Do NOT redescribe her facial features "
    "again - refer to her only as \"the same woman described by the identity lock "
    "above\" and take the pose, outfit, setting and mood entirely from the user's "
    "description. Never let scene details alter the locked face."
)

TEXT_SUBJECT_RULE_REFERENCE = PHOTO_SUBJECT_RULE_REFERENCE

IDENTITY_RULE_LOCK = (
    "1. ALWAYS place the exact lock text provided above at the start of the prompt, "
    "exactly as given, with nothing paraphrased or repeated later. When a facial "
    "identity lock is present, facial geometry and facial proportions have higher "
    "priority than hairstyle, expression, clothing, pose, lighting, camera or "
    "environment. When an outfit or product lock is present, its locked details carry "
    "the same override priority. Never allow variable scene details to rewrite a "
    "locked element."
)

IDENTITY_RULE_NONE = (
    '1. DO NOT include an "[Identity Lock: ...]" line. Start the prompt directly with '
    'the scene description and make the visible subject self-contained in written form.'
)


@dataclass(frozen=True)
class PromptIdentityConfig:
    mode: PromptIdentityMode
    identity_lock: str
    identity_rule: str
    subject_phrase: str
    subject_rule: str
    mode_hint: str


def render_instruction(template: str, identity: PromptIdentityConfig, **extra_fields: str) -> str:
    """Fill a prompt template with one resolved identity, in one place.

    Centralizing this stops the 3 call sites (commands.py, media_handler.py,
    channel_image_service.py) from hand-copying the same 4 .format() kwargs,
    which is how a NameError shipped in media_handler.py unnoticed.
    """
    identity_lock_block = f"{identity.identity_lock}\n\n" if identity.identity_lock else ""
    return template.format(
        identity_lock_block=identity_lock_block,
        identity_rule=identity.identity_rule,
        subject_phrase=identity.subject_phrase,
        subject_rule=identity.subject_rule,
        **extra_fields,
    )


PromptIdentityContext = Literal["photo", "text"]

_SUBJECT_RULES: dict[PromptIdentityContext, dict[PromptIdentityMode, str]] = {
    "photo": {
        "reference": PHOTO_SUBJECT_RULE_REFERENCE,
        "girl": PHOTO_SUBJECT_RULE_GIRL,
        "described": PHOTO_SUBJECT_RULE_DESCRIBED,
    },
    "text": {
        "reference": TEXT_SUBJECT_RULE_REFERENCE,
        "girl": TEXT_SUBJECT_RULE_GIRL,
        "described": TEXT_SUBJECT_RULE_DESCRIBED,
    },
}

# Only "girl" mode's phrase differs by context (see PHOTO_SUBJECT_PHRASE_GIRL /
# TEXT_SUBJECT_PHRASE_GIRL above); "reference" and "described" phrases are the
# same invented-or-attached wording regardless of where they're rendered.
_GIRL_SUBJECT_PHRASES: dict[PromptIdentityContext, str] = {
    "photo": PHOTO_SUBJECT_PHRASE_GIRL,
    "text": TEXT_SUBJECT_PHRASE_GIRL,
}


def resolve_prompt_identity(
    description: str, context: PromptIdentityContext = "photo"
) -> PromptIdentityConfig:
    """Resolve which identity applies to `description`.

    `context` picks the matching subject_rule wording: "photo" (default) is for
    call sites that analyze an attached image (media_handler.py,
    channel_image_service.py); "text" is for /prompt, which has no source image
    and must not tell the model to look at or restate anything from one.
    """
    normalized_description = description.lower()
    rules = _SUBJECT_RULES[context]

    if any(keyword in normalized_description for keyword in KEEP_FACE_KEYWORDS):
        mode: PromptIdentityMode = "reference"
        face_lock = IDENTITY_LOCK_REFERENCE
        subject_phrase = SUBJECT_PHRASE_REFERENCE
        subject_rule = rules["reference"]
        mode_hint = "📎 Hãy đính kèm ảnh gốc cùng prompt này trên app Gemini."
    elif any(keyword in normalized_description for keyword in GIRL_KEYWORDS):
        mode = "girl"
        face_lock = IDENTITY_LOCK_GIRL
        subject_phrase = _GIRL_SUBJECT_PHRASES[context]
        subject_rule = rules["girl"]
        mode_hint = "🔒 Prompt dùng khóa khuôn mặt cố định, không cần đính kèm ảnh."
    else:
        mode = "described"
        face_lock = ""
        subject_phrase = TEXT_SUBJECT_PHRASE_DESCRIBED
        subject_rule = rules["described"]
        mode_hint = "🖼️ Prompt tự mô tả khuôn mặt bằng chữ, không cần đính kèm ảnh."

    matched_extras = [
        (name, lock)
        for name, keywords, lock in (
            ("trang phục", OUTFIT_KEYWORDS, OUTFIT_LOCK),
            ("sản phẩm", PRODUCT_KEYWORDS, PRODUCT_LOCK),
        )
        if any(keyword in normalized_description for keyword in keywords)
    ]
    if matched_extras:
        mode_hint += f" (khóa thêm: {', '.join(name for name, _ in matched_extras)})"
    identity_lock = "\n\n".join(lock for lock in (face_lock, *(l for _, l in matched_extras)) if lock)

    return PromptIdentityConfig(
        mode=mode,
        identity_lock=identity_lock,
        identity_rule=IDENTITY_RULE_LOCK if identity_lock else IDENTITY_RULE_NONE,
        subject_phrase=subject_phrase,
        subject_rule=subject_rule,
        mode_hint=mode_hint,
    )
