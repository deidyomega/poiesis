-- Turn lifecycle state, so generation is decoupled from the browser connection:
-- 'generating' (a detached task is producing it), 'done', 'cancelled', 'error'.
-- The DB is the source of truth — the browser subscribes/reconnects to it.
ALTER TABLE messages ADD COLUMN status TEXT NOT NULL DEFAULT 'done';
