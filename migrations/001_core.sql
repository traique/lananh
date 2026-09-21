CREATE TABLE IF NOT EXISTS prompts (
    id SERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    command_type TEXT NOT NULL,
    prompt TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE prompts ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'telegram';
CREATE INDEX IF NOT EXISTS idx_prompts_channel ON prompts (channel, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_prompts_user_id ON prompts (telegram_user_id, id DESC);

CREATE TABLE IF NOT EXISTS results (
    id SERIAL PRIMARY KEY,
    prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    result_type TEXT NOT NULL,
    content_text TEXT,
    file_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS provider_calls (
    id SERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_provider_calls_provider ON provider_calls (provider, created_at DESC);
CREATE TABLE IF NOT EXISTS user_calls (
    id SERIAL PRIMARY KEY,
    channel TEXT NOT NULL,
    telegram_user_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_user_calls_user ON user_calls (channel, telegram_user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS chat_messages (
    id SERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chat_msg_user ON chat_messages (telegram_user_id, id DESC);
CREATE TABLE IF NOT EXISTS user_facts (
    id SERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (telegram_user_id, key)
);
CREATE INDEX IF NOT EXISTS idx_user_facts_user ON user_facts (telegram_user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS user_memory_summary (
    telegram_user_id BIGINT PRIMARY KEY,
    summary TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS user_memory_highlights (
    id SERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_user_memory_highlights_user ON user_memory_highlights (telegram_user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS notes (
    id SERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notes_user ON notes (telegram_user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS reminders (
    id SERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    message TEXT NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    sent BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (due_at) WHERE sent = false;
CREATE INDEX IF NOT EXISTS idx_reminders_claimable ON reminders (due_at, claimed_at) WHERE sent = false;

CREATE TABLE IF NOT EXISTS telegram_processed_updates (
    update_id BIGINT PRIMARY KEY,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_telegram_processed_updates_claimed_at ON telegram_processed_updates (claimed_at);
CREATE TABLE IF NOT EXISTS zalo_direct_responses (
    account_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    message_kind TEXT NOT NULL,
    response_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, message_id, message_kind)
);
CREATE INDEX IF NOT EXISTS idx_zalo_direct_responses_created_at ON zalo_direct_responses (created_at);
CREATE TABLE IF NOT EXISTS zoom_processed_events (
    event_id TEXT PRIMARY KEY,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_zoom_processed_events_claimed_at ON zoom_processed_events (claimed_at);

CREATE TABLE IF NOT EXISTS stock_holdings (
    telegram_user_id BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    quantity NUMERIC NOT NULL CHECK (quantity > 0),
    average_price NUMERIC NOT NULL CHECK (average_price > 0),
    stop_price NUMERIC CHECK (stop_price > 0),
    target_price NUMERIC CHECK (target_price > 0),
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_user_id, symbol)
);
