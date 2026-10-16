-- provider_calls: ghi cả lượt lỗi/timeout/429 kèm độ trễ và giai đoạn (chat,
-- utility, search...) để thấy lượt nào đốt thời gian & quota. Hàng cũ coi là 'ok'.
ALTER TABLE provider_calls ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT 'ok';
ALTER TABLE provider_calls ADD COLUMN IF NOT EXISTS latency_ms INTEGER;
ALTER TABLE provider_calls ADD COLUMN IF NOT EXISTS stage TEXT;
ALTER TABLE provider_calls ADD COLUMN IF NOT EXISTS prompt_chars INTEGER;
CREATE INDEX IF NOT EXISTS idx_provider_calls_outcome ON provider_calls (outcome, created_at DESC);

-- Bảng thống kê ghi liên tục 24/7: SERIAL (int32) -> BIGINT. Hai bảng này không
-- có khoá ngoại trỏ tới nên đổi kiểu an toàn; bảng còn lại (prompts/results...)
-- có retention và FK, giữ nguyên.
ALTER TABLE provider_calls ALTER COLUMN id TYPE BIGINT;
ALTER SEQUENCE provider_calls_id_seq AS BIGINT;
ALTER TABLE user_calls ALTER COLUMN id TYPE BIGINT;
ALTER SEQUENCE user_calls_id_seq AS BIGINT;

-- Bảng không còn code nào dùng: short-link affiliate (endpoint /r/{token} đã gỡ).
DROP TABLE IF EXISTS affiliate_short_links;
-- Hai bảng claim theo update/event cũng không còn được ghi (webhook_inbox thay
-- thế từ 008, dữ liệu dedup đã được 008 chép sang). CHỈ dọn dòng, KHÔNG drop:
-- 008 đọc 2 bảng này và phải chạy lại được (test idempotent) sau khi 015 đã áp.
DELETE FROM telegram_processed_updates;
DELETE FROM zoom_processed_events;
