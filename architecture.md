# Glitch Core -- Architecture Specification

> Last updated: 2026-04-02 | Version: 0.1.0

## Project Overview

Glitch Core is a distributed, stateful, self-improving AI entity. It is an open-source, self-hosted personal AI system where every installation is single-tenant -- the user creates their own Firebase project, runs the daemon on their own hardware, and owns all their data. There is no central server, no shared infrastructure, no published pip package, and no SaaS component.

The system is designed to be malleable. The AI can extend its own tools, generate new UI pages, retheme the interface, and improve its own capabilities at runtime -- all without a restart or rebuild step. A non-technical user can say "make the app pink and gothic" or "add a page that counts how many times I mentioned my dog" and the system generates and hot-reloads the result.

### Core Philosophy

- **Firebase is storage, not compute.** Firestore acts as the pub/sub event bus, state store, and memory layer. All code execution happens locally -- wherever "local" means to that user (AWS, home lab, laptop).
- **No Cloud Functions.** Every process runs inside the daemon or worker loops. If the daemon is down, nothing runs -- and that's a feature, not a bug. No orphaned functions burning tokens in the background if the user walks away.
- **Single-tenant by design.** Each user creates their own Firebase project. There is no multi-tenant scoping, no shared databases, no API key custody. One project, one user, total ownership.
- **The entire application surface is writable by the AI.** Tools (Python modules), UI pages (FastAPI routes + Jinja2 templates), and themes (Firestore documents) are all in the Ouroboros blast radius. The only thing that doesn't change at runtime is the engine itself.
- **Firestore is the source of truth for agent configs.** The YAML file (`glitch_core.yaml`) is seed data only -- used once during `glitch bootstrap` to populate `/agents/` in Firestore. After that, all agent configuration lives in Firestore and is editable via the web UI. Changes take effect immediately via `on_snapshot` watchers without any restart.

### Mental Model

The architecture uses an anatomical metaphor that maps to real system boundaries:

- **The Nervous System** -- Firestore. The database schema, pub/sub event bus, real-time sync to clients via `on_snapshot`.
- **The Subconscious** -- Memory compaction pipeline. Background distillation of raw conversation observations into long-term core memories.
- **The Hands** -- Execution layer. The daemon that listens for messages, runs agents, executes tools.
- **The Hive** -- Sub-agent system. Horizontal scaling via Firestore as a task queue, with heterogeneous workers (cloud APIs, local models, GPU rigs).
- **The Ouroboros** -- Self-improvement loop. The system can write, validate, and hot-reload its own tools, UI pages, and themes.

---

## Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| LLM Engine | PydanticAI | Type-safe, schema-enforced agent definitions with structured outputs and streaming |
| Model Providers | Anthropic, Google (Gemini), OpenAI, Mistral, Groq, Ollama | Six providers supported; selection per-agent via `model` field (e.g. `anthropic:claude-sonnet-4-20250514`) |
| State & Pub/Sub | Firebase Firestore | Real-time `on_snapshot` listeners, free client sync, generous free tier |
| Web UI | FastAPI + Jinja2 + HTMX + Tailwind CDN | No build step -- the entire UI is hot-reloadable by the Ouroboros system |
| Configuration | Pydantic + PydanticSettings + YAML (seed only) | Typed config with `.env` files for secrets, Firestore for runtime agent config |
| Network Mesh | Tailscale | Secure access between distributed nodes without port forwarding |
| Local Models | Ollama | Uncensored local model support for content cloud providers refuse |
| CLI | Click | `glitch start`, `glitch bootstrap`, `glitch nuke`, `glitch status`, subcommands for compaction and workers |
| Clients | Flutter mobile app, Desktop UI, Web admin | Dumb terminals streaming Firestore in real-time |

### Model Provider Prefixes

The `model` field in every `AgentConfig` uses PydanticAI's provider prefix format. The daemon checks at startup which providers have API keys configured and only creates agents for reachable models.

| Prefix | Provider | Env Var |
|--------|----------|---------|
| `anthropic:` | Anthropic (Claude) | `GLITCH_ANTHROPIC_API_KEY` |
| `google-gla:` / `gemini:` | Google Gemini | `GLITCH_GEMINI_API_KEY` |
| `openai:` | OpenAI | `GLITCH_OPENAI_API_KEY` |
| `mistral:` | Mistral | `GLITCH_MISTRAL_API_KEY` |
| `groq:` | Groq | `GLITCH_GROQ_API_KEY` |
| `ollama:` | Ollama (local) | `GLITCH_OLLAMA_HOST` |

### Why Not React?

The self-improving nature of the system requires that the UI layer be writable by the AI at runtime. React requires a build step, which creates a wall between the Ouroboros loop and the UI. With HTMX + Jinja2, a new page is just two text files (a Python route module and an HTML template) that follow the same sandbox-validate-promote pipeline as tool generation. The AI extends the UI the same way it extends backend capabilities -- no compilation, no bundling, no restart.

### Why Not Cloud Functions?

Cloud Functions require bundling the local codebase and deploying it to Google's servers. This creates a split where `git pull` updates local code but not the deployed functions, requiring a separate deploy step. It also means if a user stops using the app but forgets to delete their Firebase project, scheduled functions keep running and burning API tokens. By keeping all compute in the daemon, the billing model is simple: if the daemon isn't running, nothing runs, nothing costs money.

---

## Firestore Schema

All Firestore documents map to Pydantic models defined in `glitch_core/schemas.py`. Every agent, worker, and client agrees on these contracts.

### Collections

```
/meta/project                    -- ProjectMeta: version, schema_version, default_agent, feature_flags
/meta/theme                      -- GlitchTheme: current UI theme (colors, fonts, branding)
/meta/compaction_config          -- CompactionConfig: schedule, thresholds, safety settings

/agents/{agent_id}               -- AgentConfig: model, system_prompt (soul), tools list, content_rating,
                                    model_tier, affinity, triggers, timeout, capabilities, enabled flag
                                    THIS IS THE SOURCE OF TRUTH for all agent configuration.

/tools/{tool_id}                 -- ToolRegistration: name, description, filename, created_by, enabled
                                    Registry of dynamic tools created by Ouroboros.

/sessions/{session_id}           -- Session metadata including agent_id (which agent this session talks to)
/sessions/{sid}/messages/{mid}   -- ChatMessage: real-time chat thread (user + agent + sub_agent + system)
                                    Agent messages include streaming field and usage metadata.
/sessions/{sid}/sub_tasks/{tid}  -- SubAgentTask: worker task queue, scoped per session
/sessions/{sid}/run_logs/{lid}   -- Full PydanticAI run traces for debugging

/journals/{journal_id}           -- JournalEntry: mid-term observations from conversations
/journals_archive/{id}           -- Archived journals after compaction (never deleted)

/core_memories/{memory_id}       -- CoreMemory: long-term distilled facts. Memories go here directly
                                    from compaction -- there is no review queue.
/memories_deleted/{id}           -- Soft-deleted memories (archived, not destroyed)

/compaction_runs/{run_id}        -- CompactionRun: audit log of every compaction execution

/workers/{worker_id}             -- WorkerRegistration: heartbeat, capabilities, supported_agents,
                                    current_task, glitch_version

/theme_history/{id}              -- Historical theme snapshots (saved before each theme change)
```

### Removed Collections (from pre-refactor)

- `/soul/` -- **Removed.** Each agent now has its own `system_prompt` field directly in `/agents/{id}`. There is no separate soul collection.
- `/memory_review/` -- **Removed from the active pipeline.** Compacted memories go straight to `/core_memories/`. The review queue code still exists in `pipeline.py` but is never called -- `require_confidence` in the config controls the minimum threshold, and memories below it are simply not created. (Note: the rollback code still references `memory_review` for backwards compatibility with old runs.)
- `/meta/agent_config` -- **Removed.** Agent config is now per-agent at `/agents/{id}`, not a single meta document.

### Key Schema Design Decisions

- **Agents are Firestore documents, not YAML.** The YAML file is seed data consumed once by `glitch bootstrap`. After that, `/agents/{agent_id}` is the source of truth. The web UI provides full CRUD. The daemon watches `/agents/` with `on_snapshot` and hot-rebuilds agent instances on any change.
- **Each agent carries its own soul.** The `system_prompt` field in `AgentConfig` is the agent's personality, directives, and persona. There is no separate soul collection. The router's soul is seeded from `DEFAULT_SOUL` in `bootstrap.py`.
- **`default_agent` is configurable.** `ProjectMeta.default_agent` (at `/meta/project`) determines which agent handles sessions that don't specify one. Defaults to `"router"`.
- **Multi-agent sessions.** Each session document has an `agent_id` field. Users can start a session that talks directly to any agent (the coder, the researcher, etc.) -- the router is not a mandatory intermediary.
- **Content rating is config-driven.** The `content_rating` field on `AgentConfig` controls NSFW routing. There is no hardcoded "spicy" concept -- any agent can be marked NSFW. SFW agents cannot dispatch to NSFW agents; users must chat with NSFW agents directly.
- **`SubAgentTask` carries both immutable and mutable fields.** The top half (prompt, routing, timeout) is set by the dispatching agent. The bottom half (status, claimed_by, result) is owned by the worker.
- **`CoreMemory.previous_content` enables one-step rollback.** When compaction updates a memory, the old content shifts into `previous_content` and `version` bumps.
- **Streaming responses via Firestore doc updates.** The daemon creates a placeholder message doc with `streaming: true`, then updates its `content` field every 600ms as tokens arrive. The client's `on_snapshot` listener renders progressively.
- **Run logs preserve full PydanticAI traces.** Every agent response writes its complete `all_messages_json()` to `/sessions/{sid}/run_logs/{lid}` for debugging.

---

## Agent System Architecture

### No `is_router` Concept

There is no special router class or `is_router` flag. The "router" is just the default agent with dispatch tools (`spawn_sub_agent`) checked in its tool list. Any agent could theoretically have dispatch tools. The default agent is whichever `agent_id` is set in `ProjectMeta.default_agent`.

### Firestore-Driven Agents

All agent configs live at `/agents/{agent_id}` in Firestore. At daemon startup:

1. `load_agents_from_firestore(db)` reads all docs from `/agents/`
2. `build_agent_registry(configs, env)` creates PydanticAI `Agent` instances for agents this node can run (has the API key, has the capabilities)
3. `_build_chat_agents()` creates chat-mode agents (output_type=str, with deps injection) for every enabled agent
4. `_start_agent_watcher()` sets up an `on_snapshot` listener on `/agents/` that hot-rebuilds individual agents when their config changes

An agent config change in the web UI takes effect within seconds -- no restart needed.

### Unified Tool System

Tools come from two sources, assigned to agents via the `tools` list field in `AgentConfig`:

**Builtin Tools** (defined in `glitch_core/agents/builtin_tools.py`):

| Tool ID | Description |
|---------|-------------|
| `write_journal` | Log an observation to persistent memory |
| `spawn_sub_agent` | Delegate a task to a sub-agent worker |
| `workspace_write` | Write a file to the user's workspace |
| `workspace_read` | Read a file from the workspace |
| `workspace_list` | List files in the workspace |
| `workspace_run` | Execute a Python script from the workspace |
| `workspace_delete` | Delete a file from the workspace |
| `create_tool` | Create a new dynamic tool (Ouroboros, requires feature flag) |
| `create_page` | Create a new web page (Ouroboros, requires feature flag) |

**Dynamic Tools** (Python modules in `tools/` directory):

Created by Ouroboros via `create_tool`. Each `.py` file in `tools/` is a module containing async functions. When an agent's `tools` list includes a dynamic tool ID, the agent factory uses `_attach_dynamic_tools()` to:

1. Load the module via `importlib.util.spec_from_file_location()`
2. Find all public async functions (non-underscore-prefixed)
3. Register them as PydanticAI tools on the agent

Dynamic tools are registered in Firestore at `/tools/{tool_id}` for UI visibility.

### System Prompt Construction

The `build_system_prompt()` function in `router.py` dynamically assembles the prompt at runtime from `AgentDeps`:

1. **Agent's soul** -- the `system_prompt` from its `AgentConfig`
2. **Core memories** -- all non-deleted memories from the cache, formatted as `[category] content`
3. **Available sub-agents** -- if the agent has `spawn_sub_agent` in its tools, lists all dispatchable agents with their descriptions, triggers, and models. Content rating filtering applies (SFW agents only see SFW sub-agents).
4. **Tool execution rules** -- instructions to call tools immediately without preamble text
5. **Journal guidelines** -- rules for when to (and when not to) write journal entries

### AgentDeps

Every chat agent receives an `AgentDeps` instance at runtime containing:

- `agent_config` -- this agent's `AgentConfig`
- `all_agents` -- list of all agent configs (for dispatch menu)
- `core_memories` -- cached list of core memory dicts
- `session_id` -- current session
- `db` -- async Firestore client
- `workspace` -- `Workspace` instance
- `safe_writer` -- `SafeFileWriter` instance
- `ouroboros_enabled` -- feature flag state

---

## Daemon Architecture

`GlitchDaemon` in `daemon.py` is the main process. It runs seven concurrent asyncio tasks:

| Task | Name | Description |
|------|------|-------------|
| Agent Listener | `agent_listener` | Watches all sessions for new user messages via `on_snapshot`. Routes each message to the correct chat agent based on the session's `agent_id`. Streams responses back via Firestore doc updates (600ms batches). |
| Web Server | `web_server` | FastAPI/uvicorn on `0.0.0.0:8080`. Serves the admin UI. |
| Self Register | `self_register` | Registers this node as a worker in `/workers/{node_name}` at startup. |
| Heartbeat | `heartbeat` | Updates `last_heartbeat` on the worker doc every 30 seconds. |
| Compaction Scheduler | `compaction` | Runs memory compaction periodically (currently interval-based, every 6 hours). Reads config from `/meta/compaction_config`. Refreshes memory cache after successful compaction. |
| Worker Loop | `worker_loop` | Processes sub-agent tasks dispatched by chat agents. Uses `WorkerDaemon` internally. |
| Reaper | `reaper` | Reclaims stale tasks from dead workers every 5 minutes. |

### on_snapshot Watchers

The daemon uses Firestore's real-time `on_snapshot` listeners (not polling) for all reactive behavior:

1. **Agent config watcher** -- `_start_agent_watcher()` watches `/agents/` collection. On ADDED/MODIFIED, rebuilds the affected chat agent. On REMOVED, drops it. Uses a sync Firestore client because `on_snapshot` runs callbacks in background threads, then bridges to the asyncio loop via `loop.call_soon_threadsafe()`.

2. **Session message watchers** -- `_subscribe_to_session()` watches `/sessions/{sid}/messages/` for new user messages. One watcher per active session. Also watches the `/sessions/` collection itself to detect new sessions and auto-subscribe.

3. **Worker task watchers** -- The `WorkerDaemon._task_listener()` watches `/sessions/{sid}/sub_tasks/` filtered to `status == "pending"` for task dispatch. Same pattern: one watcher per session, auto-subscribes to new sessions.

### Message Processing Flow

1. User writes a message to `/sessions/{sid}/messages/{mid}` (from the web UI or mobile client)
2. The session's `on_snapshot` fires, putting `(session_id, msg_id, data)` into an asyncio queue
3. `_agent_listener` dequeues it, checks `role == "user"` and deduplicates
4. `_handle_message()` looks up `agent_id` from the session doc, finds the corresponding chat agent
5. Loads last 20 messages as conversation history
6. Creates a placeholder response doc with `streaming: true`
7. Calls `agent.run_stream()` with the message and history
8. Updates the response doc's `content` every 600ms as tokens arrive
9. On completion, marks `streaming: false`, writes usage metadata
10. Writes full PydanticAI trace to `run_logs` subcollection

---

## Worker System

### Architecture

Workers are task executors for sub-agent operations. The primary daemon runs a worker internally; standalone workers can also be started via `glitch workers start` on additional nodes.

### Claim Protocol (`protocol.py`)

When a worker sees a pending task via `on_snapshot`:

1. `_can_handle()` checks local routing filters: affinity, target_worker, capabilities, agent support
2. `try_claim_task()` reads the task doc, verifies `status == "pending"`, then updates to `status: "claimed"` with `claimed_by: worker_id`

This is a read-then-conditional-update pattern, not a Firestore transaction (due to async client API inconsistencies). Race safety relies on the `on_snapshot` filter (`status == "pending"`) ensuring typically only one worker sees each task. In the rare race, the second worker reads `status: "claimed"` and bails.

### Task Affinity

Three affinity levels control routing:

| Affinity | Behavior |
|----------|----------|
| `ANY` | Any capable worker can claim immediately |
| `PREFERRED` | Only `target_worker` can claim during the fallback window. After `fallback_window_seconds` expires, any worker can claim (reaper promotes the routing). |
| `EXCLUSIVE` | Only `target_worker` can ever claim. Task waits indefinitely. Reaper logs warnings after 24 hours but never reassigns. |

### Reaper (`reaper.py`)

Runs every 5 minutes with three responsibilities:

1. **Dead worker recovery** -- Finds workers with no heartbeat for 2+ minutes. Releases their claimed/running tasks back to `pending`.
2. **Preferred fallback promotion** -- Finds pending tasks with `preferred` affinity past their `fallback_window_seconds`. Rewrites routing to `affinity: any` with the `fallback_agent`.
3. **Exclusive monitoring** -- Logs warnings for `exclusive` tasks waiting 24+ hours. Never reassigns them.

### Worker Registration (`registration.py`)

At startup, each worker:

1. Determines which agents it can run (API key checks + capability matching)
2. Writes a `WorkerRegistration` doc to `/workers/{node_name}` with hostname, capabilities, supported agents, and version
3. Starts a heartbeat loop (every 30 seconds)

### Worker Result Formatting

When a sub-agent task completes, `_format_agent_result()` in `loop.py` converts structured outputs into readable markdown:

- `ResearchResult` -- formatted with summary, sources as links, confidence percentage
- `CodeArtifact` -- code blocks with language syntax highlighting, explanation, optional tests
- `CommandResult` -- command, exit code, stdout/stderr in code blocks
- Plain text -- wrapped with agent attribution

The formatted result is written as a `sub_agent` role message in the session.

---

## Compaction Pipeline

The memory compaction system (the "Subconscious") distills raw journal entries into long-term core memories.

### Four Crash-Safe Phases

1. **Read** -- Query unarchived journals (`archived == false`), oldest first, up to `max_journals_per_run`. If fewer than `min_journals_to_trigger`, skip the run.

2. **Group & Summarize** -- Batch journals (default batch_size=10) and send each batch to a PydanticAI summarization agent. The agent receives the journals plus all existing core memories as context (for merging/deduplication). Output: `CompactionResult` with `memories` and `discarded` lists.

3. **Validate & Write** -- For each compacted memory:
   - If it references an existing memory ID in `related_memory_ids`, update that memory (preserving `previous_content` for rollback)
   - Otherwise, create a new core memory
   - Track which journal IDs were consumed

4. **Archive** -- Copy consumed journals to `journals_archive` with `compaction_run` reference. Mark originals as `archived: true`. Journals are NEVER deleted.

### Configuration (`CompactionConfig`)

Stored at `/meta/compaction_config`, editable via web UI:

- `model` -- which model runs summarization (default: `anthropic:claude-sonnet-4-20250514`)
- `min_journals_to_trigger` -- minimum journals before running (default: 5)
- `max_journals_per_run` -- cap per execution (default: 100)
- `batch_size` -- journals per summarization call (default: 10)
- `require_confidence` -- minimum confidence for memory creation (default: 0.7)
- `never_compact_categories` -- protected categories: relationship, identity, medical
- `archive_journals` -- whether to archive consumed journals (default: true)
- `dry_run` -- preview mode (default: false)
- `enabled` -- master switch

### Compaction Prompts (`prompts.py`)

The summarization agent follows strict rules:
- Preserve specifics (names, dates, numbers) -- never abstract them
- Merge with existing memories via `related_memory_ids` rather than duplicating
- Never infer beyond the data
- Importance scoring: 1.0 for identity/relationships, 0.3 for casual mentions
- Confidence scoring: 1.0 for explicit statements, 0.5 for ambiguous
- Contradiction handling: create updated memory referencing the old one
- Every journal must appear in either a memory's `source_journal_ids` or in `discarded`

### Rollback (`rollback.py`)

`rollback_compaction_run(db, run_id)` fully reverts a compaction run:
1. Memories created by this run -- deleted
2. Memories updated by this run -- reverted to `previous_content`
3. Review items from this run -- removed (legacy cleanup)
4. Archived journals from this run -- restored to active
5. Run status set to `rolled_back`

Invocable via `glitch compaction rollback <run_id>`.

---

## The Three Trust Zones (Ouroboros)

Ouroboros operates in three distinct trust zones with escalating privilege:

### 1. Engine Zone (Human Only)

The core system code under `glitch_core/`. This is the only zone that CANNOT be modified at runtime. Changes require `git pull` + restart. The engine includes the daemon, agent framework, compaction pipeline, web server, and all core pages.

### 2. System Zone (SafeFileWriter)

Tools (`tools/`) and custom pages (`glitch_core/web/pages_custom/` + `templates_custom/`). These are AI-writable but gated through `SafeFileWriter`, which enforces:

- **Syntax validation** -- `compile()` check
- **AST scanning** -- blocks dangerous patterns: `os.remove`, `os.system`, `subprocess.run`, `shutil.rmtree`, and all imports of `os`, `subprocess`, `shutil`, `sys`, `ctypes`
- **Subprocess import test** -- imports the module in an isolated subprocess with a clean environment
- **Git snapshot before promotion** -- commits the current state for rollback
- **Atomic promotion** -- writes file(s) to disk
- **Git commit after promotion** -- commits the new state
- **Hot-reload attempt** -- if reload fails, automatically `git revert`s

For pages, both the Python route module and Jinja2 template are validated together and promoted atomically -- either both succeed or neither does. Templates are validated with `jinja2.Environment.parse()`.

**Feature flag gated:** Ouroboros tools (`create_tool`, `create_page`) check `deps.ouroboros_enabled` and refuse to run if the flag is off. The flag lives at `/meta/project` under `feature_flags.ouroboros_enabled`.

### 3. Workspace Zone (Free-form)

The `workspace/` directory. The AI writes anything the user asks for here -- scripts, websites, data files. Key properties:

- **No validation** beyond path safety (traversal prevention)
- **No hot-reload** -- the daemon never imports from workspace
- **No system impact** -- isolated from the running system
- **Path safety** -- cannot escape to `glitch_core/`, `tools/`, `.git/`, or `~/.glitch/`
- **Size limits** -- 50MB per file, 500MB total workspace
- **Script execution** -- `workspace_run` executes Python scripts with the user's normal environment (including API keys), since these are the user's own scripts

### Circuit Breaker (`RuntimeCircuitBreaker`)

Monitors errors after Ouroboros promotions. If `threshold` errors (default: 3) occur within the `stability_window` (default: 5 minutes) after a promotion, the circuit breaker automatically:

1. Calls `safe_writer.rollback(last_promotion_sha)` to `git revert` the promotion
2. Clears the error counter and promotion tracking
3. Logs a CRITICAL-level message

The circuit breaker is wired into the daemon's message handler -- every exception in `_handle_message()` calls `circuit_breaker.record_error(e)`.

---

## Ouroboros Generators

### Tool Generator (`tool_generator.py`)

`generate_tool()` orchestrates:
1. Build a prompt with filename, description, and constraints (no os/subprocess/shutil/sys/ctypes)
2. Run the coder agent to generate code
3. Pass code through `SafeFileWriter.write_tool()` for validation + promotion
4. If validation fails with fixable errors, feed errors back to the coder and retry (up to 3 attempts)
5. On success, register the tool in Firestore at `/tools/{tool_id}`

### Page Generator (`page_generator.py`)

`generate_page()` orchestrates:
1. Build a prompt with full tech stack constraints (FastAPI, Jinja2, HTMX, Tailwind, Firestore, glitch color palette)
2. The coder generates both files separated by `---TEMPLATE---` marker
3. Pass both through `SafeFileWriter.write_page()` for atomic validation + promotion
4. Retry loop with error feedback (up to 3 attempts)
5. On success, `PageEngine.reload_custom_pages()` makes the page available immediately

### Theme Generator (`theme_generator.py`)

`generate_theme()` orchestrates:
1. Build a prompt with `GlitchTheme` JSON schema and WCAG contrast requirements
2. The coder generates a JSON theme object
3. Parse and validate as `GlitchTheme`
4. Check WCAG AA contrast ratios on critical color pairs (text/bg, text/surface, muted/bg, muted/surface)
5. If contrast fails, retry with specific issues listed (up to 2 retries)
6. Archive current theme to `/theme_history/`, write new theme to `/meta/theme`

---

## Web UI

### Application Assembly (`app.py`)

`create_app()` builds the FastAPI application:
1. Creates `Jinja2Templates` with multi-directory search (custom templates override core)
2. Disables Jinja2 template cache (`cache_size=0`) to avoid unhashable globals issue
3. Creates `PageEngine` and discovers all page modules from `pages/` and `pages_custom/`
4. Mounts all discovered routers
5. Adds `ThemeMiddleware` for Firestore-backed theme injection
6. Stores `db`, `templates`, `page_engine` on `app.state` for route access

### PageEngine (`engine.py`)

Discovers and manages page modules:
- Scans `pages/` (core) and `pages_custom/` (AI-generated) directories
- Each module must define a `router = APIRouter(prefix="/...")` and optionally `PAGE_META = PageMeta(...)`
- `PageMeta` controls navigation: title, icon, section (core/custom), order, visibility
- `get_nav_items()` returns pages grouped by `nav_section` and sorted by `nav_order`
- `reload_custom_pages()` clears and re-scans custom pages (used by Ouroboros after page promotion)

### ThemeMiddleware (`middleware.py`)

- Loads the current `GlitchTheme` from `/meta/theme` with a 10-minute in-memory cache
- Injects `theme` and `nav` into Jinja2 template globals on every request
- Supports cache busting via `app.state._theme_bust` flag (set by theme apply routes)

### Core Pages

| Page | Route | Description |
|------|-------|-------------|
| Dashboard | `/` | System overview |
| Chat | `/chat` | Multi-agent chat interface with streaming, markdown rendering, thinking animation |
| Agents | `/agents` | Agent CRUD -- create, edit, delete agents. Edit system prompt (soul), assign tools via checkboxes, configure model/tier/affinity/content rating |
| Memories | `/memories` | View, search, and manage core memories |
| Journals | `/journals` | View journal entries, archive status |
| Soul | `/soul` | View/edit agent system prompts (legacy route, agents page is primary) |
| Workers | `/workers` | Worker status, capabilities, current tasks |
| Logs | `/logs` | Run log viewer with full PydanticAI trace inspection |
| System | `/system` | Project metadata, feature flags, compaction config |
| Theme | `/theme` | Theme switching (presets), AI theme generation |
| Review | `/review` | Memory review queue (legacy, no longer actively used) |
| Workspace | `/workspace` | Browse and manage workspace files |

---

## CLI Commands

Entry point: `glitch = "glitch_core.cli:cli"` (Click group)

### Top-Level Commands

| Command | Description |
|---------|-------------|
| `glitch start` | Start the main daemon (agent listener + web server + worker + all background tasks) |
| `glitch bootstrap` | First-run Firestore initialization -- creates database, seeds agents from YAML, writes default theme/compaction config/project meta, deploys security rules |
| `glitch nuke` | Delete ALL Firestore data, reset rules to deny-all, require re-bootstrap. Confirmation required. |
| `glitch status` | Show version, Firebase project, and worker status |

### Compaction Subcommands (`glitch compaction ...`)

| Command | Description |
|---------|-------------|
| `glitch compaction run` | Run compaction manually. `--dry-run` (default) for preview, `--force` for live with min_journals=1. |
| `glitch compaction status` | Show recent compaction run history (default: last 10) |
| `glitch compaction rollback <run_id>` | Fully revert a compaction run. Confirmation required. |

### Worker Subcommands (`glitch workers ...`)

| Command | Description |
|---------|-------------|
| `glitch workers start` | Start a standalone worker daemon (no web UI, no chat listener). Reads agents from Firestore, builds registry, processes sub-tasks. |
| `glitch workers status` | Show all registered workers with heartbeat age, capabilities, supported agents, current task. |

### Placeholder Subcommands (Not Yet Implemented)

- `glitch update` -- git pull + pip install + migrations + restart
- `glitch pages list` / `glitch pages rollback`

---

## Configuration Hierarchy

Configuration comes from three sources, in order of precedence:

### 1. Environment (`~/.glitch/.env`)

Machine-local secrets and node identity. Read by `GlitchEnv` (pydantic-settings). All prefixed with `GLITCH_`:

```
GLITCH_FIREBASE_PROJECT=my-project-id
GLITCH_FIREBASE_CREDENTIALS=/Users/me/.glitch/credentials.json
GLITCH_ANTHROPIC_API_KEY=sk-ant-...
GLITCH_GEMINI_API_KEY=AIza...
GLITCH_OPENAI_API_KEY=sk-...
GLITCH_MISTRAL_API_KEY=...
GLITCH_GROQ_API_KEY=...
GLITCH_OLLAMA_HOST=http://localhost:11434
GLITCH_NODE_NAME=main
GLITCH_NODE_CAPABILITIES=["api"]
```

### 2. Firestore (Runtime Source of Truth)

- `/agents/{id}` -- all agent configuration (model, system prompt, tools, affinity, content rating)
- `/meta/project` -- project metadata, default_agent, feature flags
- `/meta/compaction_config` -- compaction settings
- `/meta/theme` -- current UI theme

### 3. YAML Seed (`glitch_core.yaml`)

Used ONLY by `glitch bootstrap` to populate Firestore with initial agent configs. After bootstrap, this file is not read by the daemon. The daemon loads agents exclusively from Firestore.

The YAML defines the initial router and worker agents with their models, model tiers, output types, triggers, timeouts, affinities, capabilities, and content ratings.

---

## Security Model

### Firestore Rules

The current security model is **read-only from browser, admin-write from daemon**:

- `/sessions/`, `/sessions/{sid}/messages/`, `/sessions/{sid}/sub_tasks/`, `/sessions/{sid}/run_logs/` -- read: true, write: false
- `/agents/{agentId}` -- read: true, write: false
- `/meta/{docId}` -- read: true, write: false
- Everything else -- read: false, write: false

All writes go through the daemon's Admin SDK, which bypasses security rules entirely. Browser clients get read-only access for real-time `on_snapshot` listeners.

**Firebase Auth is planned but not yet implemented.** Currently, anyone who knows the Firebase project ID can read session data. This is acceptable for single-user self-hosted deployments but needs auth before any multi-user or public-facing deployment.

### Ouroboros Safety

1. **AST scanning** blocks dangerous system calls in AI-generated code
2. **Subprocess import testing** catches runtime import errors in isolation
3. **Git snapshots** before every promotion for rollback
4. **Circuit breaker** auto-reverts promotions that cause runtime errors
5. **Feature flag** -- Ouroboros is disabled by default, must be explicitly enabled

### Workspace Isolation

- Path traversal blocked (resolved paths must be under workspace root)
- Forbidden prefixes: `glitch_core`, `tools`, `soul`, `.git`, `.claude`
- Cannot write to `~/.glitch/`
- Size limits enforced (50MB/file, 500MB total)

---

## Bootstrap Process

`glitch bootstrap` (in `bootstrap.py`):

1. **Ensure Firestore database exists** -- creates `(default)` database in `nam5` region if missing
2. **Write `/meta/project`** -- ProjectMeta with version, schema_version, feature flags (ouroboros disabled)
3. **Seed `/agents/{id}`** -- reads `glitch_core.yaml`, creates one Firestore doc per agent:
   - Router gets `DEFAULT_SOUL` as system_prompt, all `DEFAULT_ROUTER_TOOLS`
   - Coder gets workspace tools + create_tool + create_page
   - Other agents get `write_journal` only
   - Default system prompts from `DEFAULT_PROMPTS` dict in `agents/__init__.py`
4. **Write `/meta/compaction_config`** -- default CompactionConfig
5. **Write `/meta/theme`** -- default preset theme
6. **Seed empty collections** -- placeholder docs in sessions, journals, journals_archive, core_memories, memories_deleted, compaction_runs, workers, theme_history
7. **Write `~/.glitch/config.json`** -- CLI config pointer
8. **Deploy Firestore rules + indexes** -- writes `firestore.rules` and runs `firebase deploy --only firestore`

---

## File-by-File Reference

### Root Files

| File | Purpose |
|------|---------|
| `pyproject.toml` | Package definition. Dependencies: fastapi, uvicorn, jinja2, pydantic, pydantic-settings, pydantic-ai, google-cloud-firestore, google-auth, click, pyyaml, python-multipart, httpx. Optional extras: `[worker]` (ollama, paramiko), `[dev]` (pytest, pytest-asyncio, ruff). CLI entry: `glitch = "glitch_core.cli:cli"`. Python 3.11+. |
| `glitch_core.yaml` | Seed agent configuration. Defines router + 4 agents (coder, researcher, sysadmin, spicy) with models, tiers, output types, triggers, affinities, capabilities, content ratings. Consumed once by `glitch bootstrap`. |
| `firestore.rules` | Firestore security rules. Read-only browser access for sessions/messages/agents/meta. All writes via Admin SDK. |
| `firestore.indexes.json` | Composite indexes: journals (archived + created_at), core_memories (deleted + category, deleted + updated_at), sub_tasks (status + created_at). |
| `firebase.json` | Firebase CLI config pointing to rules and indexes files. |
| `ARCHITECTURE.md` | This file. |
| `todo.md` | Project TODO list with high/medium/low priority items and completed items. |

### Core Package -- `glitch_core/`

| File | Purpose |
|------|---------|
| `__init__.py` | Package root. Exports `__version__ = "0.1.0"`. |
| `schemas.py` | **The central type contract.** All Pydantic models: enums (TaskStatus, TaskCommand, ModelTier, MessageRole, ContentRating, TaskAffinity, WorkerCapability, MemoryCategory, ValidationStage), task models (SubAgentTask, TaskError, TaskRouting, ClaimResult), worker models (WorkerRegistration), agent output schemas (CodeArtifact, ResearchResult, CommandResult), chat models (ChatMessage, Attachment), memory models (JournalEntry, CoreMemory), event protocol (TaskQueued, TaskCompleted), config models (AgentConfig, GlitchConfig, FeatureFlags, ProjectMeta, CompactionConfig), compaction output models (CompactedMemory, DiscardedJournal, CompactionResult, CompactionError, CompactionRun), Ouroboros models (ValidationFailure, PromotionResult, ToolRegistration), workspace models (WorkspaceFile, WorkspaceEntry, WorkspaceTree, ScriptResult). |
| `config.py` | Configuration loading. `GlitchEnv` (pydantic-settings, reads `~/.glitch/.env`): Firebase credentials, 6 API keys, node name, node capabilities. `load_yaml_config()` parses `glitch_core.yaml` into `GlitchConfig`. `get_default_agent_id(db)` reads from `/meta/project`. `get_firestore_client(env)` creates async Firestore client. `find_firebase_bin()` locates Firebase CLI including nvm paths. |
| `daemon.py` | **The main process.** `GlitchDaemon` class running 7 concurrent asyncio tasks: agent_listener (on_snapshot message watcher + streaming response), web_server (FastAPI on :8080), self_register, heartbeat (30s), compaction_scheduler (6h interval), worker_loop (WorkerDaemon), reaper (5min interval). Manages: chat agents dict, agent configs cache, memories cache, Ouroboros components (Workspace, SafeFileWriter, RuntimeCircuitBreaker). Uses both async and sync Firestore clients (sync for on_snapshot callbacks). Hot-rebuilds agents via `_rebuild_agent()` called from `_start_agent_watcher()`. |
| `bootstrap.py` | First-run Firestore initialization. Creates database if needed, seeds agents from YAML with default system prompts and tools, writes project meta and compaction config and theme, seeds empty collections, deploys security rules. Contains `DEFAULT_SOUL` string and `FIRESTORE_RULES` string. |

### Agents -- `glitch_core/agents/`

| File | Purpose |
|------|---------|
| `__init__.py` | Agent factory system. `OUTPUT_TYPE_MAP` maps output_type strings to Pydantic models. `DEFAULT_PROMPTS` dict with seed system prompts for coder/researcher/sysadmin/spicy. `_can_run_model(model, env)` checks API key availability for 6 providers. `create_agent_from_config(cfg)` universal factory -- creates PydanticAI Agent from any AgentConfig with correct output type and dynamic tools. `_attach_dynamic_tools(agent, tool_ids)` loads Python modules from `tools/` directory. `load_agents_from_firestore(db)` reads all `/agents/` docs. `build_agent_registry(agents, env)` builds dict of agent_id to Agent for agents this node can run. |
| `router.py` | Chat agent creation. `AgentDeps` model (arbitrary_types_allowed) with agent_config, all_agents, core_memories, session_id, db, workspace, safe_writer, ouroboros_enabled. `create_chat_agent(cfg)` creates a PydanticAI Agent[AgentDeps, str] with dynamic system prompt, end_strategy="exhaustive", and attaches builtin tools from BUILTIN_TOOLS registry based on the agent's tools list. |
| `builtin_tools.py` | **Builtin tool registry.** 9 tools implemented as functions that take an Agent and attach a tool to it. `BUILTIN_TOOLS` dict maps tool_id strings to attach functions. `DEFAULT_ROUTER_TOOLS` list of all 9 tools (used during bootstrap for the router agent). Each tool receives `RunContext` with `AgentDeps` for database/workspace/config access. Key tools: `write_journal` (writes to /journals/), `spawn_sub_agent` (writes SubAgentTask to /sessions/{sid}/sub_tasks/ with content rating enforcement), `create_tool` (SafeFileWriter + Firestore registration, requires ouroboros flag), `create_page` (SafeFileWriter page promotion, requires ouroboros flag). |

### Workers -- `glitch_core/workers/`

| File | Purpose |
|------|---------|
| `loop.py` | **WorkerDaemon class.** Runs heartbeat + task listener as concurrent tasks. `_task_listener()` uses on_snapshot on sub_tasks (filtered to pending) across all sessions. `_can_handle()` local routing filter checking affinity, target_worker, capabilities, agent support. `_try_and_execute()` claims then executes via agent registry. Writes formatted results as sub_agent messages and run logs. Uses both async and sync Firestore clients. `_format_agent_result()` converts structured outputs (ResearchResult, CodeArtifact, CommandResult) to readable markdown. |
| `protocol.py` | **Atomic task claiming.** `try_claim_task(db, session_id, task_id, worker_id)` -- read-then-conditional-update. Returns `ClaimResult(claimed=bool, task_id, reason)`. |
| `reaper.py` | **Stale task recovery.** `reap_stale_tasks(db)` -- finds dead workers (no heartbeat 2+ min), releases their tasks to pending, promotes preferred tasks past fallback window, warns on long-waiting exclusive tasks. |
| `registration.py` | **Worker self-registration.** `register_worker(db, env, agent_configs)` -- determines supported agents based on API keys and capabilities, writes WorkerRegistration to `/workers/{node_name}`. |

### Compaction -- `glitch_core/compaction/`

| File | Purpose |
|------|---------|
| `pipeline.py` | **Main compaction pipeline.** `run_compaction(db, config)` with 4 phases: read (query unarchived journals), summarize (batch + PydanticAI agent), write (create/update core_memories), archive (copy to journals_archive). Writes CompactionRun audit log. Idempotent -- safe to retry on crash. |
| `prompts.py` | **Summarization prompts.** `COMPACTION_SYSTEM_PROMPT` with strict rules for memory distillation. `build_compaction_prompt()` constructs per-batch prompt with existing memories as context. |
| `rollback.py` | **Full run reversal.** `rollback_compaction_run(db, run_id)` -- deletes created memories, reverts updated memories, removes review items, restores archived journals, marks run as rolled_back. |

### Ouroboros -- `glitch_core/ouroboros/`

| File | Purpose |
|------|---------|
| `__init__.py` | Exports `SafeFileWriter`, `RuntimeCircuitBreaker`, `Workspace`. |
| `sandbox.py` | **SafeFileWriter** -- the enforcement layer for the System trust zone. `write_tool()` and `write_page()` follow: validate -> git snapshot -> promote -> git commit -> reload. `RuntimeCircuitBreaker` -- monitors post-promotion errors, auto-reverts after threshold. Also contains all validation functions: `_validate_python()` (syntax + AST scan + subprocess import), `_scan_ast()` (walks AST for dangerous patterns), `_validate_template()` (Jinja2 parse check), and git helpers (`_git_snapshot`, `_git_commit`, `_git_revert`). |
| `workspace.py` | **Workspace** -- free-form user zone at `workspace/`. Path-safe write/read/list/delete/mkdir/run_script. FORBIDDEN_PREFIXES prevent escaping to system dirs. Size limits: 50MB/file, 500MB total. Script execution uses subprocess with user's full environment. |
| `tool_generator.py` | **Tool generation orchestrator.** `generate_tool()` -- prompt builder -> coder agent -> SafeFileWriter -> retry on fixable errors (3 attempts) -> Firestore registration. |
| `page_generator.py` | **Page generation orchestrator.** `generate_page()` -- builds prompt with full tech stack constraints -> coder generates Python + HTML separated by `---TEMPLATE---` marker -> SafeFileWriter atomic promotion -> retry loop (3 attempts). `CODER_PAGE_PROMPT` constant with detailed tech stack instructions. |
| `theme_generator.py` | **Theme generation.** `generate_theme()` -- prompt with GlitchTheme schema -> coder generates JSON -> parse as GlitchTheme -> WCAG contrast validation -> retry on contrast failure (2 attempts) -> save current theme to history -> write new theme. |

### Web -- `glitch_core/web/`

| File | Purpose |
|------|---------|
| `app.py` | **FastAPI assembly.** `create_app(db)` -- sets up Jinja2 templates (multi-directory, cache disabled), PageEngine discovery, ThemeMiddleware, stores db/templates/page_engine on app.state. |
| `engine.py` | **PageEngine.** Discovers page modules from `pages/` and `pages_custom/`. `PageMeta` model (title, icon, nav_section, nav_order, show_in_nav, route_prefix, badge_count). `PageEntry` wraps meta + module info. `discover_pages()` scans and imports modules. `reload_custom_pages()` for Ouroboros hot-reload. |
| `middleware.py` | **ThemeMiddleware.** Loads GlitchTheme from Firestore with 10-minute cache. Injects theme + nav into Jinja2 globals per request. Supports forced refresh via `_theme_bust` flag. |
| `theming.py` | Theme models and presets. `GlitchTheme` model, `PRESET_THEMES` dict, `_passes_contrast_check()` for WCAG validation. |
| `pages/` | 12 core page modules (dashboard, chat, agents, memories, journals, soul, workers, logs, system, theme, review, workspace). Each defines `router` + `PAGE_META`. |
| `pages_custom/` | AI-generated pages (Ouroboros). Same structure as core pages. |
| `templates/` | 14 Jinja2 templates (base.html + one per page + agent_edit.html, log_detail.html). All extend base.html, use Tailwind with `glitch-` color palette. |
| `templates_custom/` | AI-generated templates (Ouroboros). Override or supplement core templates. |

### CLI -- `glitch_core/cli/`

| File | Purpose |
|------|---------|
| `__init__.py` | Click group assembly. Registers all commands and subcommand groups (compaction, workers, update, pages). |
| `main.py` | Top-level commands: `start_cmd` (run daemon), `bootstrap_cmd` (init Firestore), `nuke_cmd` (delete all data + reset rules), `status_cmd` (show version + workers). |
| `compaction.py` | Compaction subcommands: `run` (manual execution with dry-run/force), `status` (recent history), `rollback` (full run reversal with confirmation). |
| `workers.py` | Worker subcommands: `start` (standalone worker daemon), `status` (all workers with heartbeat, capabilities, tasks). |

---

## Key Architectural Invariants

1. **Firestore is the single source of truth for agent configuration.** YAML is seed data only. After bootstrap, agents live in Firestore and are edited via the web UI. The daemon watches `/agents/` and hot-rebuilds on change.

2. **Every agent is equal.** There is no `is_router` flag. The "router" is the default agent with dispatch tools checked. Any agent can have any tool. Any agent can be the default.

3. **Sessions have agent affinity.** Each session belongs to one agent (via `agent_id` field). Users chat directly with any agent. The router is not a mandatory intermediary.

4. **Content rating is config-driven.** NSFW routing is determined by the `content_rating` field on agents, not by hardcoded agent names. SFW agents cannot dispatch to NSFW agents.

5. **Memories go straight to core_memories.** There is no review queue in the active pipeline. The compaction confidence threshold controls what gets created.

6. **Journals are never deleted.** They are archived after compaction. The original stays with `archived: true`, a copy goes to `journals_archive`. Rollback restores both.

7. **on_snapshot, not polling.** All reactive behavior uses Firestore real-time listeners. Idle cost is near zero (no read quotas burned while waiting).

8. **Streaming via document updates.** Agent responses stream via periodic updates to a Firestore message doc (600ms batches), not via WebSocket or SSE. The client's `on_snapshot` listener provides the real-time effect.

9. **The daemon owns all writes.** Browser clients are read-only. The daemon's Admin SDK bypasses security rules. All mutations go through the daemon.

10. **Ouroboros is feature-flagged.** The `create_tool` and `create_page` tools check `ouroboros_enabled` before executing. Disabled by default.

11. **Git is the rollback mechanism for Ouroboros.** Every tool/page promotion is committed. The circuit breaker and manual rollback both use `git revert`.

12. **Workers are self-describing.** Each worker writes its capabilities, supported agents, and version to Firestore at startup. The reaper uses heartbeat timestamps to detect dead workers.

---

## NOT IMPLEMENTED

Features that are designed or referenced in code but not yet functional:

| Feature | Status | Description |
|---------|--------|-------------|
| **Firebase Auth** | Planned | Browser Firestore access is currently read-only with no authentication. Needs auth before any multi-user or public deployment. Security rules currently allow unauthenticated reads on sessions/agents/meta. |
| **`glitch update`** | Placeholder | CLI group exists but no subcommands. Intended: `git pull` + `pip install` + schema migrations + daemon restart. |
| **Migration runner** | Not started | No `migrations/` directory or runner. Schema changes require manual Firestore updates or nuke + re-bootstrap. |
| **Proper cron scheduling** | Not implemented | Compaction scheduler uses a fixed 6-hour `asyncio.sleep()` interval, not actual cron parsing of `CompactionConfig.schedule_cron`. |
| **`glitch stop` / `glitch restart`** | Not started | Daemon runs in foreground. No systemd/launchd service integration yet. |
| **SSH execution (sysadmin)** | Stub only | The sysadmin agent's `execute_ssh` tool returns a placeholder string. Paramiko is in optional deps but not wired up. |
| **`glitch pages list` / `rollback`** | Placeholder | CLI group exists, no subcommands implemented. |
| **Bootstrap memory quiz** | Not started | Bootstrap should ask user for name, preferences, etc. during first run to seed initial memories. |
| **Logfire integration** | Not started | PydanticAI observability via Logfire. |
| **Docker workspace execution** | Not started | Workspace scripts run via bare `subprocess`. Production hardening would use containers. |
| **nsjail/bubblewrap sandbox** | Not started | Ouroboros subprocess validation uses basic isolated subprocess, not a proper sandbox. |
| **Logo color extraction** | Not started | Pillow-based color palette extraction for theme generation from uploaded logos. |
| **OpenAI-compatible base URL** | Not started | No LM Studio / vLLM support via custom base URL. |
| **Agent config versioning** | Not started | No history tracking for agent config changes in Firestore. |
| **Firestore structural validation** | Not started | Security rules don't enforce valid status transitions or field-level write restrictions. |
