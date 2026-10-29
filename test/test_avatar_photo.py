"""Ảnh avatar (services/avatar_photo.py): kiểm định dạng/kích thước, chuẩn hoá WebP
không EXIF, điểm mặt 478 điểm, ảnh mắt nhắm cùng kích thước ảnh chính."""

import io
import json

import pytest
from PIL import Image

from services import avatar_photo


def _jpeg(size=(800, 600), *, orientation: int | None = None, color="pink") -> bytes:
    image = Image.new("RGB", size, color)
    out = io.BytesIO()
    if orientation is None:
        image.save(out, "JPEG", quality=90)
    else:
        exif = Image.Exif()
        exif[0x0112] = orientation  # Orientation
        exif[0x010F] = "Máy ảnh bí mật"  # Make - không được còn trong bản lưu
        image.save(out, "JPEG", quality=90, exif=exif.tobytes())
    return out.getvalue()


def _png_rgba(size=(400, 400)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, (255, 0, 0, 0)).save(out, "PNG")
    return out.getvalue()


def _points(n=avatar_photo.LANDMARK_COUNT, value=0.5):
    return [[value, value, -0.01] for _ in range(n)]


def test_diem_mat_dung_cau_truc_moi_nhan():
    points = avatar_photo.validate_landmarks(_points())
    assert len(points) == 478 and points[0] == [0.5, 0.5]  # bỏ z, làm tròn
    assert avatar_photo.validate_landmarks(json.dumps(_points())) == points  # chuỗi JSON
    bad_inputs = [
        _points(477),
        _points(479),
        _points(value=1.2),
        _points(value=-0.01),
        [[0.5, True]] * 478,
        [[0.5, float("nan")]] * 478,
        [[0.5]] * 478,
        [{"x": 0.5, "y": 0.5}] * 478,
        "không phải json",
        None,
    ]
    for bad in bad_inputs:
        with pytest.raises(avatar_photo.PhotoError) as exc:
            avatar_photo.validate_landmarks(bad)
        assert exc.value.code == "landmarks"


def test_chuan_hoa_xoay_theo_exif_bo_exif_luu_webp():
    # Orientation 6 = xoay 90 độ: ảnh ngang 800x600 thành dọc 600x800.
    photo = avatar_photo.process(_jpeg(orientation=6))
    assert (photo.width, photo.height) == (600, 800) and photo.mime == "image/webp"
    with Image.open(io.BytesIO(photo.data)) as saved:
        assert saved.format == "WEBP" and saved.size == (600, 800)
        assert not saved.getexif() and "exif" not in saved.info and "icc_profile" not in saved.info


def test_thu_nho_canh_dai_toi_da_1280_va_nen_trang_cho_anh_trong_suot():
    photo = avatar_photo.process(_jpeg((3000, 2000)))
    assert (photo.width, photo.height) == (1280, 853)
    photo = avatar_photo.process(_png_rgba())
    with Image.open(io.BytesIO(photo.data)) as saved:
        assert saved.mode == "RGB" and saved.getpixel((10, 10)) == (255, 255, 255)


def test_tu_choi_file_khong_phai_anh_qua_lon_qua_nho():
    cases = [
        (b"", "empty"),
        (b"GIF89a" + b"0" * 100, "format"),
        (b"%PDF-1.7 hello", "format"),
        (_jpeg((150, 150)), "small"),
    ]
    for data, code in cases:
        with pytest.raises(avatar_photo.PhotoError) as exc:
            avatar_photo.process(data)
        assert exc.value.code == code
    with pytest.raises(avatar_photo.PhotoError) as exc:
        avatar_photo.process(b"\xff\xd8" + b"0" * (avatar_photo.MAX_UPLOAD_BYTES + 1))
    assert exc.value.code == "too_big" and exc.value.status == 413
    gif = io.BytesIO()
    Image.new("RGB", (400, 400)).save(gif, "GIF")
    with pytest.raises(avatar_photo.PhotoError, match="PNG, JPEG hoặc WebP"):
        avatar_photo.process(gif.getvalue())


@pytest.fixture
def fake_db(monkeypatch):
    rows: dict[str, dict] = {}

    async def meta():
        return {k: {key: v for key, v in row.items() if key != "data"} for k, row in rows.items()}

    async def put(kind, **kw):
        rows[kind] = {**kw, "updated_at": None}

    async def delete(kind):
        removed = [k for k in list(rows) if k == kind or (kind == "main" and k == "blink")]
        for key in removed:
            del rows[key]
        return len(removed)

    monkeypatch.setattr(avatar_photo.db, "avatar_photo_meta", meta)
    monkeypatch.setattr(avatar_photo.db, "avatar_photo_put", put)
    monkeypatch.setattr(avatar_photo.db, "avatar_photo_delete", delete)
    return rows


async def test_luu_anh_chinh_va_anh_mat_nham_cung_kich_thuoc(fake_db):
    with pytest.raises(avatar_photo.PhotoError) as exc:
        await avatar_photo.save("blink", _jpeg())
    assert exc.value.code == "no_main"
    with pytest.raises(avatar_photo.PhotoError) as exc:
        await avatar_photo.save("main", _jpeg(), landmarks=None)  # ảnh chính bắt buộc có điểm
    assert exc.value.code == "landmarks"
    saved = await avatar_photo.save("main", _jpeg(), landmarks=_points())
    assert saved["width"] == 800 and fake_db["main"]["landmarks"][0] == [0.5, 0.5]
    assert fake_db["main"]["mime"] == "image/webp"
    with pytest.raises(avatar_photo.PhotoError) as exc:
        await avatar_photo.save("blink", _jpeg((640, 480)))
    assert exc.value.code == "size_mismatch" and "800x600" in exc.value.message
    await avatar_photo.save("blink", _jpeg(color="red"))
    assert fake_db["blink"]["landmarks"] is None
    # Đổi ảnh chính khác kích thước: ảnh mắt nhắm cũ không còn khớp -> xoá.
    saved = await avatar_photo.save("main", _jpeg((900, 600)), landmarks=_points())
    assert saved["blinkRemoved"] is True and "blink" not in fake_db
    with pytest.raises(avatar_photo.PhotoError):
        await avatar_photo.save("other", _jpeg())
    assert await avatar_photo.remove("main") == 1 and fake_db == {}
