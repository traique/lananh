-- Độ bền runtime: trạng thái DEAD cho webhook inbox + index phục vụ dọn dữ liệu cũ.
-- Mọi câu lệnh idempotent (chạy lại an toàn).

-- 1) webhook_inbox: sự kiện lỗi quá WEBHOOK_MAX_ATTEMPTS lần chuyển DEAD (ngừng
--    thử, báo chủ bot 1 lần, giữ WEBHOOK_DEAD_RETENTION_DAYS ngày rồi xoá).
--    Bảng do 008 tạo; bọc to_regclass để file vẫn chạy được trên DB dựng lại
--    không theo thứ tự (test nâng cấp áp 008 sau cùng).
DO $$
DECLARE c record;
BEGIN
    IF to_regclass('webhook_inbox') IS NOT NULL THEN
        -- CHECK cũ do 008 khai báo inline (tên tự sinh) - tìm theo nội dung để bỏ.
        FOR c IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'webhook_inbox'::regclass AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%status%'
        LOOP
            EXECUTE format('ALTER TABLE webhook_inbox DROP CONSTRAINT %I', c.conname);
        END LOOP;
        ALTER TABLE webhook_inbox ADD CONSTRAINT webhook_inbox_status_check
            CHECK (status IN ('PENDING', 'PROCESSING', 'DONE', 'DEAD'));
        -- Index cũ (status <> 'DONE') gồm cả dòng DEAD; worker chỉ quét dòng còn sống.
        DROP INDEX IF EXISTS idx_webhook_inbox_ready;
        CREATE INDEX IF NOT EXISTS idx_webhook_inbox_active
            ON webhook_inbox (available_at, created_at) WHERE status IN ('PENDING', 'PROCESSING');
        CREATE INDEX IF NOT EXISTS idx_webhook_inbox_dead
            ON webhook_inbox (completed_at) WHERE status = 'DEAD';
    END IF;
END $$;

-- 2) Index cho job dọn dữ liệu hằng ngày (services/db_maintenance.py) - xoá
--    theo created_at/sent_at mà không quét toàn bảng.
CREATE INDEX IF NOT EXISTS idx_user_calls_created_at ON user_calls (created_at);
CREATE INDEX IF NOT EXISTS idx_provider_calls_created_at ON provider_calls (created_at);
CREATE INDEX IF NOT EXISTS idx_zalo_outbox_sent_at ON zalo_outbox (sent_at) WHERE sent_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_reminders_sent_due ON reminders (due_at) WHERE sent = true;
