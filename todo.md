# Glitch Core — TODO

## High Priority — Memory System Overhaul
- [ ] **Richer journal entries with conversation context.** The `write_journal` builtin tool currently captures a one-line observation. It should also capture the last ~5 messages from the conversation as context. Store these in a `context_messages: list[str]` field on the JournalEntry so the compaction pipeline knows WHY the observation was made, not just WHAT it says. The journal becomes a mini-snapshot of the conversation moment.
- [ ] **Compaction produces paragraph-length memories, not one-liners.** Update `compaction/prompts.py` to instruct the summarization agent to produce rich, contextual paragraphs — not isolated facts. Example: instead of "User likes Python" → "User is an experienced Python developer who prefers async patterns and Pydantic for data validation. Has built distributed AI systems and values simplicity over cleverness." The `CompactedMemory` model may need a `summary` field vs `content`, or just enforce paragraph-length via the prompt.
- [ ] **Post-compaction memory merging pass.** After the summarization phase, run a second pass that finds highly related memories and combines them. Five memories about coding preferences → one comprehensive paragraph. Could be a separate LLM call with all new + existing memories asking "which of these should be merged?" Returns merge groups, then a final call synthesizes each group into one memory.
- [ ] **Compaction pipeline gets full message context.** The compaction summarization agent should receive the journal's `context_messages` alongside the observation text. This gives the LLM the surrounding conversation when deciding importance, confidence, and how to phrase the memory. Update `build_compaction_prompt()` in `prompts.py` to include context.
- [ ] **Compaction agent gets all existing memories in its system prompt.** Currently loaded as context in the per-batch prompt, but should be more prominent — the agent needs to know what already exists to avoid duplicates and to merge/update intelligently.

## High Priority — Chat UX: Thinking + Tool Trace
- [ ] **Structured message content with collapsible sections.** Currently agent responses are flat text. They should be structured as segments: thinking (collapsible), visible text, tool calls (collapsible with args/results), more thinking (collapsible), final text. The PydanticAI message trace already has this data (`all_messages` with `part_kind` of `text`, `tool-call`, `tool-return`). Instead of storing a flat `content` string, store the full trace as structured segments in the message's `metadata.segments` field.
- [ ] **Chat template renders segments.** Each segment type has its own rendering: `text` → rendered as markdown (visible). `thinking` → collapsible `<details>` block, dimmed, labeled "Thinking...". `tool-call` → collapsible block showing tool name + args summary. `tool-return` → included in the tool-call collapsible with the result. The streaming flow: text streams in visibly → thinking dots during tool execution → tool call block appears (collapsed) → more text streams in.
- [ ] **Daemon writes structured segments.** Update `_handle_message` to parse the PydanticAI trace after completion and store segments rather than (or alongside) the flat `content` string. The streaming phase writes `content` progressively as before. After the follow-up completes, parse `all_messages` into segments and write them to the message doc. The chat template uses segments if present, falls back to flat content for old messages.
- [ ] **Typewriter effect works per-segment.** Each visible text segment gets its own typewriter animation. Collapsible sections appear instantly (no animation needed — they're hidden by default).

## High Priority — Ouroboros Self-Debugging (Agent Introspection)
- [ ] **`create_page` and `create_tool` return detailed results.** Currently returns "success" or a vague error. Should return the full SafeFileWriter pipeline trace: which validation stages passed/failed, import errors with full traceback, the actual stdout/stderr from the subprocess import test. The `PromotionResult` model already has `validation_failures` — the tool response should format ALL of them, not just the first.
- [ ] **New builtin tool: `system_inspect`.** Gives agents visibility into the running system. Methods: `list_routes()` — all registered FastAPI routes, `list_loaded_modules()` — what's imported, `check_template(name)` — verify a template is loadable, `get_recent_errors(n)` — last N errors from the daemon log (ring buffer in memory). This is READ-ONLY — no system modification.
- [ ] **New builtin tool: `system_test`.** Agent can make HTTP requests to its own web server to verify routes work. `test_route(method, path)` → returns status code + response body (truncated). Essentially `httpx.get("http://localhost:8080/my_page")` from within the daemon. Lets the agent verify its create_page output without the user having to check.
- [ ] **Pre-flight checks before create_page.** Before writing files, verify: required imports are available (`httpx`, etc.), template base exists, no route prefix conflicts with existing pages. Return issues as warnings before attempting promotion.
- [ ] **Post-promotion verification.** After `create_page` succeeds, automatically call the new route and report whether it returns 200. If it 500s, include the error in the tool response so the agent can fix and retry.
- [ ] **create_page/create_tool return structured feedback.** Instead of flat strings, return a structured result the agent can parse: `{"success": true, "route_registered": "/comfyui", "validation": {"syntax": "passed", "import": "passed", "ast_scan": "passed"}, "test_request": {"status": 200}}` or `{"success": false, "validation": {"syntax": "passed", "import": "failed: No module named 'httpx'"}, "fixable": true}`.
- [ ] **Error log ring buffer.** Daemon keeps last 100 errors in memory (not Firestore). The `system_inspect` tool can read them. This gives agents access to runtime errors without needing Firestore reads or log file access.
- [ ] **Workspace file verification.** After `workspace_write`, the agent should be able to verify the file exists and read it back. The workspace tools already support this (`workspace_read`), but the agent needs prompting to chain: write → read back → verify.

## High Priority — Worker/Workspace Locality
- [ ] **Agent-to-worker binding.** Add a `preferred_worker: str = "main"` field to `AgentConfig`. The `/agents/{id}/edit` page gets a dropdown of registered workers (from `/workers/` collection) to select which machine this agent runs on. The daemon routes messages for that agent to the specified worker. Default is `"main"`. This is simpler than session-level pinning — the agent itself knows where it lives, and all its sessions inherit that. Fixes the workspace locality problem: coder on MacBook always uses MacBook's workspace.
- [ ] **Workspace awareness across workers (future).** Long-term options: (1) workspace files stored in Firestore as blobs (simple but size limits), (2) workspace sync over Tailscale between workers, (3) shared storage mount (NFS/S3). For now, worker pinning solves the immediate problem. Cross-worker workspace sync is a Phase 4+ feature.
- [ ] **Worker capability in session display.** The chat header should show which worker/machine the session is running on so the user knows where their files live. e.g. "coder · macbook" vs "coder · aws-gpu".

## High Priority — Architecture: Process Separation
- [ ] **Separate FastAPI web server from the daemon process.** Currently both run in one process via `asyncio.gather`. A bad Ouroboros page promotion can crash FastAPI which kills the entire daemon (agent listener, workers, heartbeat, everything). Separation means: (1) Daemon process — agent listener, worker loop, heartbeat, compaction, reaper. The brain. Never affected by UI changes. (2) Web process — FastAPI/uvicorn. Hot-reloads pages. If it crashes, auto-restart without affecting the daemon. The web process is "just a client" that reads/writes Firestore, same as any other client.
- [ ] **Inter-process communication for hot-reload.** After the daemon's SafeFileWriter promotes a page, the web process needs to know to call `PageEngine.reload_custom_pages()`. Solution: daemon writes a signal to Firestore (e.g. `/meta/reload_trigger` with a timestamp), web process watches it via `on_snapshot` and reloads when it changes. No direct IPC needed.
- [ ] **`glitch start` launches both processes.** Could use subprocess, or `glitch start` launches daemon and `glitch start --web-only` launches just the web server. For development, run them separately. For production, `glitch start` manages both (or systemd manages them as separate services).
- [ ] **Each process owns its own Firestore client.** No shared state between processes. Both connect independently. Doubles Firestore connections but they're cheap and stateless.

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
- [ ] **Stop/cancel button for in-progress generation.** Client writes `cancelled: true` on the user message doc that triggered the generation. The daemon checks for this flag during the streaming loop (every N chunks or via a separate lightweight watcher). When detected: break out of `stream_text()`, finalize the message with whatever content has been streamed so far, set `cancelled: true` on the agent response message, skip the follow-up `run()`. The client shows a "Stopped" indicator on the message.
- [ ] **Chat UI stop button.** Replace the Send button with a Stop button while a message is streaming (detect via `streaming: true` on the latest agent message). Stop button writes `cancelled: true` to the user message that triggered the response. Button reverts to Send when streaming ends or cancellation completes.
- [ ] **Client integration doc update.** Document the cancel protocol: write `cancelled: true` to the user message doc, daemon stops generation. Add to `docs/client_integration.md` so desktop companion and mobile apps can implement stop buttons.

## High Priority — Chat Session Management
- [ ] **Delete session.** Button on the chat sidebar per session. Deletes the session doc + all messages/sub_tasks/run_logs subcollections. Redirects to `/chat` (which opens or creates the default session). Use Firebase CLI batch delete pattern or iterate subcollections. HTMX confirm modal before deleting.
- [ ] **Clear session history.** "Clear messages" button in the chat header. Keeps the session alive (same agent, same session ID) but deletes all messages in the subcollection. Fresh context window without losing the session. Useful when the agent gets "stuck" on a concept from old messages.

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
