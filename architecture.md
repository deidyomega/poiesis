# Glitch Core — Architecture Specification

## Project Overview

Glitch Core is a distributed, stateful, self-improving AI entity that replaces the OpenClaw monolith. It is an open-source, self-hosted personal AI system where every installation is single-tenant — the user creates their own Firebase project, runs the daemon on their own hardware, and owns all their data. There is no central server, no shared infrastructure, no published pip package, and no SaaS component.

The system is designed to be malleable. The AI can extend its own tools, generate new UI pages, retheme the interface, and improve its own capabilities at runtime — all without a restart or rebuild step. A non-technical user (e.g. someone's girlfriend) can say "make the app pink and gothic" or "add a page that counts how many times I mentioned my dog" and the system generates and hot-reloads the result.

### Core Philosophy

- **Firebase is storage, not compute.** Firestore acts as the pub/sub event bus, state store, and memory layer. All code execution happens locally — wherever "local" means to that user (AWS, home lab, laptop).
- **No Cloud Functions.** Every process runs inside the daemon or worker loops. If the daemon is down, nothing runs — and that's a feature, not a bug. No orphaned functions burning tokens in the background if the user walks away.
- **Single-tenant by design.** Each user creates their own Firebase project. There is no multi-tenant scoping, no shared databases, no API key custody. One project, one user, total ownership.
- **The entire application surface is writable by the AI.** Tools (Python modules), UI pages (FastAPI routes + Jinja2 templates), and themes (Firestore documents) are all in the Ouroboros blast radius. The only thing that doesn't change at runtime is the engine itself.
- **`glitch update` does everything.** `git pull` + dependency install + database migrations + restart. One command, no deployment targets, no build steps.

### Mental Model

The architecture uses an anatomical metaphor that maps to real system boundaries:

- **The Nervous System** — Firestore. The database schema, pub/sub event bus, real-time sync to clients.
- **The Subconscious** — Memory compaction pipeline. Background distillation of raw conversation observations into long-term memories.
- **The Hands** — Execution layer. The daemon that listens for messages, runs tools, executes SSH commands across the Tailnet.
- **The Hive** — Sub-agent system. Horizontal scaling via Firestore as a task queue, with heterogeneous workers (cloud APIs, local models, GPU rigs).
- **The Ouroboros** — Self-improvement loop. The system can write, validate, and hot-reload its own tools, UI pages, and themes.

---

## Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| LLM Engine | PydanticAI | Type-safe, schema-enforced agent definitions with structured outputs |
| State & Pub/Sub | Firebase Firestore | Real-time listeners, free client sync, generous free tier for storage |
| Web UI | FastAPI + Jinja2 + HTMX + Tailwind CDN | No build step — the entire UI is hot-reloadable by the Ouroboros system |
| Configuration | Pydantic + PydanticSettings + YAML | Typed config with `.env` files for secrets, YAML for agent definitions |
| Network Mesh | Tailscale | Secure access between distributed nodes without port forwarding |
| Local Models | Ollama | Uncensored local model support (the "Spicy" worker) |
| Clients | Flutter mobile app, Desktop UI, Web admin | Dumb terminals streaming Firestore in real-time |

### Why Not React?

The self-improving nature of the system requires that the UI layer be writable by the AI at runtime. React requires a build step, which creates a wall between the Ouroboros loop and the UI. With HTMX + Jinja2, a new page is just two text files (a Python route module and an HTML template) that follow the same sandbox-validate-promote pipeline as tool generation. The AI extends the UI the same way it extends backend capabilities — no compilation, no bundling, no restart.

### Why Not Cloud Functions?

Cloud Functions require bundling the local codebase and deploying it to Google's servers. This creates a split where `git pull` updates local code but not the deployed functions, requiring a separate deploy step. It also means if a user stops using the app but forgets to delete their Firebase project, scheduled functions keep running and burning API tokens. By keeping all compute in the daemon, the billing model is simple: if the daemon isn't running, nothing runs, nothing costs money.

---

## Firestore Schema

All Firestore documents map to Pydantic models defined in `glitch_core/schemas.py`. Every agent, worker, and client agrees on these contracts.

### Collections

```
/meta/project                    — ProjectMeta: version, schema_version, feature flags
/meta/agent_config               — The parsed glitch_core.yaml content
/meta/theme                      — GlitchTheme: current UI theme (colors, fonts, branding)
/meta/compaction_config          — CompactionConfig: schedule, thresholds, safety settings
/meta/migrations/history/{id}    — MigrationRecord: applied migration audit trail

/soul/{doc_id}                   — Core identity, persona, strict directives (SOUL.md content)
/soul_history/v{n}               — Versioned snapshots of previous soul edits

/sessions/{session_id}           — Session metadata, connected client info
/sessions/{sid}/messages/{mid}   — ChatMessage: the real-time chat thread (user + agent + system)
/sessions/{sid}/sub_tasks/{tid}  — SubAgentTask: the worker queue for sub-agents

/journals/{date_id}              — JournalEntry: mid-term scratchpad, passive observations from conversations
/journals_archive/{id}           — Archived journals after compaction (never deleted)

/core_memories/{memory_id}       — CoreMemory: long-term distilled facts (replacement for MEMORY.md)
/memories_deleted/{id}           — Soft-deleted memories (archived, not destroyed)

/memory_review/{id}              — Low-confidence compacted memories awaiting human approval
/compaction_runs/{run_id}        — CompactionRun: audit log of every compaction execution

/workers/{worker_id}             — WorkerRegistration: heartbeat, capabilities, current task
/worker_tokens/{token}           — (Reserved, currently unused — no token exchange in single-tenant model)

/theme_history/{id}              — Historical theme snapshots
```

### Key Schema Design Decisions

- **`SubAgentTask` carries both immutable and mutable fields.** The top half (prompt, schema, timeout) is written once by the router and never touched. The bottom half (status, claimed_by, result) is owned by the worker. The router does `set()`, workers do `update()` on just their fields.
- **`CoreMemory.previous_content` enables one-step rollback.** When the compaction pipeline or a human edits a memory, the old content shifts into `previous_content` and `version` bumps. No full versioning system needed.
- **`output_schema` is a JSON Schema dict, not a class reference.** The router calls `MyModel.model_json_schema()` and ships the raw dict in the task payload. Workers validate with `model_validate()` on the other end. This decouples deployment of the router from workers.
- **Streaming tokens go to the `messages` collection, not the task document.** The task doc tracks lifecycle; messages handle content delivery. These concerns stay separated.
- **Firestore write contention:** the hard limit is ~1 write/sec per document. The session document should be mostly-static metadata. Rapidly-changing state (agent status, streaming) goes into subcollection docs or separate documents.

---

## File-by-File Reference

### Root Files

| File | Purpose |
|------|---------|
| `README.md` | User-facing documentation. Quick start, add-node, update instructions. |
| `pyproject.toml` | Package definition. Dependencies, optional extras (`[worker]`, `[dev]`), CLI entry point (`glitch = "glitch_core.cli:cli"`). |
| `.gitignore` | Ignores `pages_custom/`, `templates_custom/`, `tools/`, `.env`, credentials JSON. User-generated content stays local. |
| `glitch_core.yaml` | Agent configuration. Defines all agents (router + workers), their models, triggers, output schemas, timeouts, affinities, and capability requirements. Read at daemon startup, injected into the router's system prompt. Editable via `glitch edit-agents` or the web UI. |
| `firestore.rules` | Firestore security rules. Single-owner model — rules exist for structural validation (prevent invalid state transitions on sub_tasks, ensure workers only write their own heartbeat doc), not access control between users. Deployed by the migration runner when a migration declares `MigrationLayer.SECURITY_RULES`. |
| `install.sh` | Bootstrap script. Guides user through: prerequisites check → Firebase project creation → service account download → API key collection → venv + pip install → Firestore bootstrap → initial config. Writes `~/.glitch/.env` and `~/.glitch/credentials.json`. |
| `add_node.sh` | Worker node setup. Run on any machine to add it as a worker. Collects: Firebase project ID, service account JSON path, node name, capabilities (api/local/gpu/tailnet). Writes `~/.glitch/.env` on that machine. No registration tokens — the user owns the Firebase project and shares the same service account. |

### Core Package — `glitch_core/`

| File | Purpose |
|------|---------|
| `__init__.py` | Package root. Exports `__version__`. |
| `schemas.py` | **The central type contract.** All Pydantic models that map to Firestore documents and flow between agents/workers/clients. Includes: enums (`TaskStatus`, `TaskCommand`, `ModelTier`, `MessageRole`, `ContentRating`), task lifecycle models (`SubAgentTask`, `TaskError`, `TaskRouting`, `TaskAffinity`), agent output schemas (`CodeArtifact`, `CommandResult`, `ResearchResult`), message models (`ChatMessage`, `Attachment`), memory models (`JournalEntry`, `CoreMemory`), router↔worker protocol (`TaskQueued`, `TaskCompleted`), and config models (`AgentConfig`, `GlitchConfig`). |
| `config.py` | Configuration loading. `GlitchEnv` (pydantic-settings model reading from `~/.glitch/.env`): Firebase project ID, credentials path, API keys (Gemini, Anthropic, Ollama host), node name, node capabilities. Also loads and validates `glitch_core.yaml` into `GlitchConfig`. API keys stay in `.env` on each machine — never stored in Firestore. |
| `daemon.py` | **The main process.** Runs on the primary node (typically AWS). Single asyncio event loop running four concurrent tasks: (1) agent listener — subscribes to Firestore for user messages, runs the router agent, (2) worker loop — processes sub_tasks this node can handle, (3) web server — FastAPI/uvicorn on port 8080 accessible via Tailscale, (4) compaction scheduler — runs memory compaction on a cron schedule, (5) reaper loop — reclaims stale tasks from dead workers every 60 seconds. Started via `glitch start`. |
| `bootstrap.py` | First-run initialization. Called by `install.sh`. Creates initial Firestore collections, writes default agent config, writes default SOUL.md content, seeds empty collections with placeholder docs so they appear in the Firebase console, writes `~/.glitch/config.json` CLI pointer. |

### Workers — `glitch_core/workers/`

The worker subsystem handles distributed task execution across heterogeneous nodes.

| File | Purpose |
|------|---------|
| `__init__.py` | Exports worker components. |
| `protocol.py` | **The claim protocol.** Atomic task claiming using Firestore transactions — two workers hitting `try_claim_task()` simultaneously results in exactly one winner. Defines `ClaimResult`, `WorkerCapability` enum (`API`, `LOCAL`, `GPU`, `TAILNET`), `WorkerRegistration` model, and `TaskAffinity` enum (`ANY`, `PREFERRED`, `EXCLUSIVE`). |
| `loop.py` | **The worker daemon.** `WorkerDaemon` class with: `_register()` — writes worker doc to Firestore, `_heartbeat_loop()` — publishes liveness every 30 seconds, `_task_listener()` — subscribes to pending sub_tasks matching this worker's capabilities, `_can_handle()` — local filter checking affinity, capabilities, and agent support, `_try_and_execute()` — atomic claim then agent execution. Routes tasks to the correct PydanticAI agent from `AGENT_REGISTRY` keyed by `model_tier`. |
| `reaper.py` | **Stale task recovery.** Runs every 60 seconds in the daemon. Three responsibilities: (1) find claimed/running tasks where the worker stopped heartbeating (>2 min) and release them back to pending, (2) promote `PREFERRED` affinity tasks past their fallback window to `ANY` affinity with the fallback agent, (3) log (never reassign) `EXCLUSIVE` tasks that have been waiting a long time — these are Spicy tasks that wait until the local node comes back. Alerts the user after 24 hours. |
| `registration.py` | Worker self-registration. Handles the startup flow: read `.env`, connect to Firestore, write `WorkerRegistration` doc, pre-warm local models (Ollama `keep_alive`). |

### Workers — Key Design: The Spicy Worker

The "Spicy" worker runs an uncensored local Ollama model for generating sexually explicit content (legal but against cloud model guidelines). Key constraints:

- **Exclusive affinity only.** Spicy tasks never fall back to cloud models. They sit in the queue indefinitely if the local machine is offline.
- **`ContentRating` enum** (`SFW` / `NSFW`) on both `SubAgentTask` and `ChatMessage`. Hard-coded routing rule: NSFW content MUST route to Spicy, enforced both in the router's system prompt and programmatically in the spawn tool.
- **No fallback.** `fallback_agent: null` in config. The reaper never reassigns exclusive NSFW tasks.
- **When Spicy comes back online** after an outage, the worker daemon reconnects to Firestore, the snapshot listener fires for all pending tasks, and it claims them in order. A `priority: int` field on `SubAgentTask` allows ordering (default 0, time-sensitive tasks set higher).

### Compaction — `glitch_core/compaction/`

The memory compaction pipeline (the "Subconscious"). Decouples memory maintenance from the conversational loop.

| File | Purpose |
|------|---------|
| `__init__.py` | Exports pipeline components. |
| `pipeline.py` | **The main compaction pipeline.** `run_compaction()` function with four crash-safe phases: (1) Read — grab unprocessed journals oldest-first, load existing core_memories for cross-referencing, (2) Group & Summarize — batch journals and send to a PydanticAI summarization agent (Gemini Flash, cheap/fast) with existing memories as context, (3) Validate & Write — validate each `CompactedMemory` against Pydantic schema, quarantine low-confidence results to `memory_review` queue, write approved memories with version tracking, (4) Archive — copy consumed journals to `journals_archive`, mark originals as archived. Journals are NEVER deleted, only archived. The pipeline is idempotent — if it crashes mid-run, the next run retries safely. Includes `CompactionConfig` model (stored in Firestore at `/meta/compaction_config`, editable via web UI or CLI) with safety controls: `min_journals_to_trigger`, `max_journals_per_run`, `require_confidence` threshold, `never_compact_categories` list (relationship, identity, medical), `dry_run` mode. Also includes `CompactionRun` audit log model written to `/compaction_runs/{run_id}` for every execution. |
| `prompts.py` | **The summarization agent's system prompt and prompt construction.** The system prompt contains strict rules: preserve specifics (names, dates, numbers), merge don't duplicate, never infer beyond the data, importance scoring (1.0 for identity/relationships, 0.3 for casual mentions), confidence scoring (1.0 for explicit statements, 0.5 for ambiguous), contradiction handling (create updated memory referencing old one, don't silently drop). `_build_compaction_prompt()` constructs the per-batch prompt including existing memories as context so the model can merge/update rather than duplicate. |
| `rollback.py` | **Compaction run rollback.** `rollback_compaction_run(db, run_id)` — reverts all memories created/updated by a specific run. Updated memories revert to `previous_content`, new memories are deleted, archived journals are restored. Invoked via `glitch compaction rollback <run_id>` or the web UI. |

### Agents — `glitch_core/agents/`

PydanticAI agent definitions. Each agent is instantiated with its own model — model selection is a constructor argument.

| File | Purpose |
|------|---------|
| `__init__.py` | Exports agent factory functions. |
| `router.py` | **The chat agent / router.** Uses a cheap fast model (Gemini Flash). Handles conversational back-and-forth, decides when to delegate to sub-agents. System prompt is built dynamically at startup from `glitch_core.yaml` — the router sees a structured menu of available sub-agents with their triggers, models, and timeouts. Includes `spawn_sub_agent` tool that writes `SubAgentTask` docs to Firestore. Hard routing rules: NSFW → Spicy only, code tasks → coder agent, etc. The router's system prompt is rebuilt when the config changes (e.g., Ouroboros adds a new agent), so it instantly knows about new capabilities. Also responsible for passive journal writing — logging observations during conversations to the `journals` collection. |
| `coder.py` | **The code generation agent.** Uses a heavy reasoning model (Claude Opus). Output schema: `CodeArtifact` (filename, language, code, explanation, tests, sandbox_passed, git_sha). Used for tool generation, page generation, and general coding tasks. |
| `researcher.py` | **The research agent.** Uses Gemini Flash with web_search tools. Output schema: `ResearchResult` (query, summary, sources with URLs, confidence score). |
| `sysadmin.py` | **The system administration agent.** Uses Claude Sonnet with `execute_ssh`, `read_file`, `write_file` tools. Output schema: `CommandResult` (command, exit_code, stdout, stderr, host, duration_ms). Requires `tailnet` capability. |
| `spicy.py` | **The uncensored local model agent.** Uses Ollama with an uncensored model. Exclusive affinity, no fallback. Content rating: NSFW. See Workers section for full design rationale. |

### Agent Dispatch Pattern

The chat agent (Gemini) does NOT call other models directly. It uses a PydanticAI tool (`spawn_sub_agent`) that writes a `SubAgentTask` document to Firestore. Worker daemons on any Tailnet node pick up the task and route it to the correct PydanticAI agent from `AGENT_REGISTRY`. This decouples the router from execution and enables the cost optimization: 95% of turns are Gemini at fractions of a cent, Opus only spins up for code generation or complex reasoning.

Two handoff modes controlled by `blocking: bool` on `SubAgentTask`:
- **Synchronous** — tool call blocks until sub-agent finishes. Simpler, works for tasks under ~30 seconds.
- **Async** — tool returns immediately with "task queued," worker writes results to `messages` collection when done. Better UX for long-running tasks. Router must be aware of pending tasks on next user message.

### Ouroboros — `glitch_core/ouroboros/`

The self-improvement system. Safe, recursive code editing via blue/green deployment principles.

| File | Purpose |
|------|---------|
| `__init__.py` | Exports Ouroboros components. |
| `tool_generator.py` | **Tool generation pipeline.** When the main agent needs a new capability, it spawns a coder sub-agent to write a Python tool (e.g., `tools/experimental_data_parser.py`). The tool follows the PydanticAI tool interface. After sandbox validation, the file is committed via Git and the tools module is hot-reloaded via `importlib.reload()`. The agent config is also updated so the router's system prompt knows about the new capability. |
| `page_generator.py` | **UI page generation pipeline.** When a user requests a custom page, the coder agent generates BOTH a page module (Python FastAPI route) and a template (Jinja2 HTML). Both are validated in a sandbox, then promoted to `pages_custom/` and `templates_custom/`. The `PageEngine` hot-reloads without restart. The user sees a new nav item on next page load. The coder agent receives `CODER_PAGE_PROMPT` with the full tech stack constraints (FastAPI, Jinja2, HTMX, Tailwind, Firestore async, the glitch color palette, available collections). |
| `sandbox.py` | **Sandbox validation.** The safety layer for all Ouroboros operations. For tools: syntax check (`compile()`), Pydantic schema validation, dry-run in isolated subprocess. For pages: Python syntax check, Jinja2 template parse check, dry-run import, template render with mock data. For both: if sandbox fails, capture traceback and feed back to coder agent for retry. Production deployment should use `nsjail` or `bubblewrap` container with no network and read-only filesystem except tmp. **Critical safety concern:** rollback trigger — if a newly loaded tool/page causes the main agent to error on the next real conversation turn (not the dry-run), automatic rollback: `git revert`, reload, log incident. |
| `theme_generator.py` | **Theme generation.** Themes are Firestore documents (`GlitchTheme` model), not files. The coder agent generates a `GlitchTheme` JSON object from a natural language prompt ("pink and gothic", "corporate and clean", "match these company colors"). Includes WCAG-lite contrast validation — if the generated theme fails contrast checks, the agent is asked to fix it. Supports logo upload → dominant color extraction → theme generation. Preset themes ship with the repo (default, pink_gothic, corporate). Theme history is preserved in `/theme_history/` collection. |

### Ouroboros Safety — Key Concerns

1. **State leakage after `importlib.reload()`.** The module object is replaced, but existing references to old functions in live objects aren't updated. After reload, re-register tools by calling a `rebuild_tools()` method on the agent that re-scans the `tools/` directory.
2. **Supply-chain attack surface.** The coder agent writes Python that runs on the user's infra. Sandbox validation catches syntax errors and schema mismatches but won't catch destructive operations. Use `nsjail`/`bubblewrap` with no network and read-only filesystem.
3. **Automatic rollback circuit breaker.** If the newly promoted tool/page causes runtime errors, automatic `git revert` + reload + incident log. Non-technical users can't run `glitch pages rollback` manually.
4. **Git commit on every promotion.** Every tool and page promotion is committed to Git for rollback safety and audit trail.

### Migrations — `glitch_core/migrations/`

Schema migration system for safe updates across all layers.

| File | Purpose |
|------|---------|
| `__init__.py` | Exports `Migration` base class, `MigrationContext`, `MigrationLayer` enum, `MigrationRecord`. |
| `runner.py` | **The migration runner.** `MigrationRunner` class that: discovers migration files in `versions/` directory, checks Firestore for already-applied migrations, runs pending migrations in sequential order, records results as `MigrationRecord` docs in `/meta/migrations/history/`. Each migration is idempotent — running twice is a no-op. Failed migrations attempt automatic rollback via `down()`, then halt further processing. Called by `glitch update` after `git pull`. |
| `versions/__init__.py` | Package marker. |
| `versions/0001_initial.py` | **Initial migration.** Sets up base Firestore structure — should match what `bootstrap.py` creates. Exists so that future migrations can assume a known starting state. |

### Migration Design

Migrations are numbered, sequential Python files. Each defines a class inheriting from `Migration` with:
- `up(ctx)` — apply the migration
- `down(ctx)` — reverse it (must be safe even if `up()` partially ran)
- `check(ctx)` — optional pre-flight to skip if already applied (idempotency)
- `layers` — declares which layers are touched (`PYTHON`, `FIRESTORE_SCHEMA`, `CLOUD_FUNCTIONS` [removed], `SECURITY_RULES`, `CONFIG`)

The code lives in the git repo. `git pull` brings new migration files. `glitch update` discovers and runs them. Firestore records which migrations have been applied so the same migration never runs twice.

If a migration touches `SECURITY_RULES`, the runner automatically deploys `firestore.rules` to Firebase.

### CLI — `glitch_core/cli/`

The `glitch` command-line interface. Entry point defined in `pyproject.toml` as `glitch = "glitch_core.cli:cli"`.

| File | Purpose |
|------|---------|
| `__init__.py` | Click group root. Imports and registers all subcommands. |
| `main.py` | Core commands: `glitch start` (run daemon), `glitch stop`, `glitch restart`, `glitch status` (show all connected workers and pending tasks), `glitch edit-soul` (open soul in editor), `glitch edit-agents` (open agent config in editor). |
| `update.py` | **The single update command.** `glitch update`: git pull → pip install -e . → run pending migrations → deploy security rules if changed → restart daemon → check remote worker versions and warn if stale. Options: `--dry-run`, `--skip-pull`, `--skip-functions` [deprecated]. |
| `workers.py` | Worker management: `glitch worker start` (run worker daemon on this node), `glitch worker-token` [reserved/unused in single-tenant], `glitch worker status`. |
| `compaction.py` | Compaction management: `glitch compaction run` (manual trigger, defaults to dry-run), `glitch compaction status` (show last N runs), `glitch compaction rollback <run_id>`. |
| `pages.py` | Custom page management: `glitch pages list`, `glitch pages rollback` (git revert last page promotion). |

### Web UI — `glitch_core/web/`

Admin interface served by FastAPI alongside the main daemon. Accessible at `http://<tailscale-hostname>:8080`. No build step — Tailwind CDN + HTMX + Jinja2 templates.

| File | Purpose |
|------|---------|
| `__init__.py` | FastAPI app factory. Creates the `FastAPI` instance with lifespan handler that shares the Firestore client with the daemon. |
| `app.py` | App assembly. Mounts all route modules, static files, applies middleware, initializes `PageEngine`. |
| `middleware.py` | `ThemeMiddleware` — loads the current `GlitchTheme` from Firestore (cached 60 seconds) and injects it into Jinja2 template globals along with the nav registry. Every template gets `theme` and `nav` without individual routes passing them. |
| `theming.py` | **Theme system.** `GlitchTheme` Pydantic model with: `ThemeColors` (bg, surface, border, accent, text, muted, success, warning, error, category tag colors), typography (font_family, font_cdn for Google Fonts), shape (border_radius, border_width), branding (app_name, app_icon emoji, logo_url, favicon_url), layout (sidebar_width, compact_mode). Preset themes dict (`PRESET_THEMES`). Contrast validation function (`_passes_contrast_check` — WCAG-lite). Color palette extraction from uploaded logos (`_extract_palette` using Pillow). |
| `engine.py` | **Page discovery and hot-reload engine.** `PageEngine` class that scans `pages/` (core) and `pages_custom/` (AI-generated) directories for Python modules. Each module defines a `router` (APIRouter) and optionally `PAGE_META` (title, icon, nav_section, nav_order). Modules are imported via `importlib.util.spec_from_file_location`. `reload_custom_pages()` removes old custom routes, clears stale modules from `sys.modules`, and re-discovers — called by Ouroboros after promoting a new page. |

### Web UI — Route Modules — `glitch_core/web/pages/`

Each file is a self-contained FastAPI route module that returns `TemplateResponse`. Every module defines a `router` (APIRouter) and `PAGE_META` (for nav registration).

| File | Purpose |
|------|---------|
| `__init__.py` | Package marker. |
| `dashboard.py` | Landing page. Worker status cards (green/red dots based on heartbeat recency), pending review count badge, last compaction run summary, system health overview. |
| `memories.py` | **Core memory management.** Card layout (not a table). Each memory shows content, category as colored tag, confidence as a subtle bar. Click to expand → source journals, version history, edit/rollback buttons. Search bar, category filter as pills. HTMX partials for: `memory_detail` (expand card), `update_memory` (form submission → return fresh card), `rollback_memory` (revert to previous_content). Soft-delete moves to `memories_deleted` collection. |
| `soul.py` | Soul/personality editor. Full-screen text editor with preview. Version history in sidebar drawer via `/soul_history/`. Revert to any version with one click. Every edit snapshots the previous version before overwriting. |
| `review.py` | **Memory review queue.** Tinder-style: one card at a time. Shows proposed memory + source journal entries for context. Three actions: approve (promote to core_memories with confidence=1.0), edit (promote with modifications), reject (mark reviewed without promoting). Badge count on nav item for pending reviews. |
| `journals.py` | Journal browser. Searchable timeline view. Filter by topic. Mostly read-only — "what did the AI notice about our conversations." Toggle to include archived journals. Topic list for filter dropdown. |
| `workers.py` | Worker status page. All registered workers with online/offline status, capabilities, current task, last heartbeat. |
| `theme.py` | Theme management. Preset selection, custom generation via coder agent, logo upload with palette extraction. Theme picker as HTMX modal. `HX-Refresh: true` after applying a theme to reload the entire page with new colors. |
| `system.py` | System administration. Compaction history (last N runs with stats), manual compaction trigger (defaults to dry-run), compaction rollback, feature flag toggles. |

### Web UI — Templates — `glitch_core/web/templates/`

Jinja2 templates. All extend `base.html`. Use HTMX for interactivity. Tailwind classes use the `glitch-*` color palette which is dynamically configured from the `GlitchTheme` Firestore document.

| File | Purpose |
|------|---------|
| `base.html` | **Layout shell.** Imports: Tailwind CDN (configured with `glitch` color palette from theme), HTMX, optional Google Font from `theme.font_cdn`. Sidebar nav built dynamically from registered pages (grouped by `nav_section`: core, system, custom). Theme picker button at bottom of sidebar. Main content area with `{% block content %}`. Modal container div. HTMX swap animations (opacity transition). The `tailwind.config` object is built dynamically from `theme.colors` passed as a Jinja2 template variable. |
| `dashboard.html` | Dashboard layout. |
| `memories.html` | Memory browser with search, filters, card grid. |
| `soul.html` | Soul editor layout. |
| `review.html` | Review queue layout. |
| `journals.html` | Journal timeline layout. |
| `workers.html` | Worker status grid. |
| `theme.html` | Theme management layout. |
| `system.html` | System admin layout. |

### Web UI — Template Components — `glitch_core/web/templates/components/`

Reusable HTMX partials. These return HTML fragments that HTMX swaps in — they are NOT full pages.

| File | Purpose |
|------|---------|
| `memory_card.html` | Single memory card. Shows: category tag (color-coded), content, version number, confidence bar, edit button (triggers `hx-get` to `memory_detail`), undo button (if `previous_content` exists, triggers `hx-post` to rollback with `hx-confirm`). |
| `memory_detail.html` | Expanded memory view with edit form, source journals, version history. Returned by HTMX when clicking "Edit" on a memory card. |
| `worker_badge.html` | Worker status badge showing online/offline, capabilities, current task. |
| `stat_block.html` | Reusable statistic display (number + label + optional trend). |
| `confirm_modal.html` | Generic confirmation modal for destructive actions. |
| `theme_picker.html` | Theme selection modal. Preset swatches + custom generation form + logo upload. |
| `review_card.html` | Single review queue item. Proposed memory + source journals + approve/edit/reject buttons. |
| `journal_entry.html` | Single journal entry in the timeline view. |
| `nav.html` | Navigation sidebar partial (if separated from base.html). |

### Custom Directories (gitignored, user/AI-generated)

| Directory | Purpose |
|-----------|---------|
| `glitch_core/web/pages_custom/` | AI-generated page modules. Same contract as core pages (router + PAGE_META). Hot-reloaded by `PageEngine`. Git-committed by Ouroboros for rollback safety but gitignored from upstream. |
| `glitch_core/web/templates_custom/` | AI-generated Jinja2 templates. Loaded alongside core templates by Jinja2's multi-directory search. |
| `tools/` | AI-generated and user-created PydanticAI tools. Hot-reloaded via `importlib`. Each tool is a Python module that follows the PydanticAI tool interface. |
| `soul/SOUL.md` | The AI's personality file. Injected into the router's system prompt on every initialization. Editable via web UI or `glitch edit-soul`. |

### Tests — `tests/`

| File | Purpose |
|------|---------|
| `test_schemas.py` | Validate all Pydantic models serialize/deserialize correctly. Test enum coverage. Test `model_json_schema()` output for cross-process schema shipping. |
| `test_compaction.py` | Test compaction pipeline phases independently. Test idempotency (run twice → same result). Test rollback. Test low-confidence quarantine. Test crash recovery (simulate failure between phases). |
| `test_workers.py` | Test claim protocol (concurrent claims → exactly one winner). Test affinity routing (exclusive, preferred with fallback, any). Test reaper behavior (stale task release, preferred→fallback promotion). |
| `test_migrations.py` | Test migration discovery and ordering. Test idempotency (apply twice → no-op). Test rollback on failure. Test `check()` skip logic. |
| `test_theming.py` | Test contrast validation. Test theme serialization to Tailwind config. Test preset themes all pass contrast checks. Test color palette extraction. |

---

## Update & Migration Flow

The user's mental model: **`glitch update` does everything.**

```
glitch update
  ├── git pull --ff-only
  ├── pip install -e .                    (pick up new dependencies)
  ├── Run pending migrations              (sequential, idempotent)
  │   ├── Firestore schema changes        (backfill fields, restructure docs)
  │   ├── Config document updates         (new defaults, new fields)
  │   └── Security rules deployment       (auto-deploy firestore.rules if changed)
  ├── Restart daemon                      (pick up code changes)
  └── Check remote worker versions        (warn if stale)
```

Remote workers need to be updated separately — `glitch update` on the main node will warn about stale workers but can't force-update them. Each worker reports its `glitch_version` in its heartbeat document.

There is no published pip package. `pip install -e .` is a local editable install that symlinks the git repo into the venv. The "package" IS the cloned repo. Users own it, can edit it, and `git pull` updates it.

---

## Security Model

- **Tailscale provides the network boundary.** The web UI and daemon are only accessible to devices on the user's Tailnet. No public exposure, no SSL complexity, no auth layer needed.
- **Firebase service account JSON provides authentication.** Same key used on all the user's machines. Stored at `~/.glitch/credentials.json` with 0600 permissions.
- **API keys stay local.** Each machine reads its own `~/.glitch/.env`. Keys never transit through Firestore. Compromising Firestore exposes conversation history and memories, not API credentials.
- **Firestore rules are structural, not authorization.** All nodes are trusted (they're all the user's). Rules prevent invalid state transitions and enforce that workers only write their own heartbeat doc.
- **Ouroboros sandbox isolation.** AI-generated code runs in isolated subprocesses. Production deployments should use nsjail/bubblewrap. Automatic rollback on runtime errors.
- **Content isolation.** `ContentRating` enum on tasks and messages. Firestore security rules can filter by content_rating if Flutter clients gain multi-user support in the future.

---

## Configuration Hierarchy

```
~/.glitch/.env                   — Machine-local secrets (API keys, Firebase creds, node identity)
                                   NEVER in Firestore. NEVER in git.

glitch_core.yaml                 — Agent definitions (models, triggers, schemas, timeouts)
                                   In git. Editable via `glitch edit-agents` or web UI.
                                   Changes picked up on daemon restart.

Firestore /meta/theme            — UI theme. Editable via web UI or AI generation.
                                   Changes take effect on next page load (60s cache).

Firestore /meta/compaction_config — Compaction pipeline settings.
                                   Editable via web UI or `glitch edit-compaction`.
                                   Read fresh on every compaction run.

Firestore /meta/project          — Version marker, feature flags.
                                   Updated by migration runner.

Firestore /soul/default          — AI personality. Editable via web UI.
                                   Read on every agent initialization.
```

---

## Key Architectural Invariants

These are constraints that should hold true across all future development:

1. **No build step.** The web UI must remain functional with only CDN imports. If it requires `npm build`, the Ouroboros page generation system breaks.
2. **No Cloud Functions.** All compute runs in the daemon or worker loops. Firebase is storage only.
3. **No published package.** The codebase is the repo. `pip install -e .` is a local symlink, not a registry fetch.
4. **No multi-tenant.** One Firebase project per user. No tenant scoping in Firestore queries. No shared infrastructure.
5. **Journals are never deleted.** Compaction archives journals, never destroys them. The `journals_archive` collection is the permanent record.
6. **Core memories have rollback.** Every update preserves `previous_content`. Every compaction run is logged and reversible.
7. **Custom content survives updates.** `pages_custom/`, `templates_custom/`, and `tools/` are gitignored from upstream. `glitch update` never touches them.
8. **All Firestore documents map to Pydantic models.** No raw dict manipulation. Schema validation is the contract between all components.
9. **The router's system prompt is built from config, not hardcoded.** Adding a new agent to `glitch_core.yaml` automatically makes the router aware of it.
10. **API keys never touch Firestore.** Local `.env` only. Each machine only needs keys for models it runs.