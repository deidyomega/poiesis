-- Memory is per-channel (like journal): a fact told to #general shouldn't surface in
-- #pm. Add channel_id; existing rows were written by #general (the only channel with
-- memory + a chat history), so home them there rather than orphan them.
ALTER TABLE memories ADD COLUMN channel_id TEXT;
UPDATE memories SET channel_id = 'general' WHERE channel_id IS NULL;
