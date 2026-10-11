-- Outbox Zalo: thử lại có giới hạn + backoff để 1 tin không gửi được (vd người
-- nhận chặn bot) không chặn mọi tin phía sau mãi mãi.
-- Mọi câu lệnh idempotent (chạy lại an toàn).

-- attempts: số lần gateway báo gửi lỗi; next_attempt_at: chưa tới giờ này thì
-- gateway không lấy lại tin; failed_at: bỏ cuộc sau ZALO_OUTBOX_MAX_ATTEMPTS lần
-- (giữ dòng để tra cứu); chunks_sent: số đoạn của tin dài đã gửi thành công,
-- lần thử sau gửi tiếp từ đoạn kế, không gửi lặp đoạn đã đi.
ALTER TABLE zalo_outbox ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE zalo_outbox ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE zalo_outbox ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;
ALTER TABLE zalo_outbox ADD COLUMN IF NOT EXISTS failed_at TIMESTAMPTZ;
ALTER TABLE zalo_outbox ADD COLUMN IF NOT EXISTS chunks_sent INTEGER NOT NULL DEFAULT 0;

-- Index cũ (002) gồm cả dòng đã bỏ cuộc; gateway chỉ quét tin còn chờ gửi.
DROP INDEX IF EXISTS idx_zalo_outbox_pending;
CREATE INDEX IF NOT EXISTS idx_zalo_outbox_ready
    ON zalo_outbox (account_id, id) WHERE sent_at IS NULL AND failed_at IS NULL;
-- Phục vụ dọn dòng đã bỏ cuộc theo failed_at mà không quét toàn bảng.
CREATE INDEX IF NOT EXISTS idx_zalo_outbox_failed_at
    ON zalo_outbox (failed_at) WHERE failed_at IS NOT NULL;
