-- Truy vết lượt trả lời (core/trace.py), token theo provider và quota thành viên Zalo.
--
-- provider_calls: mã lượt (telegram:<update_id>, zalo:<account>:<message_id>,
-- zoom:<account>:<event_id>, job:<tên>:<ngày>) để gom mọi lượt gọi provider của 1
-- câu trả lời; token do provider tự báo (usage / usage_metadata), NULL khi không báo.
-- Index một phần: hàng cũ và hàng ngoài lượt (NULL) không tốn chỗ trong index.
ALTER TABLE provider_calls ADD COLUMN IF NOT EXISTS turn_id TEXT;
ALTER TABLE provider_calls ADD COLUMN IF NOT EXISTS prompt_tokens INTEGER;
ALTER TABLE provider_calls ADD COLUMN IF NOT EXISTS completion_tokens INTEGER;
CREATE INDEX IF NOT EXISTS idx_provider_calls_turn
    ON provider_calls (turn_id) WHERE turn_id IS NOT NULL;

-- Số đo từng lượt (~200 byte/dòng, giữ 14 ngày - services/db_maintenance.py): chờ
-- khoá người dùng, chờ slot MAX_CONCURRENT_TURNS, gom ngữ cảnh, sinh câu trả lời,
-- gửi tin. Chỉ metadata, không có nội dung tin nhắn.
CREATE TABLE IF NOT EXISTS turn_metrics (
    id BIGSERIAL PRIMARY KEY,
    turn_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    user_kind TEXT NOT NULL DEFAULT '',
    lock_wait_ms INTEGER,
    slot_wait_ms INTEGER,
    context_ms INTEGER,
    llm_ms INTEGER,
    send_ms INTEGER,
    total_ms INTEGER,
    llm_calls SMALLINT NOT NULL DEFAULT 0,
    final_provider TEXT,
    fallback_used BOOLEAN NOT NULL DEFAULT false,
    outcome TEXT NOT NULL DEFAULT 'ok',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_turn_metrics_created ON turn_metrics (created_at DESC);

-- user_calls.kind: loại lượt (chat, chat_search, tuvi_chitiet, image, stock...) để
-- quota thành viên Zalo (services/usage_quota.py) đếm riêng lượt "nặng". Hàng cũ
-- để NULL và vẫn được tính là 1 lượt thường.
ALTER TABLE user_calls ADD COLUMN IF NOT EXISTS kind TEXT;
