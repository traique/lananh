ALTER TABLE shopee_affiliate_links
ADD COLUMN IF NOT EXISTS canonical_key TEXT;

CREATE INDEX IF NOT EXISTS idx_shopee_affiliate_links_canonical
ON shopee_affiliate_links (account_id, canonical_key, updated_at DESC)
WHERE canonical_key IS NOT NULL;
