-- Bật/tắt tổng kết tự động hằng ngày theo từng nhóm (/tongket on|off).
-- Mặc định bật để giữ nguyên hành vi cũ. Tắt chỉ dừng bản tổng kết hằng ngày;
-- tin nhóm vẫn được lưu nên /tongket <nhóm> và /dangnoi vẫn dùng được.
ALTER TABLE zalo_groups ADD COLUMN IF NOT EXISTS daily_summary BOOLEAN NOT NULL DEFAULT TRUE;
