-- Index cho các truy vấn nóng + dọn index/bảng thừa (kế hoạch nâng cấp 11/2026, mục 3.3).
--
-- results(prompt_id): FK ON DELETE CASCADE từ prompts không có index - mỗi lần save_prompt
-- cắt prompt cũ (giữ 20 prompt/người) Postgres quét cả bảng results cho từng dòng bị xoá;
-- LATERAL "kết quả cuối của lượt" (core/repositories/feedback.py) dùng luôn (prompt_id, id DESC).
CREATE INDEX IF NOT EXISTS idx_results_prompt ON results (prompt_id, id DESC);

-- chat_messages(created_at): quét nền trích nhớ mỗi 600 s (users_with_chat_after) lọc theo
-- created_at trên MỌI người dùng - trước đây seq scan cả bảng.
CREATE INDEX IF NOT EXISTS idx_chat_messages_created ON chat_messages (created_at);

-- bg_jobs(routine_id, id DESC): /lichtacvu lấy lần chạy gần nhất của từng tác vụ (LATERAL
-- trong core/repositories/jobs.py) và ON DELETE SET NULL khi xoá tác vụ. Chỉ dòng của tác vụ
-- định kỳ - việc /viec thường có routine_id NULL.
CREATE INDEX IF NOT EXISTS idx_bg_jobs_routine ON bg_jobs (routine_id, id DESC)
    WHERE routine_id IS NOT NULL;

-- Index thừa (mỗi lần ghi vẫn phải cập nhật): idx_reminders_due (due_at) WHERE sent = false
-- là tiền tố của idx_reminders_claimable (due_at, claimed_at) cùng điều kiện;
-- idx_prompts_channel - không câu nào lọc prompts theo channel.
-- GIỮ idx_user_facts_user: forget_key/clear_facts/xoá dữ liệu người dùng lọc theo người mà
-- không kèm điều kiện invalid_at nên các index từng phần của 017 không dùng được.
DROP INDEX IF EXISTS idx_reminders_due;
DROP INDEX IF EXISTS idx_prompts_channel;

-- GIỮ 2 bảng chống trùng cũ telegram_processed_updates/zoom_processed_events (rỗng từ 015):
-- 008 đọc chúng và mọi migration phải chạy lại được (test/test_migrations.py chạy lại 008
-- trên Postgres thật); workflow sao lưu còn liệt kê zoom_processed_events.
