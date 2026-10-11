-- Semantic recall (pgvector) đã bỏ: dọn bảng embedding cũ nếu còn.
-- KHÔNG drop extension "vector" (Supabase quản lý quyền extension).
DROP TABLE IF EXISTS chat_embeddings;
