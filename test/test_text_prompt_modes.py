"""Test cho 3 trường hợp của /prompt trong handlers/commands.py.

Đi qua đúng con đường code thật đi (resolve_prompt_identity + render_instruction)
thay vì tự dựng field bằng tay, để không lặp lại bug NameError từng lọt qua test
kiểu "gọi .format() thủ công" ở handlers/media_handler.py.

Giống nhánh ảnh, nhưng /prompt không hề có ảnh trong luồng, trừ khi user
chủ động đính kèm ảnh của mình trên app Gemini (trường hợp "mặt tôi").

Cả 3 trường hợp đều phải bắt buộc tả khung hình, dáng người, chất ảnh và
ống kính - thiếu chúng thì Gemini đẻ ra ảnh ngang, chủ thể bé tí ở giữa khung.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handlers.prompt_identity import (  # noqa: E402
    GIRL_KEYWORDS,
    IDENTITY_LOCK_GIRL,
    IDENTITY_LOCK_REFERENCE,
    KEEP_FACE_KEYWORDS,
    render_instruction,
    resolve_prompt_identity,
)

from _prompt_render import read_string_constant  # noqa: E402

TEXT_PROMPT_INSTRUCTION_BASE = read_string_constant("handlers/commands.py", "TEXT_PROMPT_INSTRUCTION_BASE")

DESC_DESCRIBED = "cô gái đứng trước nhà"
DESC_GIRL = "cô gái 20 đứng trước nhà"
DESC_REFERENCE = "giữ mặt tôi đứng trước nhà"


def _render(desc: str) -> str:
    identity = resolve_prompt_identity(desc)
    return render_instruction(TEXT_PROMPT_INSTRUCTION_BASE, identity, user_desc=desc)


def _render_described():
    return _render(DESC_DESCRIBED)


def _render_girl():
    return _render(DESC_GIRL)


def _render_reference():
    return _render(DESC_REFERENCE)


_ALL_MODES = (_render_described, _render_girl, _render_reference)


def test_ca_3_truong_hop_format_duoc():
    for render in _ALL_MODES:
        text = render()
        assert "{" not in text and "}" not in text


def test_du_11_rule():
    for render in _ALL_MODES:
        text = render()
        for n in range(1, 12):
            assert f"\n{n}. " in text, f"thiếu rule {n}"


def test_mo_ta_cua_user_duoc_chen_vao():
    assert "User's basic description: " + DESC_DESCRIBED in _render_described()
    assert "User's basic description: " + DESC_GIRL in _render_girl()
    assert "User's basic description: " + DESC_REFERENCE in _render_reference()


def test_khong_con_co_phap_midjourney_trong_vi_du():
    for render in _ALL_MODES:
        example = render().split("---")[1]
        assert "--ar" not in example
    assert "no tool-specific flags" in _render_described()


def test_vi_du_da_trung_tinh():
    example = _render_described().split("---")[1]
    for leak in ("at night", "drenched", "heavy rain", "wet asphalt", "low-light noise"):
        assert leak not in example, f"ví dụ còn rỉ chi tiết: {leak}"


def test_bat_buoc_ta_khung_hinh():
    for render in _ALL_MODES:
        text = render()
        assert "FRAMING IS MANDATORY" in text
        for need in (
            "vertical 9:16 portrait orientation",
            "horizontal 16:9 landscape orientation",
            "shot size",
            "camera height",
            "headroom",
        ):
            assert need in text, f"rule khung hình thiếu {need}"


def test_mac_dinh_anh_doc_khi_user_khong_noi_gi():
    text = _render_described()
    assert "choose a sensible framing" in text


def test_vi_du_co_san_cau_ta_khung_hinh():
    example = _render_described().split("---")[1]
    assert "Vertical 9:16 portrait orientation" in example
    assert "three-quarter body shot" in example
    assert "chest height" in example


def test_bat_buoc_ta_dang_nguoi_chi_tiet():
    for render in _ALL_MODES:
        text = render()
        assert "POSE MUST BE GEOMETRICALLY PRECISE" in text
        for need in ("elbow", "palm", "head tilt", "gaze", "hair"):
            assert need in text, f"rule dáng người thiếu {need}"


def test_vi_du_co_doan_ta_dang_nguoi():
    """Ví dụ trong template phải minh hoạ dáng đứng tả bằng hình học cụ thể
    (đúng tinh thần rule 5), không cần khớp chữ y hệt bản cũ."""
    example = _render_described().split("---")[1]
    assert "elbows barely bent" in example
    assert "hands at hip height" in example
    assert "weight rests on her left leg" in example


def test_chat_anh_bam_theo_yeu_cau_cua_user():
    for render in _ALL_MODES:
        text = render()
        assert "LIGHTING AND GRAIN MUST MATCH THE SCENE" in text
        assert "THE FINISH MUST MATCH THE LOOK THE USER ASKS FOR" in text
        assert "you MUST NOT write" in text


def test_khong_con_ep_cung_visible_pores():
    for render in _ALL_MODES:
        assert "MANDATORY WORDS" not in render()


def test_ong_kinh_theo_cu_anh_khong_chep_vi_du():
    for render in _ALL_MODES:
        text = render()
        assert "CAMERA AND LENS MUST MATCH" in text or "CAMERA AND LENS MUST BE INFERRED" in text
        assert "70-135mm" in text
        assert "40-55mm" in text
        assert "24-35mm" in text


def test_canh_bao_khong_tu_bia_camera_model():
    for render in _ALL_MODES:
        assert "Do not invent a camera brand or exact model" in render()


def test_th1_khong_co_dong_identity_lock():
    lines = _render_described().splitlines()
    assert not any(line.startswith("[Identity Lock") for line in lines)
    assert not any(line.startswith("[IDENTITY LOCK") for line in lines)


def test_th1_cam_tro_toi_anh_vi_khong_he_co_anh():
    rule = resolve_prompt_identity(DESC_DESCRIBED).subject_rule
    assert "PLAIN TEXT with NO image attached" in rule
    assert "photo of the subject from" not in _render_described()


def test_th1_bat_ta_du_dac_diem_khuon_mat():
    rule = resolve_prompt_identity(DESC_DESCRIBED).subject_rule.lower()
    for feature in (
        "age", "face shape", "eye geometry", "eyebrow", "nose", "lip",
        "jawline", "skin tone", "hair colour",
    ):
        assert feature in rule, f"rule thiếu yêu cầu tả {feature}"


def test_th1_van_cho_phep_canh_khong_co_nguoi():
    assert "when a detail is unclear" in resolve_prompt_identity(DESC_DESCRIBED).subject_rule


def test_th2_giu_nguyen_khoi_lock():
    assert IDENTITY_LOCK_GIRL in _render_girl()


def test_th2_khong_ta_lai_khuon_mat_da_khoa_thanh_chu():
    """/prompt (context="text") không có ảnh nguồn để ghi đè, nên câu tả chủ
    thể KHÔNG cần lặp lại mô tả khuôn mặt của identity lock - việc gọi thiếu
    context="text" trước đây khiến test này vô tình kiểm tra nhánh photo."""
    identity = resolve_prompt_identity(DESC_GIRL, context="text")
    phrase = identity.subject_phrase.lower()
    assert "identity lock" in phrase
    for feature in ("oval face", "medium-large eyes", "double eyelid crease", "nasal bridge", "rounded chin"):
        assert feature not in phrase, f"câu tả chủ thể lặp lại {feature}"
    assert "not" in identity.subject_rule.lower() and "redescribe" in identity.subject_rule.lower()


def test_th2_khong_cho_mo_ta_user_ghi_de_khuon_mat():
    assert "Never let hairstyle, clothing, expression" in resolve_prompt_identity(DESC_GIRL).subject_rule


def test_th3_giu_lock_reference():
    assert IDENTITY_LOCK_REFERENCE in _render_reference()


def test_th3_noi_ro_la_anh_dinh_kem():
    assert "the subject from the attached reference image" in _render_reference()
    assert "must match the attached photo exactly" in resolve_prompt_identity(DESC_REFERENCE).subject_rule


def test_th3_cam_bia_dac_diem_khuon_mat():
    rule = resolve_prompt_identity(DESC_REFERENCE).subject_rule
    assert "DO NOT invent concrete facial features" in rule
    assert "eye colour" in rule and "face shape" in rule


def test_ba_truong_hop_cho_ra_ba_prompt_khac_nhau():
    assert len({render() for render in _ALL_MODES}) == 3


def _route(desc: str) -> str:
    low = desc.lower()
    if any(kw in low for kw in KEEP_FACE_KEYWORDS):
        return "reference"
    if any(kw in low for kw in GIRL_KEYWORDS):
        return "girl"
    return "described"


def test_dinh_tuyen_theo_tu_khoa():
    assert _route("cô gái đứng trước nhà") == "described"
    assert _route("phong cảnh biển lúc hoàng hôn") == "described"
    assert _route("giữ mặt tôi nha") == "reference"
    assert _route("MẶT ANH đứng ở quán cafe") == "reference"
    assert _route("cô gái 20 đứng trước nhà") == "girl"
    assert _route("gái 20 tuổi mặc áo dài") == "girl"


def test_giu_mat_uu_tien_hon_co_gai_20():
    assert _route("giữ mặt cô gái 20") == "reference"
