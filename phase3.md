# Claude Code Prompt — Phase 3: Ouroboros, Workspace & Trust Zones

## Prerequisites

Phase 1 (core daemon, web UI, router agent, CLI) and Phase 2 (workers, claim protocol, sub-agent dispatch, reaper, compaction pipeline) must be complete before starting this phase.

Read `ARCHITECTURE.md` in the project root. It contains the full system design. Everything in this document builds on those foundations.

## Phase 3 Overview

This phase implements the self-improvement system (Ouroboros) and the free-form workspace. By the end of this phase:

1. The AI can extend its own tools, pages, and config through a validated pipeline that is **structurally enforced** — not suggested by a system prompt, but the only code path that exists.
2. The AI can build arbitrary projects for the user (scripts, websites, data files) in an unmanaged workspace that cannot affect the running system.
3. A runtime circuit breaker automatically reverts Ouroboros promotions that cause errors.
4. The web UI includes a workspace file browser for viewing and downloading generated files.
5. Theme generation via the coder agent works end-to-end.

## The Core Problem This Phase Solves

In OpenClaw, the AI has a general `write_file()` tool. The system prompt asks it nicely to validate before writing. It sometimes forgets. It sometimes writes to the wrong path. It sometimes modifies config in a way that's syntactically valid but semantically broken. The daemon reads the broken config and dies.

Glitch Core solves this structurally. There is no `write_file()` tool. The AI has zone-specific tools that enforce the appropriate safety level. The blue/green deployment isn't a suggestion — it's the only write path that exists.

## The Three Trust Zones

Every file operation the AI performs falls into exactly one of three zones. The zones are enforced by which tool the AI calls — there is no generic `write_file(path)` where a bad path could accidentally cross zones.

| Zone | Directory | Who Writes | Validation | Affects Daemon | AI Tools |
|------|-----------|-----------|------------|----------------|----------|
| Engine | `glitch_core/` | Humans only (git pull) | Code review | Yes — it IS the daemon | None — AI cannot touch this |
| System | `tools/`, `pages_custom/`, `templates_custom/` | AI via `SafeFileWriter` | Full pipeline (syntax, schema, AST scan, subprocess isolation, git commit, hot-reload, circuit breaker) | Yes — hot-reloaded into daemon | `create_tool`, `create_page`, `update_agent_config` |
| Workspace | `workspace/` | AI via `Workspace` | Path traversal check only | No — daemon never reads it | `workspace_write`, `workspace_read`, `workspace_run`, `workspace_list`, `workspace_delete` |

The AI physically cannot confuse the zones because they are different tool calls with different implementations. The tool determines the zone, and the zone determines the safety level.

---

## What to Build

### 1. `glitch_core/ouroboros/sandbox.py` — SafeFileWriter

The enforcement layer for the System zone. The AI's tools call methods on this class. This class owns the file system. The agent does not.

**Every System zone write follows this exact flow:**

```
1. Write to temp directory (blue environment)
2. Validate in isolation (multiple stages)
3. Git snapshot current state (for rollback)
4. Atomic swap into place (green promotion)
5. Git commit the new files
6. Hot-reload (importlib for tools, PageEngine for pages)
7. If reload fails → automatic git revert → restore previous state
```

If ANY step fails, the live system is never modified. There is no partially-written state.

**Implement the `SafeFileWriter` class with these methods:**

`write_tool(filename: str, code: str) -> PromotionResult`
- The ONLY way a tool module gets written to `tools/`.
- Writes to temp dir, validates (see validation stages below), snapshots current file via git, copies to `tools/`, git commits, attempts hot-reload. If reload fails, git reverts.
- Returns `PromotionResult` with success/failure, artifact_path, error details, and rollback_id (git SHA).

`write_page(page_filename: str, page_code: str, template_filename: str, template_code: str) -> PromotionResult`
- The ONLY way a page gets written to `pages_custom/` + `templates_custom/`.
- Both files are validated together. Either both promote or neither does.
- Python module validated as code, Jinja2 template validated separately.
- Same git snapshot → swap → commit → reload flow.
- If `PageEngine.reload_custom_pages()` fails, git reverts both files.

`write_config(config_content: str) -> PromotionResult`
- The ONLY way `glitch_core.yaml` gets modified.
- This is the thing that killed OpenClaw. Strictest validation:
  1. Parse as YAML (`yaml.safe_load`) — catches syntax errors
  2. Validate against `GlitchConfig` Pydantic model — catches schema errors
  3. Verify at least one router agent exists — catches "I deleted the only router"
  4. Verify all referenced output schemas exist as importable classes
  5. Git snapshot current config
  6. Write new config
  7. Git commit
- Does NOT attempt to rebuild the router's system prompt in this method — that happens on daemon restart or explicit reload. This keeps the blast radius contained.

**Validation stages for Python code (`_validate_python`):**

1. **Syntax** — `compile(code, filename, "exec")`. Fast-fail on syntax errors.
2. **Import** — Run in isolated subprocess via `subprocess.run()` with:
   - Timeout of 10 seconds
   - CWD set to the temp directory
   - Stripped environment: only `PATH` and `HOME` (set to temp dir). NO access to `~/.glitch`, no API keys, no Firebase credentials.
   - Import the module via `importlib.util.spec_from_file_location`
3. **Static analysis (AST scan)** — Parse the code with `ast.parse()` and walk the tree looking for dangerous patterns. Block these:
   - Dangerous function calls: `os.remove`, `os.unlink`, `os.rmdir`, `os.system`, `os.popen`, `os.exec*`, `shutil.rmtree`, `shutil.move`, `subprocess.run`, `subprocess.call`, `subprocess.Popen`
   - Dangerous imports: `os`, `subprocess`, `shutil`, `sys`, `ctypes`
   - Return a `ValidationFailure` with `fixable=False` for dangerous patterns
4. **Schema check** (for tools only) — verify the module defines something that looks like a PydanticAI tool (has a callable with type hints)

**Validation stages for Jinja2 templates (`_validate_template`):**

1. **Parse** — `jinja2.Environment().parse(code)`. Catches template syntax errors.
2. **Render with mock data** — Attempt to render the template with empty/mock context. Catches runtime errors like referencing undefined variables. This is a best-effort check — some variables only exist at runtime — but catches obvious mistakes.

**Pydantic models to define:**

- `PromotionResult` — success: bool, artifact_path: str | None, error: str | None, rollback_id: str | None (git SHA), validation_output: str | None
- `ValidationStage` — StrEnum: SYNTAX, IMPORT, SCHEMA, RENDER, RUNTIME
- `ValidationFailure` — stage: ValidationStage, error: str, fixable: bool (default True)

**Git helpers:**

- `_git_snapshot(paths, message)` → str | None — commit current state of files, return SHA
- `_git_commit(paths, message)` → str — add and commit files, return SHA
- `_git_revert(commit_sha)` — revert a specific commit

**Reload helpers:**

- `_try_reload_tools()` → str | None — importlib.reload on the tools module, return error or None
- `_try_reload_pages()` → str | None — call PageEngine.reload_custom_pages(), return error or None. The SafeFileWriter needs a reference to the PageEngine instance (pass it in the constructor).

### 2. `glitch_core/ouroboros/sandbox.py` — RuntimeCircuitBreaker

A separate class in the same file (or its own file if you prefer). Watches for errors after any Ouroboros promotion and automatically reverts if the error rate spikes.

**Behavior:**

- `record_promotion(sha: str)` — called after every successful promotion. Resets the error counter, records the timestamp.
- `record_error(error: Exception)` — called by the daemon's top-level error handler on every agent execution error. Logic:
  - If no recent promotion (no `last_promotion_sha`), do nothing.
  - If more than 5 minutes have passed since the last promotion, clear the promotion tracking and do nothing. The promotion is considered stable.
  - Increment `errors_since_promotion`.
  - If errors >= threshold (default 3): **automatic revert**. Git revert the promotion SHA. Reload tools and pages. Log a critical incident. Clear the promotion tracking.

**Integration point:** The daemon's agent listener wraps its `agent.run()` call in a try/except. On exception, it calls `circuit_breaker.record_error(e)` before re-raising or logging. After every successful Ouroboros promotion (any of the three SafeFileWriter methods), it calls `circuit_breaker.record_promotion(result.rollback_id)`.

### 3. `glitch_core/ouroboros/workspace.py` — Workspace

The free-form user zone. The daemon never imports from this directory. No hot-reload touches it. It's just a folder where the AI builds things for the user.

**Implement the `Workspace` class with these methods:**

`write(path: str, content: str | bytes) -> WorkspaceFile`
- Write any file to the workspace. No validation beyond path safety.
- Path is resolved relative to workspace root. Path traversal is blocked — `(self.root / path).resolve()` must start with `self.root.resolve()`.
- Additionally blocked: any path that resolves into `glitch_core/`, `tools/`, `soul/`, `.git/`, or `~/.glitch/`.
- Size limit: 50MB per file, 500MB total workspace.
- Creates parent directories as needed.

`read(path: str) -> str` — Read a text file from the workspace.

`read_bytes(path: str) -> bytes` — Read a binary file from the workspace.

`list(path: str = ".") -> WorkspaceTree` — List files in a workspace directory. Returns `WorkspaceTree` with `WorkspaceEntry` items (name, path, is_dir, size_bytes, modified_at) and total_size_bytes.

`delete(path: str) -> bool` — Delete a file or directory. Returns False if not found. Uses `shutil.rmtree` for directories.

`mkdir(path: str) -> WorkspaceFile` — Create a directory.

`run_script(script_path: str, args: list[str] | None, timeout: int = 300) -> ScriptResult`
- Execute a Python script FROM the workspace IN the workspace.
- CWD set to workspace root.
- The script gets the user's normal environment (including API keys from env, since these are the user's own scripts — not Ouroboros-generated system code).
- Timeout default 300 seconds.
- Capture stdout and stderr (truncate to last 5000/2000 chars respectively).
- Returns `ScriptResult` (exit_code, stdout, stderr, timed_out, success property).

**Pydantic models:**

- `WorkspaceFile` — path: str, size_bytes: int, created: bool, workspace_relative: str
- `WorkspaceTree` — files: list[WorkspaceEntry], total_size_bytes: int
- `WorkspaceEntry` — name: str, path: str, is_dir: bool, size_bytes: int, modified_at: datetime | None
- `ScriptResult` — exit_code: int, stdout: str, stderr: str, timed_out: bool, success: property

**Path safety is critical.** The `_resolve_safe(path)` method must:
1. Resolve the path: `(self.root / path).resolve()`
2. Verify it starts with `self.root.resolve()` (no traversal escape)
3. Verify it doesn't start with any forbidden directory path
4. Raise `PermissionError` on violations

### 4. `glitch_core/ouroboros/tool_generator.py`

The high-level orchestration for generating tools. This is the flow that the coder agent's `create_tool` tool uses internally, but with retry logic.

**Flow:**
1. Coder agent generates code based on the user's request
2. Code is passed to `SafeFileWriter.write_tool()`
3. If validation fails AND the failure is `fixable=True`, feed the error back to the coder agent and ask it to fix the code. Retry up to 3 times.
4. If all retries fail, return the last error to the user.

### 5. `glitch_core/ouroboros/page_generator.py`

Same pattern as tool_generator but for pages. The coder agent generates both a Python route module and a Jinja2 template.

**The coder agent needs a detailed prompt (`CODER_PAGE_PROMPT`) covering:**
- Tech stack: FastAPI APIRouter, Jinja2 templates, HTMX (hx-get, hx-post, hx-target, hx-swap), Tailwind CSS via CDN with glitch- color palette
- Requirements: module must define `router` (APIRouter) and `PAGE_META` (PageMeta), template must extend `base.html`, use HTMX for interactivity not custom JavaScript, all Firestore queries must be async
- Available Firestore collections and their schemas
- The glitch color palette variable names
- Available template components in `templates/components/`

**Retry flow:** Same as tool_generator — if validation fails with a fixable error, feed it back to the coder agent, retry up to 3 times.

### 6. `glitch_core/ouroboros/theme_generator.py`

Theme generation via the coder agent.

**`generate_theme(coder_agent, prompt: str) -> GlitchTheme`**
- Send prompt to coder agent with `GlitchTheme.model_json_schema()` as the expected output format
- Validate the result with `_passes_contrast_check()` from `theming.py`
- If contrast check fails, send the theme back to the coder agent with the specific failures and ask it to fix the colors. Retry up to 2 times.
- Write the validated theme to Firestore `/meta/theme`
- Save a snapshot to `/theme_history/`

**`generate_theme_from_logo(coder_agent, logo_bytes: bytes, prompt: str) -> GlitchTheme`**
- Extract dominant colors from the image using Pillow (quantize to reduce noise, Counter for most common colors)
- Include extracted palette in the coder agent prompt
- Same validation and retry flow as above
- Save the logo to a static files directory and set `theme.logo_url`

### 7. Agent Tool Registration

**`glitch_core/agents/coder.py`**
Define the coder agent with PydanticAI. Model: `anthropic:claude-opus-4-6`. The coder agent's tools are the SafeFileWriter methods:

- `create_tool(ctx, filename: str, code: str, description: str) -> PromotionResult`
- `create_page(ctx, page_filename: str, page_code: str, template_filename: str, template_code: str) -> PromotionResult`
- `update_agent_config(ctx, config_yaml: str) -> PromotionResult`

These are the ONLY tools on the coder agent. No `open()`, no `Path.write_text()`, no `os.anything`. The agent physically cannot write to disk outside of SafeFileWriter.

**`glitch_core/agents/router.py` (additions)**
The router agent gets BOTH tool sets. Add workspace tools alongside the existing spawn_sub_agent and write_journal tools:

- `workspace_write(ctx, path: str, content: str) -> WorkspaceFile`
- `workspace_read(ctx, path: str) -> str`
- `workspace_list(ctx, path: str = ".") -> WorkspaceTree`
- `workspace_run(ctx, script_path: str, args: list[str] | None, timeout: int = 300) -> ScriptResult`
- `workspace_delete(ctx, path: str) -> bool`

Add a `WORKSPACE_ROUTING_PROMPT` section to the router's system prompt that explains the two zones:

- **System zone tools** (`create_tool`, `create_page`, `update_agent_config`): Use when the user wants to extend Glitch's own capabilities or modify its behavior. These go through the validated pipeline.
- **Workspace zone tools** (`workspace_*`): Use when the user wants something BUILT for them — scripts, websites, data files, configs for other tools. These are free-form.
- **The rule:** If it changes how Glitch works → System zone. If it's something the user wants built → Workspace zone. When in doubt, use workspace. It's safer.

### 8. Web UI — Workspace Browser

**`glitch_core/web/pages/workspace.py`**
A file browser page for the workspace directory. Non-technical users need to see and download files the AI has built for them.

- `GET /workspace` and `GET /workspace/{path:path}` — if path is a directory, render the file browser. If path is a file, serve it as a download (`FileResponse`).
- Show: file name, size, last modified date, file type icon.
- Navigate into subdirectories.
- "Back" link to parent directory.
- Download button on files.
- Delete button on files/directories (with HTMX confirm modal).

**`glitch_core/web/templates/workspace.html`**
Template extending base.html. Show a breadcrumb path, a table/grid of files and directories, and action buttons.

**PAGE_META:** title="Workspace", icon="📁", nav_section="core", nav_order=5

### 9. Web UI — Theme Generation Endpoint

**Update `glitch_core/web/pages/theme.py`:**

The theme page from Phase 1 has a stub for AI generation. Replace the stub with the real implementation:

- `POST /theme/generate` — accepts `prompt` form field, calls `generate_theme()` from `theme_generator.py`, writes result to Firestore, returns `HX-Refresh: true`.
- `POST /theme/from-logo` — accepts `logo` file upload and optional `prompt` form field, calls `generate_theme_from_logo()`, returns `HX-Refresh: true`.

Update the theme picker template to include: a text input for natural language theme requests, a file upload for logo-based theme generation, and preview of the current theme colors.

### 10. Daemon Integration

**Update `glitch_core/daemon.py`:**

- Instantiate `SafeFileWriter` and `Workspace` at daemon startup.
- Pass both to the router agent builder.
- Instantiate `RuntimeCircuitBreaker` with a reference to the `SafeFileWriter`.
- In the agent listener's error handler, call `circuit_breaker.record_error(e)`.
- After any successful Ouroboros promotion (detected via `PromotionResult.success`), call `circuit_breaker.record_promotion(result.rollback_id)`.
- Make `workspace` available on `app.state` for the web UI workspace browser.

### 11. Bootstrap Updates

**Update `glitch_core/bootstrap.py`:**
- Create the `workspace/` directory during bootstrap.
- Add a `.gitkeep` to `workspace/`.

**Update `bootstrap_glitch.py`** (if regenerating the project structure):
- Add `workspace/.gitkeep` to the file list.
- Add `glitch_core/web/pages/workspace.py` to the file list.
- Add `glitch_core/web/templates/workspace.html` to the file list.

**Update `.gitignore`:**
- Add `workspace/` (user-generated content, not tracked upstream).
- Keep `tools/`, `pages_custom/`, `templates_custom/` gitignored from upstream but tracked locally by Ouroboros git commits.

---

## What NOT to Build in Phase 3

- `nsjail` / `bubblewrap` container isolation for subprocess validation — document it as a production hardening step but use basic subprocess isolation for now.
- Workspace script execution in Docker containers — use direct subprocess for now.
- Multi-file atomic promotions beyond the page (route + template) pair — single tool files and page pairs cover all current use cases.
- Workspace sharing or collaboration features.
- Workspace version control (the workspace is intentionally unmanaged — if the user wants git, they can init it themselves).

## Code Style

Same as Phase 1:
- Type hints everywhere. `str | None` not `Optional[str]`.
- Async by default.
- Pydantic v2 syntax.
- `from __future__ import annotations` in all files.
- Docstrings on all public classes and functions.
- Error handling: don't swallow exceptions. The circuit breaker is the safety net, not silent try/except blocks.

## Definition of Done

Phase 3 is complete when:

1. You can tell the AI "create a tool that fetches weather data" and it generates a Python module, validates it in a sandbox, git commits it, hot-reloads it, and the router agent can use it on the next conversation turn.
2. You can tell the AI "build me an image generation script that calls ComfyUI" and it writes files to `workspace/`, and you can see and download them from the web UI.
3. You can tell the AI "make the app pink and gothic" and the theme changes immediately on page refresh.
4. If the AI generates a broken tool that passes validation but causes runtime errors, the circuit breaker reverts it automatically within 3 errors.
5. The AI cannot write to `glitch_core/`, `tools/`, or any system directory via workspace tools. The AI cannot write to `workspace/` via system tools. The zones are structurally separated.
6. The AI cannot call `open()`, `Path.write_text()`, `os.remove()`, `subprocess.run()`, or any raw file/process operation. These do not exist in its tool registry. The only write paths are `SafeFileWriter` methods and `Workspace` methods.

## Key Architectural Invariant

**The AI's inability to bypass the pipeline is not enforced by the system prompt. It is enforced by the tool registry.** The system prompt explains WHEN to use each tool. The tool registry controls WHAT the AI can do. Even if the system prompt is ignored, jailbroken, or confused, the agent still cannot write to disk outside of the defined tools — because `open()` is not a tool, `Path.write_text()` is not a tool, and `subprocess.run()` is not a tool. The PydanticAI agent can only call functions registered as tools. Everything else is inaccessible.
