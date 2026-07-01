-- Phase 0 initial schema.

CREATE TABLE channels (
    id           TEXT PRIMARY KEY,          -- slug, e.g. 'general'
    name         TEXT NOT NULL,             -- display name
    soul_path    TEXT,                      -- markdown soul file, relative to repo souls/
    model        TEXT,                      -- model id; NULL = SDK default
    cwd          TEXT,                      -- bound repo path for coding; NULL = none
    allowed_tools TEXT,                     -- JSON array of tool names; NULL = default set
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE messages (
    id          TEXT PRIMARY KEY,
    channel_id  TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,              -- user | agent | system
    content     TEXT NOT NULL DEFAULT '',
    segments    TEXT,                       -- JSON array of {type,...}
    cancelled   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_messages_channel ON messages(channel_id, created_at);

CREATE TABLE memories (
    id          TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE journal (
    id          TEXT PRIMARY KEY,
    channel_id  TEXT,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE deploys (
    id           TEXT PRIMARY KEY,
    channel_id   TEXT,
    message_id   TEXT,                      -- chat message that shows this deploy
    summary      TEXT,
    status       TEXT NOT NULL,             -- requested|committing|restarting|health_check|live|rolled_back|failed
    reason       TEXT,                      -- rollback / failure detail
    target_sha   TEXT,                      -- commit being deployed
    rollback_sha TEXT,                      -- last-green to revert to on failure
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_deploys_created ON deploys(created_at);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,                 -- 'theme' | 'project' | 'last_green_sha'
    value TEXT NOT NULL                     -- JSON
);
