-- Channels can run on a different backend than the Claude Agent SDK.
-- 'claude' (default) = Claude Agent SDK; 'openai' = OpenAI-compatible chat completions.
ALTER TABLE channels ADD COLUMN engine TEXT NOT NULL DEFAULT 'claude';
