-- Chi tiêu cá nhân (services/expense_service.py): "cafe 45k", "đổ xăng 1tr2", ảnh hoá đơn
-- kèm chữ "hoá đơn", /chitieu tổng theo danh mục (cộng bằng SQL, không nhờ AI).
--
-- telegram_user_id theo cùng quy ước các bảng cá nhân khác: id chủ bot (Telegram/Zoom)
-- hoặc internal_user_id ÂM của từng người Zalo - dữ liệu Zalo tách hẳn Telegram.
-- source 'sepay' + external_id để dành cho webhook ngân hàng nếu chủ bot duyệt sau
-- (chưa có route nào ghi 2 cột này).

CREATE TABLE IF NOT EXISTS expenses (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    amount_vnd BIGINT NOT NULL,
    category TEXT NOT NULL DEFAULT 'khac',
    note TEXT NOT NULL DEFAULT '',
    spent_on DATE NOT NULL,
    source TEXT NOT NULL DEFAULT 'chat',
    draft BOOLEAN NOT NULL DEFAULT false,
    event_key TEXT UNIQUE,
    external_id TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT expenses_amount_check CHECK (amount_vnd > 0 AND amount_vnd <= 100000000000),
    CONSTRAINT expenses_source_check CHECK (source IN ('chat', 'command', 'receipt', 'sepay'))
);
CREATE INDEX IF NOT EXISTS idx_expenses_user_day ON expenses (telegram_user_id, spent_on);
CREATE INDEX IF NOT EXISTS idx_expenses_user_created ON expenses (telegram_user_id, created_at);
