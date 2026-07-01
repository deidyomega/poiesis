-- Per-channel base_url override so a channel can point at a specific OpenAI-compatible
-- endpoint (OpenRouter vs a local Ollama) independently of the env default — drives the
-- in-UI model picker for #spice.
ALTER TABLE channels ADD COLUMN base_url TEXT;
