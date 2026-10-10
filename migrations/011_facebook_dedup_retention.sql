-- Chống trùng bài Zalo -> Facebook và dọn dữ liệu cho Supabase free tier.
-- Dấu vân tay tách khỏi facebook_post_queue (không FK) để vẫn chặn bài trùng
-- sau khi bài gốc và ảnh của nó đã bị dọn.
CREATE TABLE IF NOT EXISTS facebook_post_fingerprints (
    post_id BIGINT PRIMARY KEY,
    account_id TEXT NOT NULL,
    text_hash TEXT,
    product_keys TEXT[] NOT NULL DEFAULT '{}',
    image_hashes BIGINT[] NOT NULL DEFAULT '{}',
    folded_text TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_facebook_fingerprints_recent
    ON facebook_post_fingerprints (account_id, created_at DESC);

-- Ảnh của bài đã đăng xong / đã bỏ qua không còn dùng tới: dọn ngay một lần.
DELETE FROM facebook_post_media m
USING facebook_post_queue q
WHERE m.post_id = q.id AND q.status IN ('POSTED', 'REJECTED');
