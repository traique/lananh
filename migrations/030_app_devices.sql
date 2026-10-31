-- Thiết bị đọc màn hình thông tin (app Android trong android/, services/app_devices.py).
-- Chủ bot gõ /app moi <tên> trên Telegram -> 1 dòng chờ ghép với MÃ GHÉP 8 ký tự hết
-- hạn sau vài phút; app gửi mã lên POST /api/v1/app/pair -> nhận token dài, dòng
-- chuyển sang đã ghép (xoá mã ghép). Chỉ lưu SHA-256 của mã ghép và token, không lưu
-- bản gốc (không cần đọc lại) - lộ DB không lộ token. Thu hồi: revoked_at.
CREATE TABLE IF NOT EXISTS app_devices (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    pair_code_hash TEXT UNIQUE,
    pair_expires_at TIMESTAMPTZ,
    token_hash TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    paired_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    CONSTRAINT app_devices_name_check CHECK (char_length(name) BETWEEN 1 AND 40),
    CONSTRAINT app_devices_state_check CHECK (
        (token_hash IS NULL AND pair_code_hash IS NOT NULL AND pair_expires_at IS NOT NULL)
        OR (token_hash IS NOT NULL AND pair_code_hash IS NULL)
        OR revoked_at IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS app_devices_user_idx ON app_devices (telegram_user_id, created_at);
