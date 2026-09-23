-- One queued post can now go out to every currently configured Facebook Page.
-- Each (post_id, page_key) pair tracks its own publish status so /fb_ok can
-- retry only the pages that failed without touching pages already POSTED.
-- "default" maps to FACEBOOK_PAGE_ID/FACEBOOK_PAGE_ACCESS_TOKEN; any other
-- page_key (e.g. "2") maps to FACEBOOK_PAGE_ID_<key>/FACEBOOK_PAGE_ACCESS_TOKEN_<key>.
CREATE TABLE IF NOT EXISTS facebook_post_targets (
    post_id BIGINT NOT NULL REFERENCES facebook_post_queue(id) ON DELETE CASCADE,
    page_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    facebook_post_id TEXT,
    permalink_url TEXT,
    error_message TEXT,
    posted_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, page_key),
    CONSTRAINT facebook_post_targets_status CHECK (
        status IN ('PENDING', 'POSTING', 'POSTED', 'ERROR')
    )
);
CREATE INDEX IF NOT EXISTS idx_facebook_post_targets_post ON facebook_post_targets (post_id);
