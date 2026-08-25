"""Test cho 3 dạng ảnh -> prompt trong handlers/media_handler.py.

Đọc IMAGE_ANALYZE_INSTRUCTION_BASE bằng ast thay vì `import handlers.media_handler`,
vì module đó import python-telegram-bot - không có trong môi trường test này.
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _prompt_render import ROOT, read_string_constant  # noqa: E402
from handlers.prompt_identity import (  # noqa: E402
    GIRL_KEYWORDS,
    IDENTITY_LOCK_GIRL,
    IDENTITY_LOCK_REFERENCE,
    KEEP_FACE_KEYWORDS,
    render_instruction,
    resolve_prompt_identity,
)

IMAGE_ANALYZE_INSTRUCTION_BASE = read_string_constant("handlers/media_handler.py", "IMAGE_ANALYZE_INSTRUCTION_BASE")

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
    source = (ROOT / "handlers/media_handler.py").read_text()
    assert "subject_phrase=subject_phrase" not in source
    assert "identity_rule=identity_rule" not in source
    assert "subject_rule=subject_rule" not in source
    assert "render_instruction(IMAGE_ANALYZE_INSTRUCTION_BASE, identity)" in source


# ─── Template chung ─────────────────────────────────
def test_ca_3_dang_deu_format_duoc_khong_thieu_placeholder():
    for render in _ALL_MODES:
        text = render()
        assert "{" not in text and "}" not in text


def test_du_11_rule():
    for render in _ALL_MODES:
        text = render()
        for n in range(1, 12):
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
    example = _render_described().split("---")[1]
    assert "Vertical 9:16 portrait orientation" in example
    assert "three-quarter body shot" in example
    assert "chest height" in example


# ─── Dáng người ─────────────────────────────────────
def test_bat_buoc_ta_dang_nguoi_chi_tiet():
    for render in _ALL_MODES:
        text = render()
        assert "POSE MUST BE GEOMETRICALLY PRECISE" in text
        for need in ("elbow", "palm", "head tilt", "gaze", "hair"):
            assert need in text, f"rule dáng người thiếu {need}"


def test_vi_du_co_doan_ta_dang_nguoi():
    example = _render_described().split("---")[1]
    assert "elbows slightly relaxed" in example
    assert "hands near hip level" in example
    assert "weight is distributed naturally" in example


# ─── Chất ảnh ───────────────────────────────────────
def test_chat_anh_bam_theo_anh_goc():
    for render in _ALL_MODES:
        text = render()
        assert "LIGHTING AND FINISH MUST MATCH THE REFERENCE" in text
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
    assert "Photographic portrait of a woman in her early 20s" in text
    assert "photo of the subject from" not in text


def test_dang_1_bat_buoc_ta_du_dac_diem_khuon_mat():
    rule = resolve_prompt_identity(CAPTION_DESCRIBED).subject_rule.lower()
    for feature in (
        "age", "face shape", "eye geometry", "eyebrow", "nose", "lip",
        "jawline", "skin tone", "hair colour",
    ):
        assert feature in rule, f"rule thiếu yêu cầu tả {feature}"


def test_dang_1_vi_du_chu_the_co_san_mo_ta_mat():
    phrase = resolve_prompt_identity(CAPTION_DESCRIBED).subject_phrase.lower()
    for feature in ("face", "eyes", "eyebrows", "nose", "lips", "skin", "hair"):
        assert feature in phrase


# ─── Dạng 2: cô gái 20 ──────────────────────────────
def test_dang_2_giu_nguyen_khoi_lock_co_dinh():
    assert IDENTITY_LOCK_GIRL in _render_girl()


def test_dang_2_ta_lai_khuon_mat_da_khoa_thanh_chu():
    identity = resolve_prompt_identity(CAPTION_GIRL)
    phrase = identity.subject_phrase.lower()
    for feature in ("soft oval-to-heart-shaped face", "smooth tapered jawline", "almond-shaped eyes", "natural double eyelids", "nasal bridge", "lips", "rounded chin"):
        assert feature in phrase, f"câu tả chủ thể thiếu {feature}"
    assert "restat" in identity.subject_rule.lower()


def test_dang_2_khong_tro_toi_anh_dinh_kem():
    text = _render_girl()
    assert "the same adult Vietnamese woman defined by the Identity Lock above" in text
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
    assert "must match the attached photo exactly" in resolve_prompt_identity(CAPTION_REFERENCE).subject_rule


def test_dang_3_cam_bia_dac_diem_khuon_mat():
    rule = resolve_prompt_identity(CAPTION_REFERENCE).subject_rule
    assert "DO NOT invent concrete facial features" in rule
    assert "eye colour" in rule and "face shape" in rule


def test_ba_dang_cho_ra_ba_prompt_khac_nhau():
    rendered = {render() for render in _ALL_MODES}
    assert len(rendered) == 3


def _route(caption: str) -> str:
    low = caption.lower()
    if any(kw in low for kw in KEEP_FACE_KEYWORDS):
        return "reference"
    if any(kw in low for kw in GIRL_KEYWORDS):
        return "girl"
    return "described"


def test_dinh_tuyen_theo_tu_khoa():
    assert _route("") == "described"
    assert _route("cho cô ấy đứng ở biển") == "described"
    assert _route("giữ mặt nha") == "reference"
    assert _route("MẶT TÔI") == "reference"
    assert _route("cô gái 20") == "girl"
    assert _route("gái 20 tuổi đứng ở suối") == "girl"


def test_giu_mat_uu_tien_hon_co_gai_20():
    assert _route("giữ mặt cô gái 20") == "reference"
