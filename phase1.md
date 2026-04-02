# Claude Code Prompt — Phase 1: Core Daemon & Main Event Loop

## Context

Read `ARCHITECTURE.md` in the project root before doing anything. It contains the complete system design, file-by-file reference, and architectural invariants. Every decision in that document was deliberate — do not deviate from it without explaining why.

The project structure has been bootstrapped — all files exist but are blank. Your job is to implement the foundational layer that everything else builds on.

## Your Role

You are implementing Glitch Core, a distributed self-hosted AI system. You are an expert Python developer working with PydanticAI, FastAPI, Firebase Firestore, Jinja2, and HTMX. Write clean, typed, async Python. No shortcuts, no placeholders, no `# TODO` stubs unless explicitly told to defer something.

## Tech Constraints

- Python 3.11+
- All Firestore operations are async (`google-cloud-firestore` async client)
- All Pydantic models use v2 syntax
- PydanticAI for agent definitions
- FastAPI for the web layer
- Jinja2 templates with HTMX — no React, no build step
- Tailwind via CDN — no npm, no PostCSS
- Click for CLI
- pydantic-settings for environment config
- YAML for agent config

## Phase 1 Scope

Build the core daemon — the single process that runs on the primary node. By the end of this phase, a user should be able to:

1. Run `install.sh` to set up their environment
2. Run `glitch start` to launch the daemon
3. Open `http://localhost:8080` and see the admin dashboard
4. Send a message via the web UI and get a response from the router agent
5. See the soul editor and be able to modify it
6. See an empty memories page and empty journals page

### What to build (in dependency order):

**1. `glitch_core/schemas.py`**
All Pydantic models for Firestore documents. Implement every model described in ARCHITECTURE.md: enums (TaskStatus, TaskCommand, ModelTier, MessageRole, ContentRating), SubAgentTask, TaskError, TaskRouting, TaskAffinity, CodeArtifact, CommandResult, ResearchResult, Source, ChatMessage, Attachment, JournalEntry, CoreMemory, TaskQueued, TaskCompleted, AgentConfig, GlitchConfig, ProjectMeta, FeatureFlags. These are the type contracts everything else depends on.

**2. `glitch_core/config.py`**
- `GlitchEnv` — pydantic-settings model reading from `~/.glitch/.env` with `GLITCH_` prefix. Fields: firebase_project, firebase_credentials (Path), gemini_api_key (optional), anthropic_api_key (optional), ollama_host (optional), node_name (default "main"), node_capabilities (list[str], default ["api"]).
- YAML loader that reads `glitch_core.yaml` and validates it into `GlitchConfig`.
- A `get_firestore_client()` helper that creates an async Firestore client from the credentials path.

**3. `glitch_core/bootstrap.py`**
First-run Firestore initialization. Creates: `/meta/project` (ProjectMeta), `/meta/agent_config` (from glitch_core.yaml), `/soul/default` (default SOUL.md content), `/meta/compaction_config` (default CompactionConfig), `/meta/theme` (default GlitchTheme). Seeds empty collections with placeholder docs. Writes `~/.glitch/config.json`. Should be runnable via `python -m glitch_core.bootstrap` and also importable as a function.

**4. `glitch_core/agents/router.py`**
The chat agent. Use PydanticAI to define an agent with:
- Model from config (default `google-gla:gemini-2.5-flash`)
- Dynamic system prompt built from: soul content (loaded from Firestore `/soul/default`), core memories (loaded from Firestore `/core_memories`), and available sub-agent descriptions (built from `GlitchConfig.worker_agents()`)
- `result_type=ChatResponse` (define this — it's the structured output the router returns)
- A `spawn_sub_agent` tool definition (can be a stub that returns "Sub-agents not yet implemented" for Phase 1)
- A `write_journal` tool that passively logs observations to the `/journals` collection during conversations

For Phase 1, the router handles all conversation directly — no actual sub-agent dispatch. The tool definitions should exist with correct type signatures so Phase 2 can wire them up.

**5. `glitch_core/web/theming.py`**
The `GlitchTheme` and `ThemeColors` Pydantic models. The `PRESET_THEMES` dict (at minimum: default, pink_gothic, corporate). The `_passes_contrast_check()` function. This is needed before the templates work.

**6. `glitch_core/web/engine.py`**
The `PageEngine` class and `PageMeta` model. Discovery of page modules from `web/pages/` and `web/pages_custom/`. Module import via `importlib.util.spec_from_file_location`. Registration into a pages dict for nav building. `reload_custom_pages()` method (will be used by Ouroboros later, but implement the mechanics now).

**7. `glitch_core/web/middleware.py`**
`ThemeMiddleware` — loads current theme from Firestore (with 60-second in-memory cache), injects `theme` and `nav` into Jinja2 template globals.

**8. `glitch_core/web/app.py`**
FastAPI app assembly. Create the app, mount middleware, initialize PageEngine, discover and mount all page routers, configure Jinja2 templates (multi-directory: `templates/` + `templates_custom/`).

**9. `glitch_core/web/templates/base.html`**
The layout shell. Implement exactly as described in ARCHITECTURE.md: Tailwind CDN configured with glitch color palette from theme, HTMX import, optional Google Font from theme.font_cdn, sidebar nav built dynamically from registered pages grouped by section, theme picker button, main content block, modal container. The Tailwind config must be built dynamically from `theme.colors` passed as a template variable.

**10. `glitch_core/web/pages/dashboard.py` + `templates/dashboard.html`**
Landing page showing: worker count (will be 0 in Phase 1), pending review count, last compaction run (will be "never"), system version. Keep it simple but functional. Must define `router` and `PAGE_META`.

**11. `glitch_core/web/pages/memories.py` + `templates/memories.html` + `templates/components/memory_card.html` + `templates/components/memory_detail.html`**
Memory browser. Full implementation: list with search and category filter, card layout, HTMX expand/edit/rollback on cards, soft delete. This is a core screen — make it complete.

**12. `glitch_core/web/pages/soul.py` + `templates/soul.html`**
Soul editor. Text editor (textarea is fine — no need for CodeMirror in Phase 1), version history, revert. Every edit snapshots previous version to `/soul_history/`.

**13. `glitch_core/web/pages/journals.py` + `templates/journals.html` + `templates/components/journal_entry.html`**
Journal browser. Searchable timeline, topic filter, toggle archived. Read-only for now.

**14. `glitch_core/web/pages/review.py` + `templates/review.html` + `templates/components/review_card.html`**
Review queue page. Show pending items from `/memory_review`. Approve/edit/reject actions. Will be empty until compaction runs, but the page should work.

**15. `glitch_core/web/pages/system.py` + `templates/system.html`**
System page showing: compaction run history (empty initially), feature flags from `/meta/project`, current version.

**16. `glitch_core/web/pages/workers.py` + `templates/workers.html` + `templates/components/worker_badge.html`**
Workers page. Lists registered workers from `/workers` collection. Will show the main node once the daemon registers itself.

**17. `glitch_core/web/pages/theme.py` + `templates/theme.html` + `templates/components/theme_picker.html`**
Theme management. Preset selection with preview swatches. Apply preset → write to Firestore → `HX-Refresh: true`. The AI generation endpoint can be a stub for Phase 1 — just have the UI and the preset switching work.

**18. Remaining component templates**
`templates/components/stat_block.html`, `templates/components/confirm_modal.html`, `templates/components/nav.html` — implement as reusable partials used by the pages above.

**19. `glitch_core/daemon.py`**
The main process. Async event loop running concurrently:
- `_agent_listener()` — subscribes to Firestore `/sessions/{sid}/messages` where role == 'user'. On new message: loads soul + core_memories, builds system prompt, runs router agent, writes response to messages collection.
- `_web_server()` — uvicorn serving the FastAPI app on 0.0.0.0:8080.
- `_self_register()` — writes this node as a worker in `/workers` collection with heartbeat.
- `_heartbeat_loop()` — updates heartbeat every 30 seconds.

For Phase 1, defer these (leave as commented-out gather tasks with a note):
- `_worker_loop()` — Phase 2
- `_compaction_scheduler()` — Phase 3
- `_reaper_loop()` — Phase 2

The web UI needs a simple chat interface on the dashboard or a dedicated chat page so we can test the agent listener end-to-end. Add a `/chat` page if needed — it should write to a Firestore session's messages collection and display responses in real-time. This can be basic — a text input, a send button, and an HTMX-powered message list that polls or uses SSE.

**20. `glitch_core/cli/__init__.py` + `glitch_core/cli/main.py`**
Click CLI with at minimum:
- `glitch start` — runs the daemon (calls `daemon.main()`)
- `glitch bootstrap` — runs bootstrap (calls `bootstrap.bootstrap()`)
- `glitch status` — prints version, connected workers, basic health

Defer `update`, `workers`, `compaction`, `pages` subcommands to later phases — just register them as empty groups with a "Coming soon" message.

**21. `install.sh` + `add_node.sh`**
Implement the install script as described in ARCHITECTURE.md. It should actually work — prerequisites check, Firebase project guidance, credential collection, venv creation, pip install, env file creation, bootstrap call. `add_node.sh` similarly functional but simpler.

**22. `pyproject.toml` + `.gitignore`**
Properly configured. Dependencies, optional extras, CLI entry point, Python version requirement.

**23. `soul/SOUL.md`**
Write a reasonable default soul file. Direct, technical but not condescending, remembers context, can delegate to sub-agents, never fabricates memories.

## What NOT to build in Phase 1

- Worker claim protocol (`workers/protocol.py`, `workers/loop.py`, `workers/reaper.py`, `workers/registration.py`) — leave blank
- Compaction pipeline (`compaction/*`) — leave blank
- Ouroboros system (`ouroboros/*`) — leave blank
- Migration runner (`migrations/runner.py`, `migrations/versions/*`) — leave blank
- Sub-agent dispatch (the `spawn_sub_agent` tool should exist but stub out)
- AI-powered theme generation (the endpoint should exist but return "not yet implemented")
- Agent modules other than router (`agents/coder.py`, `agents/researcher.py`, `agents/sysadmin.py`, `agents/spicy.py`) — leave blank

## Code Style

- Type hints everywhere. Use `str | None` not `Optional[str]`.
- Async by default. Every Firestore operation uses the async client.
- Pydantic v2 syntax. `model_dump()` not `.dict()`. `model_validate()` not `.parse_obj()`.
- Imports at the top of files, grouped: stdlib, third-party, local.
- No wildcard imports.
- Docstrings on all public classes and functions.
- Use `from __future__ import annotations` in all Python files.
- Error handling: don't swallow exceptions. Log them with context. Let the daemon's top-level handler deal with crashes.
- Firestore document reads should always handle the case where the document doesn't exist.
- Templates: use Tailwind utility classes only. No custom CSS beyond the theme variables and HTMX transitions defined in base.html.

## Testing Approach

Don't write tests in Phase 1 — the test files exist but leave them blank. The validation is: can you run `install.sh`, `glitch start`, open the web UI, send a chat message, edit the soul, browse memories, and switch themes? If yes, Phase 1 is done.

## Important Details From Our Design Sessions

- API keys are NEVER stored in Firestore. They live in `~/.glitch/.env` on each machine.
- The router's system prompt is built dynamically from `glitch_core.yaml` at startup. It sees a structured menu of available sub-agents.
- Every soul edit snapshots the previous version. Every memory edit preserves `previous_content`.
- Firestore placeholder docs (`_placeholder`) should be filtered out of all queries.
- The web UI is behind Tailscale — no auth needed, no SSL needed. `0.0.0.0:8080` is safe because Tailscale handles access.
- Themes are Firestore documents read by middleware and injected into Jinja2 globals. Cached 60 seconds.
- Page modules define `PAGE_META` for nav registration. Nav is grouped by section: core, system, custom.
- HTMX partials return HTML fragments, not full pages. Full page routes return `TemplateResponse` extending `base.html`.
- The chat interface needs to work end-to-end: user types message → written to Firestore → daemon picks it up → router agent processes → response written to Firestore → UI updates. For Phase 1, polling (HTMX `hx-trigger="every 2s"`) is fine — SSE can come later.