-- Đánh giá câu trả lời chat (services/answer_feedback.py): nút 👍/👎 dưới câu trả lời
-- Telegram và lệnh /sai [lý do] trên mọi kênh, kèm provider/model đã trả lời.
--
-- KHÔNG có khoá ngoại tới prompts: prompts chỉ giữ 20 dòng gần nhất mỗi người (core/
-- database.save_prompt) - ON DELETE CASCADE sẽ xoá mất đánh giá, còn không cascade thì
-- lệnh dọn prompts lỗi. Vì vậy mỗi dòng tự chụp lại câu hỏi/câu trả lời (cắt ~2000 ký
-- tự) lúc đánh giá. telegram_user_id theo quy ước chung: chủ bot hoặc id ÂM của từng
-- người Zalo (dữ liệu Zalo tách hẳn Telegram). Giữ 180 ngày (services/db_maintenance.py).

CREATE TABLE IF NOT EXISTS answer_feedback (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL,
    telegram_user_id BIGINT NOT NULL,
    prompt_id BIGINT NOT NULL,
    rating SMALLINT NOT NULL,
    reason TEXT,
    provider TEXT,
    model TEXT,
    prompt_text TEXT NOT NULL DEFAULT '',
    answer_text TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT answer_feedback_rating_check CHECK (rating IN (-1, 1))
);
-- 1 đánh giá cho mỗi câu trả lời của mỗi người: bấm lại/đổi ý thì cập nhật dòng cũ.
CREATE UNIQUE INDEX IF NOT EXISTS idx_answer_feedback_user_prompt
    ON answer_feedback (telegram_user_id, prompt_id);
CREATE INDEX IF NOT EXISTS idx_answer_feedback_created ON answer_feedback (created_at);

-- Provider/model đã trả lời từng lượt (ghi lúc lưu kết quả) để đánh giá sau còn biết
-- câu nào do Groq, câu nào do Gemini... Cột tuỳ chọn, dòng cũ để NULL.
ALTER TABLE results ADD COLUMN IF NOT EXISTS provider TEXT;
ALTER TABLE results ADD COLUMN IF NOT EXISTS model TEXT;
