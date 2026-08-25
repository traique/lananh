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

IDENTITY_LOCK_REFERENCE = (
    "Identity lock: the attached reference photo is the sole source of the subject's "
    "identity. Keep her exact facial geometry, facial proportions, skin tone and age "
    "presentation exactly as shown in that photo. Do not redesign, beautify, age or "
    "reinterpret the face, and do not smooth or airbrush the skin."
)

IDENTITY_LOCK_GIRL = (
    "Identity lock: every image shows the same adult Vietnamese woman in her "
    "early 20s, with an identical face in every generation - do not redesign, "
    "beautify, age or reinterpret it. She has a soft round-oval face with "
    "youthful fullness, full softly rounded cheeks, a smooth softly rounded "
    "jawline and a small rounded chin, large almond-shaped eyes with a subtly "
    "rounded appearance, natural double eyelids and natural eyelashes, "
    "medium-thin eyebrows with a soft gentle arch, a delicate straight nasal "
    "bridge with a small softly rounded nose tip, naturally full soft lips with "
    "a well-defined cupid's bow and a slightly fuller lower lip, and soft "
    "subtle cheekbones rather than pronounced bone structure. Her skin is a "
    "light fair-to-light-medium warm-neutral tone with authentic, natural skin "
    "texture: visible pores, subtle tonal variation and realistic specular "
    "highlights, never airbrushed or porcelain-smooth. This facial geometry and "
    "skin character never change; only her hairstyle, expression, makeup, "
    "clothing, accessories, pose, lighting, camera, environment and "
    "photographic finish are variable attributes, free to change from image to "
    "image."
)

# Photo mode (media_handler.py/channel_image_service.py): PHOTO_SUBJECT_RULE_GIRL
# below tells the model to restate the locked face to override what's visible in
# the source photo, so the phrase must actually carry that full description.
PHOTO_SUBJECT_PHRASE_GIRL = (
    "the same adult Vietnamese woman defined by the Identity Lock above, with a "
    "soft round-oval face carrying youthful, early-20s fullness, full softly "
    "rounded cheeks, a smooth softly rounded jawline, large almond-shaped eyes "
    "with a subtly rounded appearance, natural double eyelids, medium-thin "
    "eyebrows with a soft gentle arch, a delicate straight nasal bridge, a small "
    "softly rounded nose tip, naturally full soft lips with a well-defined "
    "cupid's bow and a slightly fuller lower lip, soft subtle cheekbones and a "
    "small rounded chin,"
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
    '2. CRITICAL - the face is FIXED by the Identity Lock above and has higher priority '
    'than every scene instruction. Restate the locked facial blueprint in the first '
    'sentence using the same geometry and proportions. Never describe or copy the face '
    'of the person in the source image. Use the source image only for pose, framing, '
    'outfit, accessories, setting, lighting, mood and photographic finish. Never let '
    'hairstyle, clothing, expression, camera angle or scene details alter the locked face.'
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
    "1. ALWAYS place the exact identity lock text provided above at the start of the "
    "prompt. Facial geometry and facial proportions have higher priority than hairstyle, "
    "expression, clothing, pose, lighting, camera or environment. Never allow variable "
    "scene details to rewrite the immutable face."
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
        return PromptIdentityConfig(
            mode="reference",
            identity_lock=IDENTITY_LOCK_REFERENCE,
            identity_rule=IDENTITY_RULE_LOCK,
            subject_phrase=SUBJECT_PHRASE_REFERENCE,
            subject_rule=rules["reference"],
            mode_hint="📎 Hãy đính kèm ảnh gốc cùng prompt này trên app Gemini.",
        )

    if any(keyword in normalized_description for keyword in GIRL_KEYWORDS):
        return PromptIdentityConfig(
            mode="girl",
            identity_lock=IDENTITY_LOCK_GIRL,
            identity_rule=IDENTITY_RULE_LOCK,
            subject_phrase=_GIRL_SUBJECT_PHRASES[context],
            subject_rule=rules["girl"],
            mode_hint="🔒 Prompt dùng khóa khuôn mặt cố định, không cần đính kèm ảnh.",
        )

    return PromptIdentityConfig(
        mode="described",
        identity_lock="",
        identity_rule=IDENTITY_RULE_NONE,
        subject_phrase=TEXT_SUBJECT_PHRASE_DESCRIBED,
        subject_rule=rules["described"],
        mode_hint="🖼️ Prompt tự mô tả khuôn mặt bằng chữ, không cần đính kèm ảnh.",
    )
