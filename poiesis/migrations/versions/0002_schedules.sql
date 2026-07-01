-- Scheduler: durable cron/interval tasks that fire a prompt into a channel.

CREATE TABLE schedules (
    id               TEXT PRIMARY KEY,
    channel_id       TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'daily',   -- 'daily' | 'interval'
    at_hour          INTEGER,                         -- daily: hour (0-23) in tz
    at_minute        INTEGER NOT NULL DEFAULT 0,      -- daily: minute
    interval_seconds INTEGER,                         -- interval: seconds between runs
    tz               TEXT NOT NULL DEFAULT 'UTC',
    notify           INTEGER NOT NULL DEFAULT 1,      -- mark resulting message as a notification
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_run         TEXT,                            -- ISO UTC of last fire
    created_at       TEXT NOT NULL
);

-- Proactive/scheduled messages carry a notification flag for the client.
ALTER TABLE messages ADD COLUMN notification INTEGER NOT NULL DEFAULT 0;
