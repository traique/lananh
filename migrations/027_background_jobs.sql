-- Việc nền có trạng thái (/viec) và tác vụ định kỳ bằng lời (/lichtacvu) - cùng
-- 1 bộ chạy lượt AI nền (services/background_jobs.py, services/routines.py).
--
-- routines: yêu cầu AI chạy theo lịch lặp (cùng dạng JSON với
-- reminder_series.recurrence, services/recurrence.py). Khác nhắc việc: nhắc việc
-- gửi 1 câu cố định; tác vụ chạy 1 lượt AI (tra web, số liệu, trí nhớ của đúng
-- người đó) rồi gửi kết quả. next_run_at = lần chạy kế tiếp; vòng nền "nhận" 1
-- lần chạy bằng UPDATE ... WHERE next_run_at = <giá trị cũ> (2 instance chạy chồng
-- lúc deploy không chạy đôi), đồng thời xếp 1 dòng bg_jobs (dedup_key theo giờ
-- hẹn). telegram_user_id: chủ bot (dương) hoặc internal_user_id ÂM của Zalo -
-- channels/zalo_users.py xoá theo cột này khi /zaloxoa, /xoadulieu.
CREATE TABLE IF NOT EXISTS routines (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    channel TEXT NOT NULL
        CONSTRAINT routines_channel_check CHECK (channel IN ('telegram', 'zalo', 'zoom')),
    prompt TEXT NOT NULL,
    recurrence JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CONSTRAINT routines_status_check CHECK (status IN ('active', 'paused')),
    next_run_at TIMESTAMPTZ,
    last_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_routines_user ON routines (telegram_user_id, id);
CREATE INDEX IF NOT EXISTS idx_routines_due ON routines (next_run_at) WHERE status = 'active';

-- bg_jobs: mỗi lượt AI nền (kind 'job' = /viec, 'routine' = 1 lần chạy tác vụ).
-- status: queued -> running -> succeeded | failed | cancelled | timed_out.
-- lease_until: worker đang chạy gia hạn mỗi 30 giây; hết hạn (process chết,
-- redeploy) thì vòng dọn đưa về queued để chạy lại 1 lần (attempts < 2), lần
-- thứ 2 thì failed. result: câu trả lời đã cắt có trần (services/background_jobs).
-- tokens: tổng token provider báo về trong lượt (NULL = provider không báo).
CREATE TABLE IF NOT EXISTS bg_jobs (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    channel TEXT NOT NULL
        CONSTRAINT bg_jobs_channel_check CHECK (channel IN ('telegram', 'zalo', 'zoom')),
    kind TEXT NOT NULL DEFAULT 'job'
        CONSTRAINT bg_jobs_kind_check CHECK (kind IN ('job', 'routine')),
    routine_id BIGINT REFERENCES routines(id) ON DELETE SET NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CONSTRAINT bg_jobs_status_check CHECK (
            status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'timed_out')
        ),
    result TEXT,
    error TEXT,
    no_reply BOOLEAN NOT NULL DEFAULT false,
    attempts SMALLINT NOT NULL DEFAULT 0,
    tokens INTEGER,
    provider TEXT,
    scheduled_for TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    dedup_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bg_jobs_dedup ON bg_jobs (dedup_key)
    WHERE dedup_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bg_jobs_queue ON bg_jobs (id) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_bg_jobs_running ON bg_jobs (lease_until) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_bg_jobs_user ON bg_jobs (telegram_user_id, id);
CREATE INDEX IF NOT EXISTS idx_bg_jobs_created ON bg_jobs (created_at);
