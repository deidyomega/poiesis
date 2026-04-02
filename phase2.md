# Claude Code Prompt — Phase 2: Workers, Sub-Agents & Compaction

## Prerequisites

Phase 1 (core daemon, web UI, router agent, CLI) must be complete. The following should already work: `glitch start` launches the daemon, the web UI is accessible, the router agent responds to messages, soul editing works, memory/journal pages render.

Read `ARCHITECTURE.md` in the project root for the full system design. This phase builds the distributed execution layer on top of the Phase 1 foundation.

## Phase 2 Overview

This phase implements the distributed worker system and the memory compaction pipeline. By the end of this phase:

1. The router agent can spawn sub-agent tasks via Firestore
2. Worker daemons on any Tailnet node pick up and execute tasks
3. The claim protocol guarantees exactly one worker claims each task
4. The reaper recovers stale tasks from dead workers
5. The Spicy worker (local Ollama, exclusive affinity) queues tasks indefinitely when offline
6. The compaction pipeline distills journal entries into core memories on a schedule
7. The memory review queue in the web UI is functional end-to-end
8. The CLI has working `glitch worker start` and `glitch compaction` commands

---

## What to Build (in dependency order)

### 1. `glitch_core/schemas.py` — Additions

Phase 1 defined the core models. Add or verify these exist with the exact fields specified:

**`TaskAffinity` (StrEnum):**
- `ANY` — any capable worker, first come first served
- `PREFERRED` — try specific worker, fall back after timeout
- `EXCLUSIVE` — ONLY this worker, wait indefinitely

**`TaskRouting` (BaseModel):**
- `affinity: TaskAffinity = TaskAffinity.ANY`
- `target_worker: str | None = None` — worker_id for preferred/exclusive
- `required_capabilities: list[WorkerCapability] = Field(default_factory=list)`
- `claim_timeout: timedelta | None = None`
- `execution_timeout: timedelta = timedelta(seconds=60)`
- `fallback_agent: str | None = None` — agent name to fall back to (for PREFERRED)
- `fallback_after: timedelta = timedelta(minutes=5)` — how long to wait before fallback

**`WorkerCapability` (StrEnum):**
- `API` — can call cloud model APIs (Gemini, Claude)
- `LOCAL` — can run local models (Ollama)
- `GPU` — has CUDA GPU
- `TAILNET` — on the Tailscale network

**`WorkerRegistration` (BaseModel):**
- `worker_id: str`
- `hostname: str`
- `capabilities: list[WorkerCapability]`
- `supported_agents: list[str]` — agent names from config this worker can run
- `last_heartbeat: datetime`
- `status: str = "online"` — "online", "draining", "offline"
- `current_task: str | None = None`
- `glitch_version: str` — for stale version detection during updates
- `node_name: str` — from GlitchEnv

**`ContentRating` (StrEnum):**
- `SFW = "sfw"`
- `NSFW = "nsfw"`

Add `content_rating: ContentRating = ContentRating.SFW` to both `SubAgentTask` and `ChatMessage` if not already present.

Add `routing: TaskRouting = Field(default_factory=TaskRouting)` to `SubAgentTask` if not already present.

Add `priority: int = 0` to `SubAgentTask` — higher priority tasks are claimed first. Default 0, time-sensitive tasks set higher by the router.

**`ClaimResult` (BaseModel):**
- `claimed: bool`
- `task_id: str`
- `reason: str | None = None` — why claim failed

**`CompactionConfig` (BaseModel):**
Stored in Firestore at `/meta/compaction_config`. Editable via web UI.
- `enabled: bool = True`
- `schedule_cron: str = "0 3 * * *"` — 3 AM daily
- `model: str = "google-gla:gemini-2.5-flash"`
- `min_journals_to_trigger: int = 5`
- `max_journals_per_run: int = 100`
- `batch_size: int = 10` — journals per LLM call
- `max_memories_per_run: int = 20`
- `require_confidence: float = 0.7` — below this, flag for review
- `archive_journals: bool = True`
- `dry_run: bool = False`
- `never_compact_categories: list[str] = Field(default_factory=lambda: ["relationship", "identity", "medical"])`
- `min_memory_age_hours: int = 24`

**`CompactionRun` (BaseModel):**
Audit log written to `/compaction_runs/{run_id}`.
- `run_id: str`
- `started_at: datetime`
- `completed_at: datetime | None`
- `status: str = "running"` — running, completed, failed, skipped, dry_run, rolled_back
- `journals_read: int = 0`
- `journals_archived: int = 0`
- `memories_created: int = 0`
- `memories_updated: int = 0`
- `memories_flagged: int = 0`
- `errors: list[CompactionError] = Field(default_factory=list)`
- `config_snapshot: dict = Field(default_factory=dict)`

**`CompactionError` (BaseModel):**
- `stage: str` — "grouping", "summarization", "validation", "write"
- `message: str`
- `journal_ids: list[str] = Field(default_factory=list)`
- `recoverable: bool = True`

**`CompactedMemory` (BaseModel):**
The structured output from the summarization LLM.
- `category: str`
- `content: str`
- `importance: float = Field(ge=0.0, le=1.0)`
- `confidence: float = Field(ge=0.0, le=1.0)`
- `source_journal_ids: list[str]`
- `related_memory_ids: list[str] = Field(default_factory=list)`

**`CompactionResult` (BaseModel):**
What the summarization model returns per batch.
- `memories: list[CompactedMemory]`
- `discarded: list[DiscardedJournal] = Field(default_factory=list)`

**`DiscardedJournal` (BaseModel):**
- `journal_id: str`
- `reason: str` — "duplicate", "trivial", "superseded"

### 2. `glitch_core/workers/protocol.py` — Claim Protocol

The atomic task claiming system using Firestore transactions.

**`try_claim_task(db, session_id, task_id, worker_id) -> ClaimResult`**

This function runs inside a Firestore transaction (`@async_transactional`). The transaction:

1. Reads the task document
2. Checks `status == "pending"` — if not, return `ClaimResult(claimed=False, reason=f"already_{status}")`
3. Checks `command != "abort"` — if aborted, return `ClaimResult(claimed=False, reason="aborted")`
4. Writes `status="claimed"`, `claimed_by=worker_id`, `claimed_at=datetime.utcnow()`
5. Returns `ClaimResult(claimed=True, task_id=task_id)`

Two workers hitting this simultaneously: the Firestore transaction guarantees exactly one wins. The loser gets `claimed=False` and moves on. This is the core correctness guarantee of the distributed system.

### 3. `glitch_core/workers/registration.py` — Worker Self-Registration

Handles the startup flow for any worker node.

**`register_worker(db, env: GlitchEnv, config: GlitchConfig) -> WorkerRegistration`**

1. Determine which agents this node can run based on its capabilities and available API keys:
   - If `env.gemini_api_key` is set → can run agents with `google-gla:*` models
   - If `env.anthropic_api_key` is set → can run agents with `anthropic:*` models
   - If `env.ollama_host` is set → can run agents with `ollama:*` models
   - Cross-reference with `env.node_capabilities` and each agent's `requires` field
2. Build `WorkerRegistration` with: worker_id (use `env.node_name`), hostname (`socket.gethostname()`), capabilities, supported_agents list, glitch_version
3. Write to Firestore `/workers/{worker_id}`
4. If this node supports Ollama, pre-warm the model: send a minimal generation request with `keep_alive="24h"` to keep the model loaded in VRAM
5. Return the registration

### 4. `glitch_core/workers/loop.py` — Worker Daemon

The `WorkerDaemon` class. This runs on every node that processes tasks (including the main node).

**Constructor takes:** `db` (Firestore client), `env` (GlitchEnv), `config` (GlitchConfig), `agent_registry` (dict mapping agent names to PydanticAI Agent instances)

**`run()` method:**
Run concurrently via `asyncio.gather`:
- `_register()` — call `register_worker()` from registration.py
- `_heartbeat_loop()` — every 30 seconds, update `/workers/{worker_id}` with `last_heartbeat=datetime.utcnow()` and `status="online"`
- `_task_listener()` — the main work loop

**`_task_listener()` method:**
Subscribe to Firestore `collection_group("sub_tasks")` where `status == "pending"`. On each new/changed document:
1. Call `_can_handle(task_data)` — local filter, don't even try to claim tasks we can't run
2. If we can handle it, call `_try_and_execute(doc, task_data)`

**`_can_handle(task_data) -> bool` method:**
The local routing filter. Check in order:

1. **Exclusive affinity to another worker?** If `routing.affinity == EXCLUSIVE` and `routing.target_worker != self.worker_id` → skip
2. **Preferred for another worker and not yet timed out?** If `routing.affinity == PREFERRED` and `routing.target_worker != self.worker_id`:
   - Calculate age since `created_at`
   - If age < `routing.fallback_after` → skip (give the preferred worker time)
   - If age >= `routing.fallback_after` → we're eligible (preferred window expired)
3. **Capability match?** Check that `routing.required_capabilities` is a subset of `self.registration.capabilities`
4. **Agent support?** Check that `task_data["agent_name"]` is in `self.registration.supported_agents`

All checks must pass to return True.

**`_try_and_execute(doc, task_data)` method:**

1. Extract session_id from the document reference path (`doc.reference.parent.parent.id`)
2. Call `try_claim_task()` — if claim fails, return silently
3. Update task status to "running" and `started_at`
4. Look up the PydanticAI agent from `self.agent_registry[task_data["agent_name"]]`
5. Run the agent: `result = await agent.run(task_data["prompt"])`
6. On success: update task with `status="completed"`, `result=result.data.model_dump()`, `completed_at`
7. On exception: update task with `status="failed"`, `error=TaskError(error_type="execution", message=str(e), retryable=True).model_dump()`
8. Write the result as a `ChatMessage` to the session's messages collection with `role=MessageRole.SYSTEM`, `agent_name=task_data["agent_name"]`, `task_id=task_id`

### 5. `glitch_core/workers/reaper.py` — Stale Task Recovery

**`reap_stale_tasks(db)` function:**

Runs every 60 seconds in the daemon. Three responsibilities:

**1. Release tasks from dead workers:**
- Read all worker docs from `/workers`
- Identify dead workers: `last_heartbeat` older than 2 minutes
- Query `collection_group("sub_tasks")` for tasks with `status in ["claimed", "running"]` and `claimed_by in dead_worker_ids`
- For each stale task: update `status="pending"`, clear `claimed_by`, `claimed_at`, append to `logs` array: `"Released from dead worker {worker_id} at {timestamp}"`

**2. Promote preferred→fallback tasks past their window:**
- Query pending tasks
- For each task where `routing.affinity == PREFERRED`:
  - Calculate age since `created_at`
  - If age > `routing.fallback_after` AND `routing.fallback_agent` is set:
    - Update `agent_name` to `routing.fallback_agent`
    - Update `routing.affinity` to `ANY`
    - Clear `routing.target_worker`
    - Append to logs: `"Fell back from {target_worker} to {fallback_agent} after {age}s"`

**3. Monitor exclusive tasks (never reassign):**
- For pending tasks where `routing.affinity == EXCLUSIVE`:
  - These are Spicy tasks — they wait indefinitely by design
  - If waiting longer than 24 hours: log a warning, optionally notify (just log for now)
  - NEVER reassign or promote exclusive tasks. The whole point is they wait for their specific worker.

### 6. `glitch_core/agents/router.py` — Sub-Agent Dispatch (upgrade from Phase 1)

Phase 1 has a stub `spawn_sub_agent` tool. Replace it with the real implementation.

**`spawn_sub_agent` tool:**

```python
@chat_agent.tool
async def spawn_sub_agent(ctx, task: SubAgentTask) -> TaskQueued:
```

1. If `task.content_rating == ContentRating.NSFW`: hard-enforce exclusive routing to Spicy. Set `routing.affinity = EXCLUSIVE`, `routing.target_worker = "spicy"`, `routing.fallback_agent = None`, `routing.claim_timeout = None`. This is a programmatic enforcement, not a system prompt suggestion.
2. Write the task document to `sessions/{session_id}/sub_tasks/{auto_id}` with `status="pending"` and `created_at=SERVER_TIMESTAMP`
3. Check if the target worker is online (read its worker doc, check heartbeat recency)
4. Return `TaskQueued` with: task_id, agent_name, and a status message indicating whether the relevant worker is online or the task is queued for later pickup

**The router's system prompt must include routing heuristics** built from `glitch_core.yaml`:
- List each worker agent with its triggers, model, and timeout
- Hard rules: "You MUST spawn a code-tier sub-agent for any task involving writing, modifying, or reviewing code"
- Hard rules: "NSFW content MUST be routed to 'spicy'. No exceptions. NEVER attempt NSFW generation with any cloud-hosted model."
- "If spicy is offline, inform the user the task is queued."

**Async task completion handling:**

When a sub-task completes (worker writes to messages collection), the router needs to be aware of it on the next user message. The daemon's agent listener should check for pending/recently-completed tasks in the session and include their results in the router's context for the next turn.

Implement this by:
1. Before running the router agent on a new user message, query `sub_tasks` for the session
2. Find tasks with `status in ["completed", "failed"]` that haven't been acknowledged
3. Inject their results into the router's message history as system messages
4. Mark them as acknowledged (add an `acknowledged: true` field)

### 7. `glitch_core/agents/coder.py`, `researcher.py`, `sysadmin.py`, `spicy.py`

Define each agent with PydanticAI. These are the worker-side agents that actually execute tasks.

**`coder.py`:**
- Model: `anthropic:claude-opus-4-6`
- System prompt: coding-focused, aware of the project structure, outputs `CodeArtifact`
- Result type: `CodeArtifact`
- No tools in Phase 2 — tools come in Phase 3 (SafeFileWriter). For now, the coder agent just returns structured code output. The worker writes the result to Firestore; it doesn't execute it.

**`researcher.py`:**
- Model: `google-gla:gemini-2.5-flash`
- System prompt: research-focused, web search oriented
- Result type: `ResearchResult`
- Tools: if PydanticAI supports a web search tool for Gemini, wire it up. Otherwise, the researcher agent works from its training data for now.

**`sysadmin.py`:**
- Model: `anthropic:claude-sonnet-4-6`
- System prompt: sysadmin-focused, cautious about destructive commands
- Result type: `CommandResult`
- Tools: `execute_ssh` (stub for Phase 2 — the tool signature should exist but return "SSH execution not yet implemented"). Full Tailnet SSH integration is a follow-up.

**`spicy.py`:**
- Model: `ollama:{model_name}` — model name from config
- System prompt: uncensored creative writing, no content restrictions
- Result type: a simple `SpicyResult(BaseModel)` with `content: str`
- The Ollama model string format for PydanticAI needs to be verified. Check PydanticAI docs for Ollama provider syntax.

**Agent registry factory:**

Create a function `build_agent_registry(config: GlitchConfig, env: GlitchEnv) -> dict[str, Agent]` that:
1. Reads the agent configs from `GlitchConfig`
2. For each worker agent, instantiates the appropriate PydanticAI Agent
3. Only creates agents for which the node has the required API keys/capabilities
4. Returns a dict mapping agent names to Agent instances

This is what gets passed to `WorkerDaemon`.

### 8. `glitch_core/compaction/pipeline.py` — The Compaction Pipeline

The full pipeline as described in ARCHITECTURE.md. Four crash-safe phases:

**Phase 1 — Read:**
- Query `/journals` where `archived == False`, ordered by `timestamp`, limited to `config.max_journals_per_run`
- If fewer than `config.min_journals_to_trigger` journals exist, set run status to "skipped" and return
- Load all existing `/core_memories` into a dict for cross-referencing

**Phase 2 — Group & Summarize:**
- Build a PydanticAI summarization agent using `config.model` with the compaction system prompt
- Batch journals into groups of `config.batch_size`
- For each batch, build a prompt that includes:
  - All existing core memories with their IDs, categories, content, version, and confidence
  - The batch of journal entries with their IDs, topics, details, timestamps, and session IDs
  - Instruction to return a `CompactionResult`
- Run the agent, collect `CompactionResult` per batch
- On per-batch errors: log a `CompactionError`, continue with remaining batches (don't abort the whole run)

**Phase 3 — Validate & Write:**
- For each `CompactedMemory` in the results:
  - If `confidence < config.require_confidence`: write to `/memory_review` instead of `/core_memories`, increment `memories_flagged`
  - If `related_memory_ids` references an existing memory: update that memory (set new content, shift old content to `previous_content`, bump version), increment `memories_updated`
  - Otherwise: create a new core memory, increment `memories_created`
- Track which journal IDs were consumed by successfully written memories

**Phase 4 — Archive:**
- ONLY archive journals whose compacted output was confirmed written
- For each consumed journal: copy to `/journals_archive` with `archived_at` and `compaction_run` fields, then update the original with `archived=True`
- Journals that were NOT consumed (because their batch errored, or the LLM discarded them) remain unarchived for the next run

**Crash safety contract:**
- Crash before writing memories → nothing changed, next run retries
- Crash after writing memories but before archiving journals → next run re-processes those journals, merge logic in the LLM prompt prevents duplicates
- Crash after archiving → run is complete, audit log may be incomplete but no data is lost

Write a `CompactionRun` audit log to `/compaction_runs/{run_id}` at the end of every execution regardless of outcome.

If `config.dry_run == True`: run phases 1 and 2, log what WOULD have been written, but skip phases 3 and 4.

### 9. `glitch_core/compaction/prompts.py` — Compaction System Prompt

The system prompt for the summarization agent. This is critical — a bad prompt means bad memories.

**Rules the prompt must encode:**
1. PRESERVE SPECIFICS. Names, dates, numbers, preferences — keep them exact. "User's girlfriend is vegetarian" not "User's partner has dietary preferences."
2. MERGE, DON'T DUPLICATE. If a journal entry confirms or updates an existing memory, reference that memory's ID in `related_memory_ids`. Don't create a new memory that says the same thing.
3. NEVER INFER BEYOND THE DATA. If journals mention the user talked about buying a car, the memory is "User discussed buying a car" not "User is planning to buy a car."
4. IMPORTANCE SCORING: 1.0 for identity/relationships/medical, 0.8 for active projects/goals, 0.5 for preferences/opinions, 0.3 for casual mentions. Below 0.3 probably not worth a core memory — add to discarded with reason.
5. CONFIDENCE SCORING: 1.0 for explicitly stated by user, 0.8 for strongly implied across multiple entries, 0.5 for mentioned once, below 0.5 for ambiguous — still create but flag for review.
6. CONTRADICTION HANDLING: If new journal contradicts existing memory, create updated memory with NEW information and reference the old memory ID. Do NOT silently drop the old fact.
7. NEVER discard journals about relationships, identity, medical information, or strong preferences. Even if they seem trivial. Err on the side of keeping.

**Prompt construction function:**

`build_compaction_prompt(journal_docs: list, existing_memories: dict) -> str`

Builds a prompt with two sections:
1. "Existing Core Memories" — each memory with its ID, category, content, version, confidence
2. "Journal Entries to Compact" — each journal with its ID, topic, detail, timestamp, source_session

Ends with: "Compact these journal entries into core memories. Return ONLY the structured CompactionResult."

### 10. `glitch_core/compaction/rollback.py` — Compaction Rollback

**`rollback_compaction_run(db, run_id: str)`**

1. Load the compaction run doc from `/compaction_runs/{run_id}`
2. Query `/core_memories` where `compaction_run == run_id`:
   - If `previous_content` exists: revert content to `previous_content`, decrement version, clear `previous_content`
   - If `previous_content` is None: this was a new memory created by this run — delete it
3. Query `/journals_archive` where `compaction_run == run_id`:
   - Restore each journal to `/journals` collection with `archived=False`
   - Delete the archive doc
4. Update the compaction run doc with `status="rolled_back"` and `rolled_back_at`

### 11. Daemon Integration

**Update `glitch_core/daemon.py`:**

The daemon's `asyncio.gather` now includes:

```python
await asyncio.gather(
    self._agent_listener(),          # Phase 1 — already implemented
    self._web_server(),              # Phase 1 — already implemented
    self._self_register(),           # Phase 1 — already implemented
    self._heartbeat_loop(),          # Phase 1 — already implemented
    self._worker_loop(),             # NEW — Phase 2
    self._compaction_scheduler(),    # NEW — Phase 2
    self._reaper_loop(),             # NEW — Phase 2
)
```

**`_worker_loop()`:**
Instantiate `WorkerDaemon` with the agent registry and run it. The main node is both the router AND a worker — it can execute sub-tasks locally if it has the capabilities.

**`_compaction_scheduler()`:**
Loop: load `CompactionConfig` from Firestore, calculate next run time from `schedule_cron`, sleep until then, call `run_compaction()`, log results. If `config.enabled == False`, sleep for an hour and check again.

**`_reaper_loop()`:**
Loop: call `reap_stale_tasks(db)` every 60 seconds. Catch and log exceptions — the reaper should never crash the daemon.

### 12. CLI Updates

**`glitch_core/cli/workers.py`:**
- `glitch worker start` — runs a standalone worker daemon (no web UI, no agent listener). For satellite nodes that only process tasks.
- `glitch worker status` — queries `/workers` collection, prints each worker's name, status, capabilities, last heartbeat, current task.

**`glitch_core/cli/compaction.py`:**
- `glitch compaction run` — manual compaction trigger. Default `--dry-run` flag for safety. Add `--force` to run without dry-run.
- `glitch compaction status` — show last N compaction runs from `/compaction_runs`.
- `glitch compaction rollback <run_id>` — call `rollback_compaction_run()`.

### 13. Web UI Updates

**`glitch_core/web/pages/workers.py` + `templates/workers.html`:**
Phase 1 has a basic workers page. Upgrade it to show:
- Online/offline status with colored indicator (green if heartbeat within 2 min, red otherwise)
- Capabilities as tags
- Currently executing task (if any)
- Supported agents list
- Glitch version (highlight if stale compared to main node)
- Last heartbeat timestamp

**`glitch_core/web/pages/review.py` + `templates/review.html` + `templates/components/review_card.html`:**
Phase 1 has a review page structure. Make it fully functional now that compaction can produce review items:
- List pending items from `/memory_review` where `reviewed == False`
- Each card shows: proposed memory content, category, confidence score, importance score
- Below the proposed memory: show the source journal entries (hydrate from `/journals` or `/journals_archive`) so the reviewer has context for WHY this memory was proposed
- Three action buttons:
  - **Approve** — write to `/core_memories` with `confidence=1.0` (human-approved), mark review item as reviewed
  - **Edit** — expand an edit form (HTMX), modify content and/or category, then promote with edits
  - **Reject** — mark as reviewed without promoting
- Badge count on the nav item showing pending review count

**`glitch_core/web/pages/system.py` + `templates/system.html`:**
Phase 1 has a basic system page. Upgrade to show:
- Compaction run history: last 10 runs with status, counts (created/updated/flagged/archived), duration, errors
- Manual compaction trigger button (defaults to dry-run, with a "Run for real" confirmation)
- Compaction rollback button per run (with confirmation modal)
- Current `CompactionConfig` values displayed (editable is a nice-to-have but not required)

**`glitch_core/web/pages/dashboard.py` + `templates/dashboard.html`:**
Update the dashboard to show real data now that workers and compaction exist:
- Worker count (online / total)
- Pending sub-tasks count
- Pending review items count (with link to review page)
- Last compaction run summary
- Active sessions count

### 14. Web UI — Chat Page Upgrade

Phase 1 has a basic chat interface. Upgrade it to show sub-agent activity:

- When the router spawns a sub-task, show a status indicator in the chat: "🔄 Delegated to coder agent..."
- When the sub-task completes, show the result inline in the chat
- When the sub-task fails, show the error
- For async tasks: the chat should poll (or use HTMX polling) for task status updates and render them when complete

---

## What NOT to Build in Phase 2

- `SafeFileWriter` and validated pipeline (Phase 3 — Ouroboros)
- Workspace free-form file operations (Phase 3)
- AI-powered theme generation (Phase 3)
- Tool hot-reloading via importlib (Phase 3)
- Page generation via coder agent (Phase 3)
- Runtime circuit breaker (Phase 3)
- SSH execution over Tailnet (the `execute_ssh` tool should exist as a stub)
- Migration runner (`migrations/runner.py`) — leave blank for now
- Flutter/mobile client integration

## Code Style

Same as Phase 1:
- Type hints everywhere. `str | None` not `Optional[str]`.
- Async by default. Every Firestore operation uses the async client.
- Pydantic v2 syntax.
- `from __future__ import annotations` in all files.
- Docstrings on all public classes and functions.
- Firestore document reads must handle the case where the document doesn't exist.
- Filter out `_placeholder` docs from all collection queries.

## Testing Approach

Don't write unit tests yet — the test files exist but leave them blank. Validation is integration-level:

1. Start the main daemon (`glitch start`). Verify it registers as a worker in Firestore.
2. Send a message that should trigger sub-agent dispatch (e.g., "write a Python function that reverses a string"). Verify: task doc created in Firestore, worker claims it, coder agent runs, result written to messages, chat UI shows the response.
3. Start a second worker on another terminal or machine (`glitch worker start`). Verify both workers appear in the web UI workers page with online status.
4. Stop the second worker. Verify: heartbeat stops, reaper marks it as stale after 2 minutes, any claimed tasks are released back to pending.
5. Run manual compaction (`glitch compaction run --force`). Verify: journals are processed, core memories created, audit log written, review items created for low-confidence memories.
6. Open the review page. Approve a memory. Verify it appears in core memories with confidence=1.0.
7. Roll back the compaction run (`glitch compaction rollback <run_id>`). Verify: memories reverted, journals un-archived.
8. If Ollama is available: configure a spicy agent, send an NSFW request, verify it routes exclusively to the local worker and waits if offline.

## Key Design Decisions to Preserve

- **The claim protocol uses Firestore transactions.** Don't use optimistic locking or last-write-wins. The transaction guarantees exactly one winner.
- **The reaper NEVER reassigns exclusive tasks.** Spicy tasks wait indefinitely. This is by design.
- **NSFW routing is enforced programmatically in the `spawn_sub_agent` tool**, not just in the system prompt. Even if the router's prompt is ignored, the code forces exclusive affinity to Spicy for NSFW content.
- **Compaction never deletes journals.** Archive only. The `journals_archive` collection is the permanent record.
- **Core memories always preserve `previous_content` on update.** One-step rollback without a full versioning system.
- **The compaction pipeline is idempotent.** Running it twice produces the same result. Crash at any point and the next run recovers safely.
- **API keys stay in `.env` on each machine.** Workers read their own keys. The agent registry only creates agents for which the node has the required keys. A node without an Anthropic key simply can't run the coder agent — it doesn't error, it just doesn't register support for it.
- **Workers report `glitch_version` in their heartbeat.** The `glitch update` command on the main node warns about stale workers. Workers on remote machines need to be updated separately.
