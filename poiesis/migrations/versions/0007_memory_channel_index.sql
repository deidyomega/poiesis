-- Index the per-channel memory lookup (list_memories/search_memories run every turn),
-- matching the precedent set by idx_messages_channel in 0001.
CREATE INDEX IF NOT EXISTS idx_memories_channel ON memories(channel_id);
