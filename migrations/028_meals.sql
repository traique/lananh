-- Nhật ký ăn uống (services/nutrition_service.py, lệnh /an): mỗi dòng 1 món của 1 bữa.
-- kcal là ƯỚC LƯỢNG cho cả khẩu phần đã ghi (bảng món Việt soạn sẵn
-- assets/thuc_pham.yaml nhân số phần; NULL = món lạ, không có số), kcal_source
-- cho biết nguồn: 'table' (bảng soạn sẵn), 'ai' (1 lượt AI ước lượng, chỉ khi bật
-- NUTRITION_AI_ESTIMATE), 'none'.
--
-- telegram_user_id theo cùng quy ước các bảng cá nhân khác: id chủ bot (Telegram/Zoom)
-- hoặc internal_user_id ÂM của từng người Zalo - dữ liệu Zalo tách hẳn Telegram.
-- event_key (mã tin gốc + số thứ tự món) chống ghi 2 lần khi webhook/gateway gửi lại.

CREATE TABLE IF NOT EXISTS meals (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    eaten_on DATE NOT NULL,
    meal TEXT NOT NULL DEFAULT 'khac',
    food TEXT NOT NULL,
    portion REAL NOT NULL DEFAULT 1,
    kcal INTEGER,
    kcal_source TEXT NOT NULL DEFAULT 'none',
    source TEXT NOT NULL DEFAULT 'command',
    event_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT meals_meal_check CHECK (meal IN ('sang', 'trua', 'toi', 'phu', 'khac')),
    CONSTRAINT meals_food_check CHECK (length(food) BETWEEN 1 AND 120),
    CONSTRAINT meals_portion_check CHECK (portion > 0 AND portion <= 20),
    CONSTRAINT meals_kcal_check CHECK (kcal IS NULL OR (kcal >= 0 AND kcal <= 20000)),
    CONSTRAINT meals_kcal_source_check CHECK (kcal_source IN ('table', 'ai', 'none')),
    CONSTRAINT meals_source_check CHECK (source IN ('command', 'chat'))
);
CREATE INDEX IF NOT EXISTS idx_meals_user_day ON meals (telegram_user_id, eaten_on);
CREATE INDEX IF NOT EXISTS idx_meals_user_created ON meals (telegram_user_id, created_at);
