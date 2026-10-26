-- Nhịp tim sao lưu DB: workflow GitHub Actions (.github/workflows/backup.yml) ghi 1
-- dòng sau mỗi lần dump + mã hoá + upload thành công (kind='backup') và sau mỗi lần
-- diễn tập khôi phục đạt (kind='drill'). Bot cảnh báo khi bản sao lưu mới nhất quá
-- BACKUP_STALE_DAYS ngày - GitHub tự tắt lịch chạy của repo public sau 60 ngày không
-- có commit, backup có thể dừng âm thầm. ~1 dòng/ngày nên không cần dọn.
--
-- Role chỉ-đọc lananh_backup (dùng cho pg_dump) được tạo TAY trong SQL editor để
-- mật khẩu không bao giờ nằm trong repo - xem docs/backup-restore.md. Nếu role đã
-- có sẵn lúc migration này chạy thì cấp luôn quyền ghi nhịp tim; nếu tạo sau, câu
-- GRANT nằm trong hướng dẫn tạo role.
CREATE TABLE IF NOT EXISTS backup_runs (
    run_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'backup' CHECK (kind IN ('backup', 'drill')),
    finished_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    size_bytes BIGINT
);
CREATE INDEX IF NOT EXISTS idx_backup_runs_kind_finished
    ON backup_runs (kind, finished_at DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lananh_backup') THEN
        GRANT SELECT, INSERT ON backup_runs TO lananh_backup;
    END IF;
END $$;
