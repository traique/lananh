-- Tín hiệu cổ phiếu từ nhóm Zalo (services/zalo_signals.py, lệnh /tinhieu).
--
-- 1) zalo_group_messages lưu thêm:
--    - quote_text: chữ tin được trả lời (reply) - "mã này múc được" cần nó để biết mã;
--    - sender_role: vai trò người gửi TẠI LÚC NHẮN do gateway lấy từ getGroupInfo
--      (owner = trưởng nhóm, admin = phó nhóm, member, unknown = chưa lấy được;
--      '' = tin lưu trước khi có cột này).
ALTER TABLE zalo_group_messages ADD COLUMN IF NOT EXISTS quote_text TEXT NOT NULL DEFAULT '';
ALTER TABLE zalo_group_messages ADD COLUMN IF NOT EXISTS sender_role TEXT NOT NULL DEFAULT '';

-- 2) Chế độ quét tín hiệu theo nhóm (/tinhieu on|off <nhóm> [admin|tatca]):
--    off = không quét; admin = chỉ trưởng/phó nhóm + người thêm tay ở
--    zalo_signal_people; all = mọi thành viên. Mặc định tắt.
ALTER TABLE zalo_groups ADD COLUMN IF NOT EXISTS signal_mode TEXT NOT NULL DEFAULT 'off';
ALTER TABLE zalo_groups DROP CONSTRAINT IF EXISTS zalo_groups_signal_mode_check;
ALTER TABLE zalo_groups ADD CONSTRAINT zalo_groups_signal_mode_check
    CHECK (signal_mode IN ('off', 'admin', 'all'));

-- 3) Người được theo dõi thêm ở chế độ admin (chuyên gia không làm admin nhóm).
CREATE TABLE IF NOT EXISTS zalo_signal_people (
    account_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, group_id, sender_id),
    FOREIGN KEY (account_id, group_id) REFERENCES zalo_groups(account_id, group_id) ON DELETE CASCADE
);

-- 4) Tín hiệu đã nhận diện. Người nêu, nhóm, giờ lấy từ tin gốc (không từ AI); quote
--    là đoạn NGUYÊN VĂN của tin. 1 người - 1 mã - 1 hướng - 1 loại chỉ 1 dòng mỗi ngày
--    (giờ VN) để nhắc lại không thành tin báo mới. zoom_sent_at / zalo_sent_at: đã giao
--    cho từng kênh (NULL = chưa; vòng quét sau thử lại trong vài giờ).
CREATE TABLE IF NOT EXISTS zalo_stock_signals (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    sender_role TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL,
    action TEXT NOT NULL
        CONSTRAINT zalo_stock_signals_action_check CHECK (action IN ('buy', 'sell')),
    kind TEXT NOT NULL
        CONSTRAINT zalo_stock_signals_kind_check CHECK (kind IN ('recommend', 'self')),
    entry TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    stop TEXT NOT NULL DEFAULT '',
    quote TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL,
    signal_date DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    zoom_sent_at TIMESTAMPTZ,
    zalo_sent_at TIMESTAMPTZ,
    UNIQUE (account_id, group_id, sender_id, symbol, action, kind, signal_date),
    FOREIGN KEY (account_id, group_id) REFERENCES zalo_groups(account_id, group_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_zalo_stock_signals_recent
    ON zalo_stock_signals (account_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_zalo_stock_signals_symbol
    ON zalo_stock_signals (account_id, symbol, sent_at DESC);
