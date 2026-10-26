-- Sự kiện để Lan Anh chủ động hỏi thăm ("mai anh đi phỏng vấn" -> tối mai hỏi
-- "Phỏng vấn thế nào anh?") - services/proactive_service.py. Chỉ trích cho người
-- được phép (chủ bot; thành viên Zalo tự bật /hoithan on). Kênh giao suy ra từ
-- telegram_user_id: id dương = chủ bot -> Telegram, id ÂM = Zalo của đúng người
-- đó -> Zalo; KHÔNG bao giờ giao chéo sự kiện nguồn Zalo sang Telegram.
-- status: pending (chờ tới ngày) -> asked (đã claim/gửi, không gửi lại) | done
-- (đã xong, quá hạn hoặc người dùng bảo "đừng nhắc nữa"). Tối đa 50 dòng/người
-- (cắt khi ghi), dọn theo tuổi ở services/db_maintenance.py.
CREATE TABLE IF NOT EXISTS user_events (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    when_text TEXT NOT NULL DEFAULT '',
    event_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CONSTRAINT user_events_status_check CHECK (status IN ('pending', 'asked', 'done')),
    claimed_at TIMESTAMPTZ,
    asked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_events_dedup
    ON user_events (telegram_user_id, event_date, title);
CREATE INDEX IF NOT EXISTS idx_user_events_due
    ON user_events (event_date) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_user_events_user
    ON user_events (telegram_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_events_created
    ON user_events (created_at);
