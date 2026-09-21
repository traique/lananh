CREATE TABLE IF NOT EXISTS chat_embeddings (
    id SERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    content TEXT NOT NULL,
    embedding vector(768) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chat_embeddings_user ON chat_embeddings (telegram_user_id);
