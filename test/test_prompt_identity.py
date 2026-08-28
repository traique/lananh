import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handlers.prompt_identity import (
    GIRL_KEYWORDS,
    KEEP_FACE_KEYWORDS,
    IDENTITY_LOCK_GIRL,
    PHOTO_SUBJECT_PHRASE_GIRL,
    TEXT_SUBJECT_PHRASE_GIRL,
    resolve_prompt_identity,
)


def test_girl_identity_matches_reference_facial_geometry():
    expected_features = (
        "softly rounded oval face",
        "slender, average-height build",
        "not a stocky or heavier-set figure",
        "almond-shaped eyes taper gently at the outer corner",
        "subtle double eyelid crease",
        "eyebrows are naturally full with only a slight arch",
        "straight, narrow nasal bridge",
        "small softly rounded tip",
        "slightly fuller lower lip",
        "small, softly rounded chin",
    )

    for feature in expected_features:
        assert feature in IDENTITY_LOCK_GIRL


def test_photo_girl_phrase_does_not_duplicate_identity_lock():
    """PHOTO_SUBJECT_RULE_GIRL must still override what's visible in the source
    photo, but it now does so by pointing back to the Identity Lock block
    already placed at the top of the output (rule 1), instead of restating the
    full facial blueprint a second time in the same prompt."""
    assert "identity lock" in PHOTO_SUBJECT_PHRASE_GIRL.lower()
    for cue in ("oval face", "medium-large eyes", "rounded chin", "nasal bridge"):
        assert cue not in PHOTO_SUBJECT_PHRASE_GIRL


def test_text_girl_phrase_does_not_duplicate_identity_lock():
    """/prompt (context="text") has no source photo to override, so the
    subject phrase must NOT restate the identity lock's face description -
    doing so just duplicated the same face twice in the same output."""
    assert "identity lock" in TEXT_SUBJECT_PHRASE_GIRL.lower()
    assert "heart-shaped" not in TEXT_SUBJECT_PHRASE_GIRL
    assert "almond-shaped" not in TEXT_SUBJECT_PHRASE_GIRL


def test_girl_identity_does_not_lock_styling():
    forbidden_styling = ("red ribbon", "ponytail", "white t-shirt")

    for styling in forbidden_styling:
        assert styling not in IDENTITY_LOCK_GIRL.lower()


def test_identity_priority_separates_immutable_face_from_scene_variables():
    identity = resolve_prompt_identity("cô gái 20 mặc áo dài")

    assert identity.mode == "girl"
    assert "facial geometry" in identity.identity_rule.lower()
    assert "hairstyle" in identity.identity_rule.lower()
    assert "clothing" in identity.identity_rule.lower()
    assert "scene" in identity.identity_rule.lower()


def test_reference_mode_wins_over_girl_mode():
    identity = resolve_prompt_identity("giữ mặt cô gái 20")

    assert identity.mode == "reference"
    assert identity.subject_phrase == "the subject from the attached reference image"


def test_keyword_routes_remain_stable():
    assert "giữ mặt" in KEEP_FACE_KEYWORDS
    assert "cô gái 20" in GIRL_KEYWORDS


def test_identity_lock_separates_skin_realism_from_scene_styling():
    assert "natural skin texture" in IDENTITY_LOCK_GIRL.lower()
    assert "photographic finish" in IDENTITY_LOCK_GIRL.lower()
    assert "variable attributes" in IDENTITY_LOCK_GIRL.lower()


def test_identity_lock_forbids_face_drift_from_variable_attributes():
    for attribute in ("hairstyle", "expression", "clothing", "pose", "camera", "lighting", "environment"):
        assert attribute in IDENTITY_LOCK_GIRL
