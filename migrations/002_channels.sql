CREATE SEQUENCE IF NOT EXISTS zalo_users_uid_seq;
CREATE TABLE IF NOT EXISTS zalo_users (
    external_id TEXT PRIMARY KEY,
    internal_user_id BIGINT UNIQUE NOT NULL DEFAULT (-(nextval('zalo_users_uid_seq'))),
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE zalo_users
ADD COLUMN IF NOT EXISTS internal_user_id BIGINT UNIQUE NOT NULL DEFAULT (-(nextval('zalo_users_uid_seq')));

CREATE TABLE IF NOT EXISTS zalo_groups (
    account_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, group_id),
    UNIQUE (account_id, alias)
);
CREATE TABLE IF NOT EXISTS zalo_group_messages (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, group_id, message_id),
    FOREIGN KEY (account_id, group_id) REFERENCES zalo_groups(account_id, group_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_zalo_group_messages_window ON zalo_group_messages (account_id, group_id, sent_at DESC);
CREATE TABLE IF NOT EXISTS zalo_group_summaries (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    summary_type TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, group_id, summary_type, window_start, window_end),
    FOREIGN KEY (account_id, group_id) REFERENCES zalo_groups(account_id, group_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS zalo_outbox (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    summary_id BIGINT
);
ALTER TABLE zalo_outbox ADD COLUMN IF NOT EXISTS summary_id BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_zalo_outbox_summary ON zalo_outbox (summary_id) WHERE summary_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_zalo_outbox_pending ON zalo_outbox (account_id, id) WHERE sent_at IS NULL;

CREATE TABLE IF NOT EXISTS zalo_facebook_groups (
    account_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, group_id),
    UNIQUE (account_id, alias)
);
CREATE TABLE IF NOT EXISTS facebook_post_queue (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    source_message_ids TEXT[] NOT NULL DEFAULT '{}',
    original_content TEXT NOT NULL DEFAULT '',
    processed_content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING_APPROVAL',
    facebook_post_id TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at TIMESTAMPTZ,
    posted_at TIMESTAMPTZ,
    CONSTRAINT facebook_post_queue_status CHECK (
        status IN ('PENDING_APPROVAL', 'POSTING', 'POSTED', 'REJECTED', 'ERROR')
    )
);
CREATE TABLE IF NOT EXISTS facebook_post_media (
    id BIGSERIAL PRIMARY KEY,
    post_id BIGINT NOT NULL REFERENCES facebook_post_queue(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    mime_type TEXT NOT NULL,
    content BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (post_id, position)
);
CREATE TABLE IF NOT EXISTS shopee_affiliate_links (
    account_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    affiliate_url TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, source_url)
);
CREATE TABLE IF NOT EXISTS affiliate_short_links (
    token TEXT PRIMARY KEY,
    target_url TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_facebook_post_queue_status ON facebook_post_queue (account_id, status, created_at DESC);
