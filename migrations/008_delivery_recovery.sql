ALTER TABLE reminders ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'telegram';
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS recipient_id TEXT;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS account_id TEXT NOT NULL DEFAULT '';
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS user_jid TEXT NOT NULL DEFAULT '';
UPDATE reminders SET recipient_id = telegram_user_id::text WHERE recipient_id IS NULL;
-- The former schema lost channel metadata; negative IDs can be recovered from Zalo users.
UPDATE reminders r SET channel = 'zalo', recipient_id = u.external_id
FROM zalo_users u WHERE r.telegram_user_id = u.internal_user_id AND r.telegram_user_id < 0;
ALTER TABLE reminders ALTER COLUMN recipient_id SET NOT NULL;
ALTER TABLE reminders ADD CONSTRAINT reminders_channel_check CHECK (channel IN ('telegram', 'zalo', 'zoom'));
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS event_key TEXT UNIQUE;
ALTER TABLE notes ADD COLUMN IF NOT EXISTS event_key TEXT UNIQUE;

ALTER TABLE zalo_outbox ADD COLUMN IF NOT EXISTS reminder_id INTEGER REFERENCES reminders(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_zalo_outbox_reminder ON zalo_outbox (reminder_id) WHERE reminder_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS webhook_inbox (
    channel TEXT NOT NULL CHECK (channel IN ('telegram', 'zoom')),
    event_id TEXT NOT NULL,
    payload JSONB,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'PROCESSING', 'DONE')),
    lease_token TEXT,
    lease_until TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (channel, event_id)
);
CREATE INDEX IF NOT EXISTS idx_webhook_inbox_ready ON webhook_inbox (available_at, created_at) WHERE status <> 'DONE';
CREATE INDEX IF NOT EXISTS idx_webhook_inbox_done ON webhook_inbox (completed_at) WHERE status = 'DONE';
-- Payloads from old claims cannot be recovered. Preserve their deduplication window.
INSERT INTO webhook_inbox (channel, event_id, status, created_at, completed_at)
SELECT 'telegram', update_id::text, 'DONE', claimed_at, claimed_at FROM telegram_processed_updates
ON CONFLICT DO NOTHING;
INSERT INTO webhook_inbox (channel, event_id, status, created_at, completed_at)
SELECT 'zoom', event_id, 'DONE', claimed_at, claimed_at FROM zoom_processed_events
ON CONFLICT DO NOTHING;

ALTER TABLE facebook_post_queue ADD COLUMN IF NOT EXISTS claim_token TEXT;
ALTER TABLE facebook_post_queue ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE facebook_post_queue ADD COLUMN IF NOT EXISTS source_event_key TEXT UNIQUE;
ALTER TABLE facebook_post_targets ADD COLUMN IF NOT EXISTS publish_started_at TIMESTAMPTZ;
ALTER TABLE facebook_post_targets DROP CONSTRAINT IF EXISTS facebook_post_targets_status;
ALTER TABLE facebook_post_targets ADD CONSTRAINT facebook_post_targets_status CHECK (
    status IN ('PENDING', 'POSTING', 'POSTED', 'ERROR', 'UNKNOWN')
);
-- An old POSTING row has no record of which create calls completed. Do not retry blindly.
INSERT INTO facebook_post_targets (post_id, page_key, status, error_message)
SELECT id, 'default', 'UNKNOWN', 'Lượt đăng cũ bị gián đoạn; cần đối soát trước khi gửi lại.'
FROM facebook_post_queue WHERE status = 'POSTING'
  AND NOT EXISTS (SELECT 1 FROM facebook_post_targets WHERE post_id = facebook_post_queue.id)
ON CONFLICT DO NOTHING;
UPDATE facebook_post_targets SET status = 'UNKNOWN', publish_started_at = now()
WHERE status <> 'POSTED' AND facebook_post_id IS NULL AND EXISTS (
    SELECT 1 FROM facebook_post_queue WHERE id = post_id AND status = 'POSTING'
);
UPDATE facebook_post_targets SET status = 'POSTED' WHERE facebook_post_id IS NOT NULL;

-- Keep old cache rows for caption repair, but require fresh conversion/manual confirmation.
ALTER TABLE shopee_affiliate_links ADD COLUMN IF NOT EXISTS verification_version SMALLINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS stock_closed_positions (
    telegram_user_id BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    closed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_user_id, symbol)
);
