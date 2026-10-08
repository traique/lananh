-- Affiliate links go to the first comment of each Page post; NULL = not commented yet.
ALTER TABLE facebook_post_targets ADD COLUMN IF NOT EXISTS comment_id TEXT;
