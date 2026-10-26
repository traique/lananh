-- Danh mục v2: sổ giao dịch (lãi/lỗ đã chốt, phí, thuế), mốc giá vốn cho điều
-- chỉnh quyền, danh sách theo dõi + cảnh báo giá do người dùng tự đặt, nhật ký
-- thông báo chủ động (chống gửi trùng, trần mỗi ngày cho Zalo).
-- Mọi bảng mới khoá theo telegram_user_id (id Telegram dương của chủ bot, id ÂM
-- của từng thành viên Zalo) và được /zaloxoa xoá cùng (channels/zalo_users.py).

-- 1) Mốc giá vốn: ngày (giờ VN) mà quantity/average_price/stop/target của vị thế
-- đang được tính theo. Sự kiện quyền (thưởng/cổ tức cổ phiếu/chia tách) có ngày
-- GDKHQ SAU mốc và được giá xác nhận thì vị thế được quy đổi. Chỉ /capnhatcp,
-- mua thêm và lần quy đổi quyền đặt lại mốc; sửa SL/TP hay bán bớt thì KHÔNG
-- (khác updated_at). Vị thế cũ lấy mốc theo updated_at: mốc muộn hơn thực tế chỉ
-- có thể bỏ sót 1 sự kiện (như trước đây), không bao giờ quy đổi 2 lần 1 sự kiện
-- mà anh đã tự sửa bằng /capnhatcp.
ALTER TABLE stock_holdings ADD COLUMN IF NOT EXISTS cost_basis_as_of DATE;
UPDATE stock_holdings
SET cost_basis_as_of = ((updated_at AT TIME ZONE 'UTC') + INTERVAL '7 hours')::date
WHERE cost_basis_as_of IS NULL;
ALTER TABLE stock_holdings
    ALTER COLUMN cost_basis_as_of SET DEFAULT (((now() AT TIME ZONE 'UTC') + INTERVAL '7 hours')::date);
ALTER TABLE stock_holdings ALTER COLUMN cost_basis_as_of SET NOT NULL;

-- 2) Sổ giao dịch. side:
--   OPEN   vị thế đầu kỳ khi bật sổ (quantity = số cp, price = giá vốn)
--   BUY    mua thêm (quantity mua, price mua, fee phí mua)
--   SELL   bán (quantity bán, price NULL = chưa rõ giá -> không tính lãi/lỗ đã chốt)
--   ADJUST ghi đè bằng /capnhatcp hoặc xoá bằng /xoacp (quantity = vị thế MỚI)
--   CORP   quy đổi theo sự kiện quyền (quantity = vị thế MỚI, price NULL)
-- average_cost: giá vốn bình quân SAU dòng này (SELL: giá vốn lúc bán).
-- realized_pnl (SELL có giá): (giá bán - giá vốn) x số lượng - phí bán - thuế.
CREATE TABLE IF NOT EXISTS stock_transactions (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('OPEN', 'BUY', 'SELL', 'ADJUST', 'CORP')),
    quantity NUMERIC NOT NULL CHECK (quantity >= 0),
    price NUMERIC CHECK (price > 0),
    fee NUMERIC NOT NULL DEFAULT 0 CHECK (fee >= 0),
    tax NUMERIC NOT NULL DEFAULT 0 CHECK (tax >= 0),
    average_cost NUMERIC CHECK (average_cost >= 0),
    realized_pnl NUMERIC,
    note TEXT NOT NULL DEFAULT '',
    traded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_stock_transactions_user_time
    ON stock_transactions (telegram_user_id, traded_at DESC, id DESC);
INSERT INTO stock_transactions (
    telegram_user_id, symbol, side, quantity, price, average_cost, note, traded_at
)
SELECT h.telegram_user_id, h.symbol, 'OPEN', h.quantity, h.average_price, h.average_price,
       'Vị thế đầu kỳ khi bật sổ giao dịch', h.updated_at
FROM stock_holdings h
WHERE NOT EXISTS (
    SELECT 1 FROM stock_transactions t
    WHERE t.telegram_user_id = h.telegram_user_id AND t.symbol = h.symbol
);

-- 3) Danh sách mã theo dõi (/theodoi).
CREATE TABLE IF NOT EXISTS stock_watchlist (
    telegram_user_id BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_user_id, symbol)
);

-- 4) Ngưỡng giá do NGƯỜI DÙNG tự đặt ("báo anh khi HPG về 25") - không phải tín
-- hiệu của stock/policy.py. Trạng thái vượt ngưỡng lưu DB (không phải RAM) để
-- restart/redeploy không báo lại: unknown -> armed (chưa chạm) -> crossed (đã
-- báo); giá quay lại quá ngưỡng 1 khoảng thì armed lại.
CREATE TABLE IF NOT EXISTS stock_price_alerts (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('ABOVE', 'BELOW')),
    level NUMERIC NOT NULL CHECK (level > 0),
    last_state TEXT NOT NULL DEFAULT 'unknown'
        CHECK (last_state IN ('unknown', 'armed', 'crossed')),
    notified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (telegram_user_id, symbol, direction, level)
);
CREATE INDEX IF NOT EXISTS idx_stock_price_alerts_symbol ON stock_price_alerts (symbol);

-- 5) Nhật ký thông báo chủ động (cảnh báo giá, chạm stop/target trong phiên,
-- lịch sự kiện quyền): chống gửi trùng qua restart và đếm trần/ngày cho Zalo.
CREATE TABLE IF NOT EXISTS stock_alert_log (
    telegram_user_id BIGINT NOT NULL,
    kind TEXT NOT NULL,
    ref_key TEXT NOT NULL,
    sent_on DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_user_id, kind, ref_key, sent_on)
);
CREATE INDEX IF NOT EXISTS idx_stock_alert_log_created_at ON stock_alert_log (created_at);
