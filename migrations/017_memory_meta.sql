-- Trí nhớ hội thoại v2 (services/memory_service.py, services/conversation_log.py).
-- Mọi câu lệnh idempotent (chạy lại an toàn).

-- 1) user_facts: độ quan trọng/ghim/nguồn để giữ fact bền (tên, ngày sinh...)
--    thay vì chỉ giữ 40 fact mới cập nhật nhất; và "vô hiệu hoá thay vì xoá":
--    giá trị cũ bị thay thế được giữ lại (invalid_at + superseded_by) để còn trả
--    lời "trước đây anh làm ở đâu?". Chỉ dòng hiện hành (invalid_at IS NULL) được
--    đưa vào prompt. source='user' (lệnh /nho, "nhớ giúp anh là...") hoặc pinned
--    thì bộ trích xuất tự động không được sửa/xoá.
ALTER TABLE user_facts ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE user_facts ADD COLUMN IF NOT EXISTS importance SMALLINT NOT NULL DEFAULT 2;
ALTER TABLE user_facts ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT '';
ALTER TABLE user_facts ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'auto';
ALTER TABLE user_facts ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE user_facts ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE user_facts ADD COLUMN IF NOT EXISTS invalid_at TIMESTAMPTZ;
ALTER TABLE user_facts ADD COLUMN IF NOT EXISTS superseded_by BIGINT;
-- Fact cũ: lấy mốc theo updated_at (không đóng dấu ngày chạy migration, nếu
-- không mốc "từ tháng X" hiện trong prompt sẽ sai).
UPDATE user_facts SET created_at = updated_at WHERE created_at IS NULL;
UPDATE user_facts SET valid_from = updated_at WHERE valid_from IS NULL;
ALTER TABLE user_facts ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE user_facts ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE user_facts ALTER COLUMN valid_from SET DEFAULT now();
ALTER TABLE user_facts ALTER COLUMN valid_from SET NOT NULL;
ALTER TABLE user_facts DROP CONSTRAINT IF EXISTS user_facts_importance_check;
ALTER TABLE user_facts ADD CONSTRAINT user_facts_importance_check
    CHECK (importance BETWEEN 1 AND 3);
ALTER TABLE user_facts DROP CONSTRAINT IF EXISTS user_facts_source_check;
ALTER TABLE user_facts ADD CONSTRAINT user_facts_source_check
    CHECK (source IN ('auto', 'user'));
-- UNIQUE(telegram_user_id, key) cũ (001, tên tự sinh) chặn việc giữ giá trị cũ
-- cùng key -> bỏ, thay bằng unique index CHỈ trên dòng hiện hành.
DO $$
DECLARE c record;
BEGIN
    FOR c IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'user_facts'::regclass AND contype = 'u'
    LOOP
        EXECUTE format('ALTER TABLE user_facts DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_facts_current
    ON user_facts (telegram_user_id, key) WHERE invalid_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_user_facts_rank
    ON user_facts (telegram_user_id, pinned DESC, importance DESC, updated_at DESC)
    WHERE invalid_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_user_facts_superseded
    ON user_facts (telegram_user_id, invalid_at DESC) WHERE invalid_at IS NOT NULL;

-- 2) chat_messages.kind: loại lượt trả lời (chat, chat_ext = câu trả lời dựa trên
--    dữ liệu ngoài, stock, weather, rag, agent, translate...). Trích trí nhớ chỉ
--    đọc lượt chat/rag/agent; lượt stock giúp chặn câu chat sau mâu thuẫn action
--    của policy. Dòng cũ mặc định 'chat'.
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'chat';

-- 3) Ghi chú theo phiên: mỗi phiên chat (khoảng nghỉ > CHAT_SESSION_TIMEOUT_SEC
--    tách phiên) 1 dòng, khoá bằng id tin nhắn đầu phiên. title + bullets ngắn
--    gọn cho đầu phiên sau; summary (<= 800 ký tự) là tóm tắt phiên đang chạy,
--    chèn giữa phiên khi các lượt đầu đã trôi khỏi cửa sổ lịch sử. Tối đa ~100
--    dòng/người (cắt khi ghi) + dọn theo tuổi ở services/db_maintenance.py.
CREATE TABLE IF NOT EXISTS user_session_notes (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    session_key BIGINT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    bullets TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_session_notes_session
    ON user_session_notes (telegram_user_id, session_key);
CREATE INDEX IF NOT EXISTS idx_user_session_notes_user
    ON user_session_notes (telegram_user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_session_notes_updated
    ON user_session_notes (updated_at);
