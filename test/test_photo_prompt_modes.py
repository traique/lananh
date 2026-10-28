"""Test cho 3 dạng ảnh -> prompt (services/prompt_templates.py, dùng ở handlers/media_handler.py).

Đọc IMAGE_ANALYZE_INSTRUCTION_BASE bằng ast thay vì import module (xem _prompt_render.py).
Cách này cũng buộc test đi qua đúng resolve_prompt_identity + render_instruction
(con đường code thật dùng) thay vì tự .format() tay với field tự chọn.

Điểm mấu chốt: mỗi dạng lấy khuôn mặt từ một nguồn khác nhau, nên prompt
mẫu phải tả chủ thể theo một cách khác nhau:
  1. không caption -> tả người trong ảnh thành chữ
  2. cô gái 20     -> tả khuôn mặt trong khối lock thành chữ
  3. mặt tôi      -> không tả mặt, khoá theo ảnh user đính kèm

Ngoài khuôn mặt, cả 3 dạng đều phải bắt buộc tả khung hình, dáng người,
chất ảnh và ống kính theo ảnh gốc - thiếu chúng thì ảnh tạo ra sai dù mặt
đúng.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _prompt_render import ROOT, read_string_constant  # noqa: E402
from services.prompt_identity import (  # noqa: E402
    IDENTITY_LOCK_GIRL,
    IDENTITY_LOCK_REFERENCE,
    render_instruction,
    resolve_prompt_identity,
)

IMAGE_ANALYZE_INSTRUCTION_BASE = read_string_constant(
    "services/prompt_templates.py", "IMAGE_ANALYZE_INSTRUCTION_BASE"
)

CAPTION_DESCRIBED = ""
CAPTION_GIRL = "cô gái 20"
CAPTION_REFERENCE = "giữ mặt nha"


def _render(caption: str) -> str:
    identity = resolve_prompt_identity(caption)
    return render_instruction(IMAGE_ANALYZE_INSTRUCTION_BASE, identity)


def _render_described():
    return _render(CAPTION_DESCRIBED)


def _render_girl():
    return _render(CAPTION_GIRL)


def _render_reference():
    return _render(CAPTION_REFERENCE)


_ALL_MODES = (_render_described, _render_girl, _render_reference)


# ─── Regression: bug NameError từng lọt qua test ────
def test_photo_msg_khong_dung_bien_chua_gan_gia_tri():
    """subject_phrase/identity_rule/subject_rule từng được truyền vào .format()
    dưới dạng biến trần chưa hề gán trong photo_msg(), gây NameError bị except
    chung nuốt và hiện nhầm thành lỗi Gemini - tính năng ảnh->prompt trên
    Telegram chết hoàn toàn mà test cũ không phát hiện được vì nó tự dựng
    field bằng tay thay vì gọi qua render_instruction()."""
    handler = (ROOT / "handlers/media_handler.py").read_text()
    templates = (ROOT / "services/prompt_templates.py").read_text()
    for source in (handler, templates):
        assert "subject_phrase=subject_phrase" not in source
        assert "identity_rule=identity_rule" not in source
        assert "subject_rule=subject_rule" not in source
    assert "build_image_instruction(caption, identity)" in handler
    assert "render_instruction(IMAGE_ANALYZE_INSTRUCTION_BASE, identity)" in templates


# ─── Template chung ─────────────────────────────────
def test_ca_3_dang_deu_format_duoc_khong_thieu_placeholder():
    for render in _ALL_MODES:
        text = render()
        assert "{" not in text and "}" not in text


def test_du_12_rule():
    for render in _ALL_MODES:
        text = render()
        for n in range(1, 13):
            assert f"\n{n}. " in text, f"thiếu rule {n}"


def test_khong_con_co_phap_cua_tool_khac():
    """--ar 4:5 là cú pháp Midjourney, dán vào Gemini chỉ là rác chữ."""
    for render in _ALL_MODES:
        text = render()
        example = text.split("---")[1]
        assert "--ar" not in example
    assert "no tool-specific flags" in _render_described()


def test_vi_du_da_trung_tinh_khong_con_canh_mua_dem():
    example = _render_described().split("---")[1]
    for leak in ("at night", "drenched", "heavy rain", "wet asphalt", "low-light noise"):
        assert leak not in example, f"ví dụ còn rỉ chi tiết: {leak}"


# ─── Khung hình ─────────────────────────────────────
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


def test_vi_du_co_san_cau_ta_khung_hinh():
    """Các ô khung hình chỉ nói phải tả gì, không cho giá trị mẫu: bản trước có e.g.
    "three-quarter body shot framed from mid-thigh upward", "camera at chest height" và
    model chép y nguyên dù ảnh cắt tới bắp chân, thấy cả 2 bánh xe đạp."""
    example = _render_described().split("---")[1]
    for slot in ("[orientation", "[shot size", "[camera height and angle", "headroom"):
        assert slot in example
    for copied in ("mid-thigh upward", "chest height", "Vertical 9:16"):
        assert copied not in example


# ─── Dáng người ─────────────────────────────────────
def test_bat_buoc_ta_dang_nguoi_chi_tiet():
    for render in _ALL_MODES:
        text = render()
        assert "POSE MUST BE GEOMETRICALLY PRECISE" in text
        for need in ("elbow", "palm", "head tilt", "gaze", "hair"):
            assert need in text, f"rule dáng người thiếu {need}"


def test_vi_du_co_doan_ta_dang_nguoi():
    example = _render_described().split("---")[1]
    pose = next(line for line in example.splitlines() if line.startswith("[Pose:"))
    for need in ("elbow", "each hand", "palms", "weight", "head tilt", "gaze", "hair"):
        assert need in pose, f"ô dáng người thiếu {need}"


# ─── Chất ảnh ───────────────────────────────────────
def test_chat_anh_bam_theo_anh_goc():
    for render in _ALL_MODES:
        text = render()
        assert "LIGHTING AND FINISH MUST MATCH THE SOURCE PHOTO" in text
        assert "contradictory raw and beauty-filter" in text


def test_khong_con_ep_cung_visible_pores():
    for render in _ALL_MODES:
        text = render()
        assert "MANDATORY WORDS" not in text


# ─── Ống kính ───────────────────────────────────────
def test_ong_kinh_suy_tu_anh_goc():
    for render in _ALL_MODES:
        text = render()
        assert "CAMERA AND LENS MUST BE INFERRED FROM PERSPECTIVE" in text
        assert "70-135mm" in text
        assert "40-55mm" in text
        assert "24-35mm" in text


def test_canh_bao_khong_tu_bia_camera_model():
    for render in _ALL_MODES:
        assert "Do not invent a camera brand or exact model" in render()


# ─── Dạng 1: không caption ──────────────────────────
def test_dang_1_khong_co_dong_identity_lock():
    lines = _render_described().splitlines()
    assert not any(line.startswith("[Identity Lock") for line in lines)
    assert not any(line.startswith("[IDENTITY LOCK") for line in lines)


def test_dang_1_khong_tro_toi_anh_trong_vi_du():
    text = _render_described()
    assert "Photographic portrait of [the person in the source photo" in text
    assert "photo of the subject from" not in text


def test_dang_1_bat_buoc_ta_du_dac_diem_khuon_mat():
    rule = resolve_prompt_identity(CAPTION_DESCRIBED).subject_rule.lower()
    for feature in (
        "age",
        "face shape",
        "eye geometry",
        "eyebrow",
        "nose",
        "lip",
        "jawline",
        "skin tone",
        "hair colour",
    ):
        assert feature in rule, f"rule thiếu yêu cầu tả {feature}"


def test_dang_1_vi_du_chu_the_co_san_mo_ta_mat():
    # /prompt chữ: không có ảnh, ví dụ cho sẵn 1 khuôn mặt bịa để model biết mức chi tiết.
    phrase = resolve_prompt_identity(CAPTION_DESCRIBED, "text").subject_phrase.lower()
    for feature in ("face", "eyes", "eyebrows", "nose", "lips", "skin", "hair"):
        assert feature in phrase


def test_dang_1_anh_chu_the_la_o_phai_dien_khong_phai_mat_bia_san():
    """Có ảnh mẫu: viết sẵn "a woman in her early 20s with long dark hair..." thì model dễ
    chép nguyên khuôn mặt bịa đó (ảnh đàn ông vẫn ra "a woman in her early 20s")."""
    phrase = resolve_prompt_identity(CAPTION_DESCRIBED).subject_phrase
    assert phrase.startswith("[the person in the source photo") and "rule 2" in phrase
    assert "early 20s" not in _render_described().split("---")[1]


# ─── Dạng 2: cô gái 20 ──────────────────────────────
def test_dang_2_giu_nguyen_khoi_lock_co_dinh():
    assert IDENTITY_LOCK_GIRL in _render_girl()


def test_dang_2_khong_ta_lai_khuon_mat_da_khoa_thanh_chu():
    """Mặt đã có nguyên khối Identity Lock ở đầu prompt rồi, câu tả chủ thể chỉ
    cần trỏ lại khối đó, không lặp lại các đặc điểm khuôn mặt lần 2 - tránh
    trùng lặp ~70 từ trong cùng một prompt."""
    identity = resolve_prompt_identity(CAPTION_GIRL)
    phrase = identity.subject_phrase.lower()
    assert "identity lock" in phrase
    for feature in (
        "oval face",
        "medium-large eyes",
        "double eyelid crease",
        "nasal bridge",
        "rounded chin",
    ):
        assert feature not in phrase, f"câu tả chủ thể lặp lại {feature}"
    assert "not restate" in identity.subject_rule.lower()


def test_dang_2_khong_tro_toi_anh_dinh_kem():
    text = _render_girl()
    assert "the same woman defined by the Identity Lock above" in text
    assert "photo of the subject from" not in text


def test_dang_2_cam_ta_mat_nguoi_trong_anh():
    rule = resolve_prompt_identity(CAPTION_GIRL).subject_rule
    assert "Never describe or copy the face" in rule
    assert "pose" in rule and "outfit" in rule


# ─── Dạng 3: mặt tôi ────────────────────────────────
def test_dang_3_giu_lock_reference():
    assert IDENTITY_LOCK_REFERENCE in _render_reference()


def test_dang_3_noi_ro_la_anh_dinh_kem():
    assert "the subject from the attached reference image" in _render_reference()
    assert (
        "must match the attached photo exactly"
        in resolve_prompt_identity(CAPTION_REFERENCE).subject_rule
    )


def test_dang_3_cam_bia_dac_diem_khuon_mat():
    rule = resolve_prompt_identity(CAPTION_REFERENCE).subject_rule
    assert "DO NOT invent concrete facial features" in rule
    assert "eye colour" in rule and "face shape" in rule


def test_dang_3_anh_dinh_kem_chi_cho_mat_van_phai_ta_du_canh():
    """ "đính kèm mặt tôi": luật cũ dặn chỉ tả dáng/đồ/bối cảnh "khi ảnh tham chiếu chưa
    quyết định" -> model bỏ trống hết, prompt ra chung chung không có gì của ảnh mẫu."""
    rule = resolve_prompt_identity("đính kèm mặt tôi").subject_rule
    assert "only when they are not already determined" not in rule
    assert "supplies ONLY the face" in rule
    for need in ("pose", "hairstyle", "outfit", "props", "setting", "lighting"):
        assert need in rule, f"luật mặt tôi thiếu yêu cầu tả {need}"
    assert "describe all of it in full" in rule
    assert "ảnh rõ mặt" in resolve_prompt_identity("đính kèm mặt tôi").mode_hint


# ─── Ví dụ là khuôn có ô trống, không phải prompt chép được ───
def test_vi_du_khong_con_cau_chung_chung_chep_nguyen_van_duoc():
    """Bản cũ: "in a simple outdoor setting", "the requested hairstyle", "simple everyday
    clothing appropriate to the described setting" - hợp với mọi ảnh nên model chép ra
    nguyên văn (prompt thật từ ảnh cô gái đạp xe dưới cây hoa giấy không có xe đạp, hoa,
    áo quần hay ly nước nào)."""
    for render in _ALL_MODES:
        example = render().split("---")[1]
        for filler in (
            "a simple outdoor setting",
            "the requested hairstyle",
            "simple everyday clothing",
            "appropriate to the described setting",
        ):
            assert filler not in example
        for slot in ("[Pose:", "[Outfit:", "[Setting:", "[Lens:", "[Light and finish:"):
            assert slot in example
        assert "replace each slot with concrete details" in render()


def test_ba_dang_cho_ra_ba_prompt_khac_nhau():
    rendered = {render() for render in _ALL_MODES}
    assert len(rendered) == 3


def _route(caption: str) -> str:
    return resolve_prompt_identity(caption).mode


def test_dinh_tuyen_theo_tu_khoa():
    assert _route("") == "described"
    assert _route("cho cô ấy đứng ở biển") == "described"
    assert _route("giữ mặt nha") == "reference"
    assert _route("MẶT TÔI") == "reference"
    assert _route("cô gái 20") == "girl"
    assert _route("gái 20 tuổi đứng ở suối") == "girl"


@pytest.mark.parametrize(
    "caption,mode",
    [
        ("đính kèm mặt tôi", "reference"),
        ("giữ nguyên khuôn mặt nha", "reference"),
        ("ghép mặt chị vào", "reference"),
        ("mặt mình nhé", "reference"),
        ("giu mat", "reference"),  # gõ không dấu
        ("giu nguyen khuon mat", "reference"),
        ("co gai 20", "girl"),
        # Bỏ dấu thì "mặt anh" trùng "mắt anh": câu không dấu kiểu này không bật khoá mặt.
        ("chinh mat anh to hon", "described"),
    ],
)
def test_bien_the_tu_khoa_va_go_khong_dau(caption, mode):
    assert _route(caption) == mode


def test_giu_mat_uu_tien_hon_co_gai_20():
    assert _route("giữ mặt cô gái 20") == "reference"


def test_khoa_mat_toi_giu_nguyen_cau_khoa_de_dinh_kem_anh_mat():
    """Chủ bot: "mặt tôi" giữ nguyên yêu cầu khoá mặt để đính kèm ảnh mặt tham chiếu."""
    lock = resolve_prompt_identity("đính kèm mặt tôi").identity_lock
    assert lock == IDENTITY_LOCK_REFERENCE
    assert lock.startswith(
        "Identity lock: the attached reference photo is the sole source of the subject's identity."
    )
    assert "Do not redesign, beautify, age or reinterpret the face" in lock


def test_khoa_trang_phuc_san_pham_luon_nhac_dinh_kem_anh():
    """Trước đây "giữ trang phục" vẫn nhắc "không cần đính kèm ảnh" trong khi khoá trỏ vào
    ảnh tham chiếu - Gemini không có ảnh nào để giữ đồ theo."""
    for context in ("photo", "text"):
        outfit = resolve_prompt_identity("giữ trang phục", context)
        assert "không cần đính kèm" not in outfit.mode_hint
        assert "ảnh có bộ đồ cần giữ" in outfit.mode_hint
        assert "attached outfit photo" in outfit.identity_lock
        both = resolve_prompt_identity("giữ mặt giữ sản phẩm", context)
        assert "1 ảnh rõ mặt của người cần giữ + ảnh có sản phẩm cần giữ" in both.mode_hint
        assert "attached product photo" in both.identity_lock
    assert "chính ảnh mẫu này" in resolve_prompt_identity("giữ trang phục", "photo").mode_hint
    assert "chính ảnh mẫu này" not in resolve_prompt_identity("giữ trang phục", "text").mode_hint
    assert resolve_prompt_identity("cô gái 20").mode_hint.endswith("không cần đính kèm ảnh.")
