-- Ảnh chân dung cho trang /admin/avatar (web_avatar.py, services/avatar_photo.py):
-- 'main' là ảnh chính kèm 478 điểm mặt MediaPipe (trình duyệt dò 1 lần lúc upload,
-- toạ độ chuẩn hoá 0..1 dạng [[x, y], ...]); 'blink' là ảnh mắt nhắm tuỳ chọn, cùng
-- kích thước ảnh chính. Ảnh đã chuẩn hoá trước khi lưu (WebP, cạnh dài <= 1280 px,
-- bỏ EXIF) nên mỗi dòng chỉ vài trăm KB. Chỉ phục vụ qua route có phiên admin.
CREATE TABLE IF NOT EXISTS avatar_photo (
    kind TEXT PRIMARY KEY,
    mime TEXT NOT NULL,
    data BYTEA NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    landmarks JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT avatar_photo_kind_check CHECK (kind IN ('main', 'blink')),
    CONSTRAINT avatar_photo_size_check CHECK (width > 0 AND height > 0),
    CONSTRAINT avatar_photo_main_landmarks CHECK (kind <> 'main' OR landmarks IS NOT NULL)
);
