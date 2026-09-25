-- Re-applies the facebook_post_targets table under a fresh version number.
--
-- Why this file exists: an earlier draft of the multi-page Facebook feature
-- shipped a DIFFERENT migration under the filename "005_...sql" (it only did
-- ALTER TABLE ... ADD COLUMN page_key, no CREATE TABLE). If that draft was
-- ever deployed, Postgres already has a schema_migrations row for
-- version = 5, keyed only by the integer version number — so when the file
-- named "005" was later swapped for one that creates facebook_post_targets,
-- the migration runner saw "version 5 already applied" and silently skipped
-- it, and facebook_post_targets was never created. Shipping the same
-- CREATE TABLE under a brand-new version number (006) guarantees it runs
-- here regardless of that history. Every statement below is idempotent, so
-- this is a harmless no-op on a database where 005 already created the table
-- correctly.
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
