-- Cách xưng hô riêng cho từng thành viên Zalo (/zalopair <id> [tên] [chị|cô|...]):
-- persona, lời chào khi pair và câu báo lỗi gọi người đó đúng như chủ bot đặt.
-- NULL = mặc định của persona (gọi "anh", theo trí nhớ riêng nếu có ghi cách
-- xưng hô khác). Danh sách khớp core/address.py::ADDRESS_FORMS.
ALTER TABLE zalo_users ADD COLUMN IF NOT EXISTS address_form TEXT;
ALTER TABLE zalo_users DROP CONSTRAINT IF EXISTS zalo_users_address_form_check;
ALTER TABLE zalo_users ADD CONSTRAINT zalo_users_address_form_check
    CHECK (address_form IS NULL OR address_form IN ('anh', 'chị', 'cô', 'chú', 'bạn', 'em'));
