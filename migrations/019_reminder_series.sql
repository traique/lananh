-- Nhắc việc v2 (services/reminder_service.py): nhắc lặp lại (ngày/tuần/tháng/năm,
-- âm lịch), ngày nhớ (giỗ, sinh nhật, kỷ niệm) báo trước, hoãn/xong sau khi nhắc.
--
-- Mỗi LẦN nhắc vẫn là 1 dòng reminders riêng: khi scheduler claim 1 lần của chuỗi
-- (series_id), Python chèn lần kế tiếp (sau now()) thành dòng mới - idempotent nhờ
-- UNIQUE(series_id, due_at). Dòng đã nhắc giữ luồng sent=true cũ, nên outbox Zalo
-- (UNIQUE reminder_id), SQL ack/bỏ cuộc và dọn dẹp dòng đã gửi không phải đổi.

CREATE TABLE IF NOT EXISTS reminder_series (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    message TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'reminder',
    category TEXT NOT NULL DEFAULT '',
    recurrence JSONB NOT NULL,
    lead_days SMALLINT NOT NULL DEFAULT 0,
    channel TEXT NOT NULL DEFAULT 'telegram',
    recipient_id TEXT NOT NULL,
    account_id TEXT NOT NULL DEFAULT '',
    user_jid TEXT NOT NULL DEFAULT '',
    event_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT reminder_series_kind_check CHECK (kind IN ('reminder', 'ngay_nho')),
    CONSTRAINT reminder_series_category_check
        CHECK (category IN ('', 'gio', 'sinh_nhat', 'ky_niem', 'khac')),
    CONSTRAINT reminder_series_lead_check CHECK (lead_days BETWEEN 0 AND 30),
    CONSTRAINT reminder_series_channel_check CHECK (channel IN ('telegram', 'zalo', 'zoom'))
);
CREATE INDEX IF NOT EXISTS idx_reminder_series_user ON reminder_series (telegram_user_id, kind);

-- Xoá chuỗi thì xoá luôn các lần của nó (kể cả lần đang chờ); code xoá tin outbox
-- Zalo chưa gửi TRƯỚC (FK outbox chỉ SET NULL - tin vẫn sẽ đi).
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS series_id BIGINT
    REFERENCES reminder_series(id) ON DELETE CASCADE;
-- Lúc nhắc thật (Telegram/Zoom: gửi xong; Zalo: đưa vào outbox) - để "hoãn 10p"/"xong"
-- trong 30 phút tìm đúng lần vừa nhắc. done_at: người dùng đã trả lời "xong"/"hoãn".
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS fired_at TIMESTAMPTZ;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS done_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_series_due
    ON reminders (series_id, due_at) WHERE series_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_reminders_user_pending
    ON reminders (telegram_user_id, due_at) WHERE sent = false;
CREATE INDEX IF NOT EXISTS idx_reminders_user_fired
    ON reminders (telegram_user_id, fired_at) WHERE fired_at IS NOT NULL;
