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
    "[Identity Lock: The attached reference image is the sole identity source. "
    "Preserve the person's exact facial geometry, facial proportions, skin tone, "
    "age presentation and natural skin character. Do not redesign, beautify, age, "
    "or reinterpret the face. DO NOT smooth or airbrush the face.]"
)

IDENTITY_LOCK_GIRL = """[IDENTITY LOCK — FACIAL IDENTITY HAS HIGHEST PRIORITY
The subject is the same adult Vietnamese woman in every generation. Preserve the same facial geometry and proportions across every image. Do not redesign, beautify, age, or reinterpret her face.

Face shape: soft oval-to-heart-shaped face, slightly wider upper face tapering gradually toward a small rounded chin. Smooth tapered jawline with no sharp angularity.
Eyes: large almond-shaped eyes with a subtly rounded appearance, natural double eyelids, balanced eye spacing and natural eyelashes. Keep the eyes proportionally large without making them anime-like or exaggerated.
Eyebrows: medium-thin mostly straight eyebrows, naturally shaped, with a very subtle arch toward the outer ends.
Nose: delicate straight nasal bridge, relatively narrow nose, small softly rounded nose tip and compact nasal wings.
Lips: naturally soft medium-full lips, slightly fuller lower lip, soft cupid's bow and moderate mouth width.
Cheeks: softly rounded cheeks with subtle natural volume and moderate cheekbones.
Chin and jaw: small rounded chin and smooth tapered jawline.
Facial proportions: harmonious spacing between eyes, nose and lips; delicate nose and mouth relative to the eyes; stable overall facial silhouette.
Skin: light fair-to-light-medium warm-neutral skin tone with authentic natural skin texture, fine pores, subtle tonal variation and realistic specular highlights.

IMMUTABLE IDENTITY: facial geometry, facial proportions, eye geometry, eyelid structure, eyebrow shape, nose geometry, lip geometry, cheek structure, jawline and chin shape.
VARIABLE ATTRIBUTES: hairstyle, hair arrangement, expression, makeup, clothing, accessories, pose, lighting, camera, environment and photographic finish. Changing these must never change the facial identity.]"""

TEXT_SUBJECT_PHRASE_GIRL = (
    "the same adult Vietnamese woman defined by the Identity Lock above, with a "
    "soft oval-to-heart-shaped face, a smooth tapered jawline, large almond-shaped "
    "eyes with a subtly rounded appearance, natural double eyelids, medium-thin "
    "mostly straight eyebrows, a delicate straight nasal bridge, a small softly "
    "rounded nose tip, naturally soft medium-full lips with a slightly fuller lower "
    "lip, softly rounded cheeks and a small rounded chin,"
)

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


def resolve_prompt_identity(description: str) -> PromptIdentityConfig:
    normalized_description = description.lower()

    if any(keyword in normalized_description for keyword in KEEP_FACE_KEYWORDS):
        return PromptIdentityConfig(
            mode="reference",
            identity_lock=IDENTITY_LOCK_REFERENCE,
            identity_rule=IDENTITY_RULE_LOCK,
            subject_phrase=SUBJECT_PHRASE_REFERENCE,
            subject_rule=PHOTO_SUBJECT_RULE_REFERENCE,
            mode_hint="📎 Hãy đính kèm ảnh gốc cùng prompt này trên app Gemini.",
        )

    if any(keyword in normalized_description for keyword in GIRL_KEYWORDS):
        return PromptIdentityConfig(
            mode="girl",
            identity_lock=IDENTITY_LOCK_GIRL,
            identity_rule=IDENTITY_RULE_LOCK,
            subject_phrase=TEXT_SUBJECT_PHRASE_GIRL,
            subject_rule=PHOTO_SUBJECT_RULE_GIRL,
            mode_hint="🔒 Prompt dùng khóa khuôn mặt cố định, không cần đính kèm ảnh.",
        )

    return PromptIdentityConfig(
        mode="described",
        identity_lock="",
        identity_rule=IDENTITY_RULE_NONE,
        subject_phrase=TEXT_SUBJECT_PHRASE_DESCRIBED,
        subject_rule=PHOTO_SUBJECT_RULE_DESCRIBED,
        mode_hint="🖼️ Prompt tự mô tả khuôn mặt bằng chữ, không cần đính kèm ảnh.",
    )
