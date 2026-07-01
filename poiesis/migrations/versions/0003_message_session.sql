-- Link each agent message to its Agent-SDK session transcript (~/.claude/projects/*/<session_id>.jsonl).
-- Lets us open the full raw transcript (untruncated tool I/O + thinking) for any turn.
ALTER TABLE messages ADD COLUMN session_id TEXT;
