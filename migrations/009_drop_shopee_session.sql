-- Shopee Affiliate browser automation was removed; drop the stored login session.
DELETE FROM settings WHERE key = 'shopee:affiliate:storage_state:v1';
