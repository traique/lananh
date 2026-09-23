-- Allow more than one destination Facebook Page. Each Zalo source group is
-- pinned to a page_key ("default", "2", "3", ...); FACEBOOK_PAGE_ID/
-- FACEBOOK_PAGE_ACCESS_TOKEN cover "default" and FACEBOOK_PAGE_ID_<key>/
-- FACEBOOK_PAGE_ACCESS_TOKEN_<key> cover any additional page. Existing rows
-- default to "default" so current single-page setups keep working untouched.
ALTER TABLE zalo_facebook_groups ADD COLUMN IF NOT EXISTS page_key TEXT NOT NULL DEFAULT 'default';
ALTER TABLE facebook_post_queue ADD COLUMN IF NOT EXISTS page_key TEXT NOT NULL DEFAULT 'default';
