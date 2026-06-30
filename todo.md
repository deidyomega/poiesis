# Glitch Core — TODO

## High Priority — Memory System Overhaul
- [ ] **Richer journal entries with conversation context.** The `write_journal` builtin tool currently captures a one-line observation. It should also capture the last ~5 messages from the conversation as context. Store these in a `context_messages: list[str]` field on the JournalEntry so the compaction pipeline knows WHY the observation was made, not just WHAT it says. The journal becomes a mini-snapshot of the conversation moment.
- [ ] **Compaction produces paragraph-length memories, not one-liners.** Update `compaction/prompts.py` to instruct the summarization agent to produce rich, contextual paragraphs — not isolated facts. Example: instead of "User likes Python" → "User is an experienced Python developer who prefers async patterns and Pydantic for data validation. Has built distributed AI systems and values simplicity over cleverness." The `CompactedMemory` model may need a `summary` field vs `content`, or just enforce paragraph-length via the prompt.
- [ ] **Post-compaction memory merging pass.** After the summarization phase, run a second pass that finds highly related memories and combines them. Five memories about coding preferences → one comprehensive paragraph. Could be a separate LLM call with all new + existing memories asking "which of these should be merged?" Returns merge groups, then a final call synthesizes each group into one memory.
- [ ] **Compaction pipeline gets full message context.** The compaction summarization agent should receive the journal's `context_messages` alongside the observation text. This gives the LLM the surrounding conversation when deciding importance, confidence, and how to phrase the memory. Update `build_compaction_prompt()` in `prompts.py` to include context.
- [ ] **Compaction agent gets all existing memories in its system prompt.** Currently loaded as context in the per-batch prompt, but should be more prominent — the agent needs to know what already exists to avoid duplicates and to merge/update intelligently.

## High Priority — Daily Log (Temporal Grounding)
- [ ] **Daily log documents.** New Firestore collection `/daily_logs/{YYYY-MM-DD}`. Each doc accumulates a narrative summary of the day's conversations. Appended to every ~3 hours by a summarizer task. Gives the AI a sense of "today" vs "yesterday." The daily log is NOT raw messages or journal entries — it's a processed narrative diary.
- [ ] **3-hour digest task.** New daemon task (like compaction_scheduler). Every 3 hours, reads recent messages across all sessions since last digest, sends to a summarizer agent with prompt: "Write a brief diary entry for what happened in the last few hours." Appends the result to today's daily log doc. Lightweight — a few sentences per digest, not a full compaction.
- [ ] **Daily log in the system prompt.** The dynamic system prompt includes today's log and a condensed version of yesterday's. Fresh day = empty log = AI knows nothing has happened yet today. Prevents stale context bleeding ("go get ramen" when that was last night). Format: `## Today (Friday, April 4)\n{today_log}\n\n## Yesterday\n{yesterday_summary}`.
- [ ] **Day rollover.** Configurable timezone in `/meta/project` (default UTC). At rollover, today's log closes. Yesterday's log gets condensed into a shorter summary (one paragraph). The full version stays in Firestore for reference. Weekly summaries could be generated from daily logs.
- [ ] **Daily log → compaction pipeline.** Daily logs feed into the existing compaction pipeline as source material alongside journals. The compaction pipeline extracts lasting facts from daily logs into core_memories. Daily logs are the short-term narrative; core_memories are the long-term knowledge.
- [ ] **Relationship to existing journal.** The `write_journal` tool captures in-the-moment observations (micro). The daily log captures the narrative arc of the day (macro). Both feed compaction. The journal is "what the AI noticed" — the daily log is "what happened."

## High Priority — Memory Decay & Reinforcement (Forgetting Curve)
- [ ] **Memory strength model.** Each core memory gets a `strength` score based on recency, frequency, and importance. Unreinforced memories fade over time (exponential decay, ~30 day half-life). Frequently referenced memories stay strong. Formula: `strength = importance * log2(access_count + 1) * e^(-λ * days_since_last_access)`. New fields on CoreMemory: `access_count: int`, `last_accessed: datetime`, `strength: float`.
- [ ] **Reinforcement detection.** When the compaction pipeline encounters a journal entry that overlaps an existing core memory, that's a reinforcement — bump `access_count` and reset `last_accessed` instead of creating a duplicate. The merge pass already detects related memories; extend it to distinguish "reinforce existing" from "merge into new."
- [ ] **Strength-aware memory loading.** Instead of loading all memories equally into the system prompt, sort by `current_strength()` computed on-the-fly. Options: hard cutoff (top N), soft fade (weak memories prefixed with "(faint memory)"), or tiered (strong in system prompt, weak available via a `recall` tool that the AI searches on demand — like needing a moment to remember).
- [ ] **Dormancy, not deletion.** Faded memories go dormant (below a strength threshold) but are never deleted. A single reinforcement brings them back. Just like human memory — you haven't forgotten, you just need a trigger.
- [ ] **Tunable decay parameters.** Half-life, decay rate, reinforcement boost, and strength threshold should be configurable in `/meta/compaction_config`. Different instances might want different memory persistence (a daily companion vs a work assistant).

## High Priority — Chat UX: Thinking + Tool Trace
- [ ] **Structured message content with collapsible sections.** Currently agent responses are flat text. They should be structured as segments: thinking (collapsible), visible text, tool calls (collapsible with args/results), more thinking (collapsible), final text. The PydanticAI message trace already has this data (`all_messages` with `part_kind` of `text`, `tool-call`, `tool-return`). Instead of storing a flat `content` string, store the full trace as structured segments in the message's `metadata.segments` field.
- [ ] **Chat template renders segments.** Each segment type has its own rendering: `text` → rendered as markdown (visible). `thinking` → collapsible `<details>` block, dimmed, labeled "Thinking...". `tool-call` → collapsible block showing tool name + args summary. `tool-return` → included in the tool-call collapsible with the result. The streaming flow: text streams in visibly → thinking dots during tool execution → tool call block appears (collapsed) → more text streams in.
- [ ] **Daemon writes structured segments.** Update `_handle_message` to parse the PydanticAI trace after completion and store segments rather than (or alongside) the flat `content` string. The streaming phase writes `content` progressively as before. After the follow-up completes, parse `all_messages` into segments and write them to the message doc. The chat template uses segments if present, falls back to flat content for old messages.
- [ ] **Typewriter effect works per-segment.** Each visible text segment gets its own typewriter animation. Collapsible sections appear instantly (no animation needed — they're hidden by default).

## High Priority — Ouroboros Self-Debugging (Agent Introspection)
- [x] **`create_page` and `create_tool` return detailed results.** Structured feedback with all validation failures grouped by stage, fixability flags, rollback SHA, and artifact path. `_format_promotion_result()` shared helper.
- [x] **`system_inspect` builtin tool.** Methods: `recent_errors(n)`, `list_routes()`, `check_template(name)`, `test_route(path)`. Read-only introspection into the running system.
- [x] **Post-promotion HTTP verification.** `create_page` auto-tests the route after promotion — returns status code and error body if 500.
- [x] **Error ring buffer.** Daemon keeps last 100 errors in memory. `system_inspect(recent_errors)` reads them. Also written to Firestore as error-type run logs.
- [x] **write_page collects ALL validation failures.** No more early returns — syntax, page patterns, and template errors all reported together.
- [ ] **Pre-flight checks before create_page.** Before writing files, verify: required imports are available, template base exists, no route prefix conflicts with existing pages. Return issues as warnings before attempting promotion.
- [ ] **Workspace file verification.** After `workspace_write`, the agent should be able to verify the file exists and read it back. The workspace tools already support this (`workspace_read`), but the agent needs prompting to chain: write → read back → verify.

## High Priority — Worker/Workspace Locality
- [ ] **Agent-to-worker binding.** Add a `preferred_worker: str = "main"` field to `AgentConfig`. The `/agents/{id}/edit` page gets a dropdown of registered workers (from `/workers/` collection) to select which machine this agent runs on. The daemon routes messages for that agent to the specified worker. Default is `"main"`. This is simpler than session-level pinning — the agent itself knows where it lives, and all its sessions inherit that. Fixes the workspace locality problem: coder on MacBook always uses MacBook's workspace.
- [ ] **Workspace awareness across workers (future).** Long-term options: (1) workspace files stored in Firestore as blobs (simple but size limits), (2) workspace sync over Tailscale between workers, (3) shared storage mount (NFS/S3). For now, worker pinning solves the immediate problem. Cross-worker workspace sync is a Phase 4+ feature.
- [ ] **Worker capability in session display.** The chat header should show which worker/machine the session is running on so the user knows where their files live. e.g. "coder · macbook" vs "coder · aws-gpu".

## High Priority — Architecture: Process Separation & Unified Worker Model
**Core principle:** The webapp is "just another client." It reads/writes Firestore, renders HTML. It does NOT run agents, execute tools, or do compaction. Every running daemon instance is a worker.

- [ ] **Separate web server from daemon.** Web process = FastAPI/uvicorn, serves pages, reads Firestore. Daemon process = agent listener, tool execution, background tasks. If the web crashes, the brain keeps running. `glitch start` launches both; `glitch start --web-only` and `glitch start --daemon-only` for separate control.
- [ ] **Reload signal via Firestore.** After SafeFileWriter promotes a page, daemon writes to `/meta/reload_trigger` with timestamp. Web process watches via `on_snapshot` and calls `reload_custom_pages()`. No direct IPC needed.
- [ ] **Unified worker model.** Collapse "daemon" and "worker" into one concept. Every running instance registers as a worker with capabilities derived from its `.env`:
  - Hardware: `gpu`, `local`, `tailnet` (existing)
  - LLM access: `llm:anthropic`, `llm:google`, `llm:openai`, `llm:ollama` (derived from which API keys are set)
  - Baseline: `firestore` (every instance has this)
- [ ] **Singleton tasks as claimable docs.** Compaction, reaper, daily log digest, reminder scheduler — all become scheduled task docs in Firestore with `required_capabilities`. Any worker with matching capabilities can claim. First to claim wins (reuse existing worker claim protocol). No leader election needed.
  - Compaction: requires `llm:<compaction_model_provider>`
  - Reaper: requires `firestore` only
  - Daily log digest: requires `llm:<digest_model_provider>`
  - Reminder scheduler: requires `firestore` only
- [ ] **Each process owns its own Firestore client.** No shared state. Both connect independently.

## High Priority — Infrastructure
- [ ] Proper cron parsing for compaction scheduler (currently interval-based)
- [ ] Firebase Auth — require login for browser Firestore access (currently read-only rules)
- [ ] Migration runner (`migrations/runner.py`) — sequential, idempotent schema migrations

## High Priority — Deployment & Installer (`glitch` CLI)
- [ ] **Create `glitch_core/cli.py`** — Click-based CLI (pyproject.toml already declares `glitch = "glitch_core.cli:cli"`). Subcommands: `glitch bootstrap`, `glitch start`, `glitch stop`, `glitch restart`, `glitch update`, `glitch status`.
- [ ] **`glitch bootstrap` — idempotent, incremental, debuggable installer.** Not a "run once" wizard — it's the single entry point for "make this instance healthy." Every step checks current state first and skips if already done. Re-running is always safe. Supports CLI flags to override/fix individual values without re-running the whole flow.
  - **CLI flags:** `--firebase-project`, `--ANTHROPIC_API_KEY`, `--GEMINI_API_KEY`, `--OPENAI_API_KEY`, `--credentials=/path/to/key.json`, `--fix` (re-validate and repair everything), `--non-interactive` (CI/scripting mode, fail on missing values instead of prompting)
  - **Steps (each idempotent):**
    1. Check dependencies (Python ≥3.11, uv, Node.js, firebase-tools) — install prompts for missing, or error in non-interactive mode
    2. Check `~/.glitch/.env` — create if missing, merge CLI flags into existing (update single keys without clobbering others)
    3. Validate Firebase project exists and Firestore is enabled — prompt to create/enable if not
    4. Check service account credentials — locate, validate, copy to `~/.glitch/credentials.json`
    5. Validate API keys with a real test call (Anthropic: list models, etc.) — report which providers are live
    6. `firebase login` check + `firebase use <project>` if needed
    7. Seed Firestore — skip collections/docs that already exist (don't clobber a running instance!)
    8. Deploy Firestore rules + indexes (always re-deploy — idempotent)
    9. Quiz user on basic memories (name, preferences) → seed as core_memories (skip if memories already exist)
    10. Optionally customize router soul, set up systemd/launchd
    11. Print health summary: what's working, what's missing, what to do next
  - **Recovery mode (`--fix`):** Re-runs all validation steps, re-deploys rules, checks Firestore schema integrity, reports and fixes drift. "My instance is broken, heal it."
  - **The bootstrap IS an agent.** Pass `--ANTHROPIC_API_KEY` and the bootstrap spins up a Sonnet agent *before anything else exists*. The agent has tools to check dependencies, validate credentials, test connections, read/write the `.env`, seed Firestore, and deploy rules. The user just talks to it: "my Firestore isn't connecting" → agent checks credentials, tests the connection, diagnoses the issue, guides you through the fix. First-run is a guided conversation: "Who is this instance for?" → agent seeds memories, writes a soul, configures the personality. The API key is the spark — it gives Glitch a brain before it even has a body. The bootstrap conversation IS the first conversation with your AI.
- [ ] **`glitch start`** — launches daemon (and eventually separate web process). Supports `--foreground` for dev and `--daemon` for production (via systemd/nohup).
- [ ] **`glitch stop` / `glitch restart`** — requires running as a system service (systemd/launchd) or PID file.
- [ ] **`glitch update`** — `git pull` + `uv sync` + run pending migrations + restart daemon. Safe rolling update.
- [ ] **`glitch status`** — show running processes, last heartbeat, active sessions, Firestore connectivity, API key status (valid/expired/missing).
- [ ] **`glitch doctor`** — diagnostic tool: check all dependencies, validate `.env`, test Firestore connection, test API keys, verify rules are deployed, check for common issues.
- [ ] **Bootstrap should set up systemd/launchd service** for daemon auto-start on boot.

## High Priority — Stop Generation
- [x] **Stop/cancel button for in-progress generation.** Any client writes `cancel_generation: true` on the session doc. Daemon checks during each flush cycle (~600ms). When detected: breaks streaming, finalizes with partial content + `cancelled: true`, clears the flag. Firestore rules allow clients to update only the `cancel_generation` field on sessions.
- [x] **Chat UI stop button.** Send button swaps to red Stop button while streaming. Stop writes `cancel_generation: true` to the session doc via Firebase client SDK. Reverts to Send when `streaming: false` arrives. Cancelled messages show "Generation stopped" badge.
- [ ] **Client integration doc update.** Document the cancel protocol: write `cancel_generation: true` to the session doc. Add to `docs/client_integration.md` so desktop companion and mobile apps can implement stop buttons.

## High Priority — Chat Session Management
- [x] **Delete session.** Implemented in chat.py with subcollection cleanup + sidebar button.
- [x] **Clear session history.** "Clear" button in chat header, deletes messages + run_logs, keeps session alive.

## High Priority — Network Resilience
- [ ] **Graceful handling of network disconnects.** When the laptop sleeps or ISP drops, the `on_snapshot` gRPC streams die. The Firestore Python SDK's `on_snapshot` runs in background threads and throws exceptions that aren't caught cleanly. Needed: (1) wrap `on_snapshot` callbacks with error handlers that log but don't crash, (2) detect disconnection (consecutive errors or gRPC UNAVAILABLE status), (3) attempt re-subscription after a backoff delay (5s, 10s, 30s, 60s), (4) the daemon should never exit due to a network error — only due to explicit shutdown. The agent listener, worker loop, and agent config watcher all use `on_snapshot` and all need this treatment.
- [ ] **Health check / status indicator.** The web UI should show connection status — green when the daemon is processing, yellow when reconnecting, red when disconnected. Could be a simple Firestore doc the daemon heartbeats to, and the browser watches. The heartbeat already exists at `/workers/{id}` — the web UI could watch it and show status.
- [ ] **Daemon auto-recovery.** If all `on_snapshot` watchers die, the daemon should detect this (e.g. no messages processed in 5 minutes despite active sessions) and re-initialize the watchers. Not a full restart — just re-subscribe to Firestore.

## Medium Priority
- [ ] Logfire integration for PydanticAI observability
- [ ] Compaction: smarter batching by topic similarity
- [ ] Logo upload → color palette extraction for theme generation (Pillow)
- [ ] Firestore security rules: structural validation (workers only write own heartbeat, valid status transitions)
- [ ] `glitch pages list` / `glitch pages rollback` CLI commands
- [ ] OpenAI-compatible base URL override (`GLITCH_OPENAI_COMPAT_BASE_URL`) for LM Studio / vLLM
- [ ] Chat: "clear session" / "new conversation" button
- [ ] Dashboard: show pending sub-tasks count
- [ ] Git init as part of bootstrap for Ouroboros rollback safety

## Low Priority / Polish
- [ ] SSH execution over Tailnet (sysadmin agent real implementation)
- [ ] Workspace script execution in Docker containers (production hardening)
- [ ] nsjail/bubblewrap sandbox for Ouroboros subprocess validation
- [ ] Multi-file atomic promotions beyond page pairs
- [ ] Agent config versioning / history in Firestore
- [ ] Chat: message search
- [ ] Memories page: pagination / infinite scroll

## Completed
- [x] Ouroboros: tool generation pipeline (SafeFileWriter + create_tool)
- [x] Ouroboros: page generation pipeline (SafeFileWriter + create_page)
- [x] Ouroboros: AI theme generation (theme_generator.py)
- [x] Ouroboros: workspace (free-form user zone)
- [x] Ouroboros: circuit breaker (auto-revert bad promotions)
- [x] Workers: distributed sub-agent dispatch
- [x] Workers: claim protocol
- [x] Workers: reaper for stale tasks
- [x] Chat: streaming responses with typewriter effect
- [x] Chat: markdown rendering (marked.js with GFM + tables)
- [x] Chat: thinking animation
- [x] Multi-agent sessions (direct chat with any agent)
- [x] Dynamic Firestore-driven agents with web UI CRUD
- [x] Unified tool system (builtin + dynamic, any tool on any agent)
- [x] on_snapshot real-time listeners (replaced polling)
- [x] Firestore quota optimization (~1.5K reads/day idle)
- [x] Run logs page with full PydanticAI trace viewer
- [x] Compaction pipeline with rollback
- [x] Theme switching (preset themes)
- [x] Memory review queue removed (direct to core_memories)
- [x] Agent management tools (list, read, create, update, delete agents at runtime)
- [x] Custom page validation (TemplateResponse signature, await, Jinja2Templates instance)
- [x] Hot-reload router mounting for custom pages
- [x] TemplateNotFound → 404 handler for deleted custom pages
- [x] Chat UX: Thinking + Tool Trace (structured segments with collapsible thinking/tool calls)
- [x] Stop generation (cancel button, session-level protocol, any-client compatible)
- [x] Error surfacing in chat (type, message, traceback link) + error run logs
- [x] Ouroboros self-debugging (structured feedback, system_inspect, post-promotion verification, error ring buffer)
