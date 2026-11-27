-- Luồng Zalo -> Facebook: hiện TÊN nhóm thay cho group ID, và chống trùng giữa
-- các nhóm nguồn (channels/facebook_repository.py, services/facebook_dedup.py).
--
-- 1) Tên thật của nhóm Zalo (lấy từ gateway lúc /fb_themnhom, /nhomzalo hoặc khi
--    xem bài). '' = chưa biết tên (nhóm thêm từ trước khi có cột này): bot hiện
--    tên gợi nhớ cho tới khi lấy được tên thật.
ALTER TABLE zalo_facebook_groups ADD COLUMN IF NOT EXISTS group_name TEXT NOT NULL DEFAULT '';

-- 2) Dấu vân tay ghi thêm nhóm + người gửi của bài gốc: bài trùng tới từ nhóm
--    khác vẫn biết bài gốc ở nhóm nào (kể cả khi bài gốc đã bị dọn), và luật
--    "cùng người bán đăng lại ở nhóm khác" cần người gửi. NULL = dòng cũ.
ALTER TABLE facebook_post_fingerprints ADD COLUMN IF NOT EXISTS group_id TEXT;
ALTER TABLE facebook_post_fingerprints ADD COLUMN IF NOT EXISTS sender_id TEXT;

-- 3) Số lần bài này bị đăng lại (đã chặn) và các nhóm đã đăng lại nó - hiện
--    trong /fb_xem, /fb_boloc để biết deal nào lan qua nhiều nhóm.
ALTER TABLE facebook_post_fingerprints ADD COLUMN IF NOT EXISTS dup_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE facebook_post_fingerprints ADD COLUMN IF NOT EXISTS dup_groups TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE facebook_post_fingerprints ADD COLUMN IF NOT EXISTS last_dup_at TIMESTAMPTZ;

-- 4) Tra trùng chính xác (cùng nội dung / cùng sản phẩm) trên TOÀN cửa sổ
--    FACEBOOK_DEDUP_DAYS bằng index, không chỉ trong vài trăm bài gần nhất.
CREATE INDEX IF NOT EXISTS idx_facebook_fingerprints_text_hash
    ON facebook_post_fingerprints (account_id, text_hash)
    WHERE text_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_facebook_fingerprints_products
    ON facebook_post_fingerprints USING GIN (product_keys);
