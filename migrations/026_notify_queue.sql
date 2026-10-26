-- Hàng đợi thông báo gom (services/notify_hub.py): tin chủ động mức NORMAL (hỏi
-- thăm, sự kiện quyền báo trước, cảnh báo nhẹ, tóm tắt...) và tin URGENT bị giữ
-- trong giờ yên lặng nằm ở đây cho tới lượt xả - gộp thành MỘT tin cho mỗi người
-- nhận. Bền qua restart/redeploy (Render ngủ không làm mất tin).
--
-- channel + user_id là ĐÚNG người nhận: telegram/zoom = chủ bot (ALLOWED_USER_ID),
-- zalo = internal_user_id ÂM của từng người (kể cả Zalo của chủ bot) - không bao
-- giờ gom chéo kênh/người. status: pending -> sent | dropped (vượt trần mỗi người
-- nhận, hoặc người nhận đã bị gỡ pairing). dedup_key (tuỳ nguồn, vd
-- "brief:zalo:-5:2026-10-26") chống xếp 2 lần cùng 1 tin khi nguồn chạy lại; dòng
-- sent/dropped được giữ RETENTION_NOTIFY_QUEUE_DAYS ngày rồi mới xoá
-- (services/db_maintenance.py) để chống trùng vẫn có tác dụng trong khoảng đó.
-- claimed_until: lease khi đang gửi Telegram/Zoom (2 instance chạy chồng lúc
-- deploy không gửi đôi); Zalo xả trong 1 transaction cùng lúc với zalo_outbox.
CREATE TABLE IF NOT EXISTS notify_queue (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL
        CONSTRAINT notify_queue_channel_check CHECK (channel IN ('telegram', 'zalo', 'zoom')),
    user_id BIGINT NOT NULL,
    level TEXT NOT NULL
        CONSTRAINT notify_queue_level_check CHECK (level IN ('urgent', 'normal')),
    source TEXT NOT NULL,
    content TEXT NOT NULL,
    dedup_key TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CONSTRAINT notify_queue_status_check CHECK (status IN ('pending', 'sent', 'dropped')),
    claimed_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_notify_queue_dedup
    ON notify_queue (channel, user_id, dedup_key) WHERE dedup_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_notify_queue_pending
    ON notify_queue (channel, user_id, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_notify_queue_created
    ON notify_queue (created_at);
