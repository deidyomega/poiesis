# Glitch v2 — Rebuild Spec (DRAFT)

> Draft for review — supersedes `architecture.md`. Shred freely.
> Goal: a small, **finishable**, self-hosted personal agent Matt uses daily — that
> grows by editing its own code when asked.

## The wound this fixes

v1 (this repo) was 80% built and never usable — a "can do anything" system optimized
for generality and GitHub appeal, not for one person's daily use. v2 inverts it: do
exactly what's needed (replicate the OpenClaw + Discord daily driver Matt already runs),
as a finished web surface, and grow by asking it to add **concrete** features to itself.

## Three decisions that define the shape

1. **Surface** = a responsive web app at `agent.mattharris.tech`, single username/password.
   No Discord, no Tailscale, no Firebase.
2. **Self-modification reaches the real app, including core.** The app is its own git repo;
   a coding agent edits it, commits, restarts; a supervisor reverts to last-green if it
   won't boot. No human review gate (Matt's call).
3. **State = SQLite + markdown files.** Two local processes (`web` + `engine`) under one
   supervisor. No distributed anything.

## The one principle: structure over suggestion

v1's bug agent followed a soft "prayer" and drifted — forgot the Sentry correlation check,
skipped the PR→Sentry link. v2 enforces sequence and completeness **structurally**:

- Workflows are orchestrated stages run in fixed order — skipping isn't a move the agent has.
- Handoffs between stages are **typed contracts** (Pydantic). The PR stage receives
  `{sentry_error, diff, summary}` — so a PR missing the Sentry link is *impossible*, not
  "hopefully remembered."

The prompt stops guaranteeing correctness; the data flow does. (Same lesson as v1's
SafeFileWriter — "the only write path that exists" — applied to sequencing.)

## Architecture

### Process & safety
Two long-lived processes under one supervisor — the *only* justified split (this is the
good "web vs. daemon" separation the old `todo.md` already wanted, **not** the distributed Hive):

- **`web`** = FastAPI/uvicorn: UI, SSE, auth, webhook ingress, enqueues jobs, renders job
  state. The *disposable face* — blue/green redeployed on most self-mods (UI/feature tweaks).
- **`engine`** = the durable spine: heartbeat, timers, workflow/job execution, spawns agent
  work in worktrees. **Owns the jobs.** Redeployed (with **drain**) only when workflow/engine
  code itself changes.
- **`supervisor`** = the only non-self-modifiable thing. Boots both, health-checks over HTTP,
  tags a commit "green" once healthy, blue/greens each unit independently, and
  `git reset --hard <last-green>` + restart if a self-edit won't come up. With no review
  gate, this is the *entire* safety net — bulletproof and dead simple.

Shared state: one SQLite DB (WAL mode) both processes use; single-user contention is trivial.

**Hard rule — a self-redeploy must never kill an in-flight job.** Most self-mods touch only
`web`, so the `engine` (and any running bug pipeline) is left untouched by construction.
Backstop for the rarer `engine` redeploy: every job's state is durable (persisted per-stage)
and every stage is **idempotent + resumable**, so even a hard restart resumes from the last
checkpoint instead of losing work. (The bug pipeline's dedup-by-PR check is exactly this kind
of idempotency.)

### State
- **SQLite**: jobs, workflow steps, bugs, channels, messages, memories, journal, schedule.
- **Files (markdown)**: per-channel `soul.md` / `identity.md`, `task.md`, memory notes —
  hand- and agent-editable (the OpenClaw pattern).
- **Git**: the app's own repo (self-mod) + target work repos managed as worktrees.

### The workflow runtime (the heart)
- **Workflows = concrete Python** (e.g. `bug_workflow`), each a sequence of named stages.
  Not a config DSL — hardcoded per the "specific over generic" rule. The plumbing below is
  the only generic part.
- **`jobs` table**: one row per workflow instance — current stage, typed payload, status,
  timestamps. Durable & resumable across restarts/crashes.
- **Step runner**: advances jobs, persists state after each stage.
- **Timers**: a stage can sleep (5s, 5min); the heartbeat wakes it.
- **External events**: inbound webhooks (Sentry, GitHub "PR closed") resume the matching
  job by correlation key (bug id / PR number).
- **Worktree-per-job**: created at handoff, destroyed on PR-close — enables parallel bug
  fixes and the loop-back.
- **Loop-backs**: a stage can route to an earlier one (PR bot → bug handler).

### The agent core (one core, every channel)
A coding-agent loop instantiated per stage/turn, scoped to a worktree, with a soul + toolset:
files (read/write/edit), shell (run tests), git, GitHub (`gh`), MCP clients (Sentry now;
GA/Recurly/Datadog later). The same core powers bug stages, #feature (coding a work repo),
#general (chat), and self-mod (coding its own repo). **Self-mod is just the daily coding
flow aimed at the app's own repo** — which de-risks it.

### Surfaces
- **Auth**: single user/pass + session cookie.
- **Channels**: persistent contexts = soul + tools + optional bound repo + optional schedule
  (general, feature, bug, analytics, pm). Replaces v1's session/agent split.
- **Per-channel chat** with real **SSE streaming** (kills the 600ms Firestore-doc hack).
- **Jobs dashboard**: bugs moving through the pipeline, their stage, PR link, and an
  **escalation inbox** where triple-R "raise" items land for Matt.
- Responsive Tailwind, mobile-first, **no build step** (so the agent can rewrite UI and
  reload instantly).

### Scheduler
One heartbeat/cron loop drives both workflow timers and channel schedules. PM's 10am nudge =
a cron entry that runs the PM agent against `task.md` and posts a message (+ notify).

## The #bug workflow (v2, concrete)

```
Sentry webhook ─▶ [intake] wait 5s
   Stage 1  Intake/Sentry worker: MCP-pull all same-second errors + logs →
            decide if joined → persist consolidated report →
            create worktree off main → handoff {report, logs, mcp_ctx}
   Stage 2  Bug handler (in worktree): root cause → failing test → fix →
            stash-validate loop → commit/push → handoff {branch, summary}
            (re-entered on loop-back)
   Stage 3  PR bot: create/format PR from {sentry_error, diff, summary} →
            wait 5min → triple-R (reply / resolve / raise-to-Matt) →
            route required fixes back to Stage 2.  On PR-closed → delete worktree.
```
External (not ours): `pr-{n}.rothblueprint.com` preview + Neon PII-scrubbed DB branch.

## Basic memory (no PhD)
`remember` / `recall` tool + agent-written journal notes (SQLite rows) + core memories
loaded into context. Optional single nightly distill call to fold journal → memories and
de-dupe. No decay, no scoring, no pipeline. ~150 lines, not ~1,200.

## Salvage map (from ~8,500 LOC)
- **Keep/adapt**: `schemas.py` (esp. for handoff models), HTMX/Tailwind base + chat
  templates, the soul concept, git snapshot/commit/revert helpers from `sandbox.py`
  (→ supervisor last-green logic), basic memory (journals + core_memories, gutted).
- **Scrap**: Firestore + dual clients + streaming-doc hack; the Hive (claim/affinity/
  reaper/registration/distributed workers); 6-provider sprawl; AST blocklist; tool/page
  one-file generators; compaction decay/daily-log/merge; memory_review; the trust-zone split.
- **New**: workflow runtime + jobs; supervisor; SSE; auth; MCP client; Sentry + GitHub
  integrations; worktree manager; scheduler.

## Build order (usable early, not 80%-forever)
- **Phase 0 — Usable shell (small, fast):** FastAPI + auth + SQLite + one chat channel +
  SSE + agent core (files/shell/git) + supervisor + self-mod on its own repo. → A
  daily-usable chat agent that can edit its own code, live behind the domain. Replaces
  #general; proves self-mod end to end.
- **Phase 1 — Workflow runtime + #bug (keystone):** jobs/steps/timers/webhooks/worktrees/
  handoffs; Sentry MCP; GitHub PR; the 3-stage pipeline; jobs dashboard + escalation inbox.
- **Phase 2 — The cheap channels fall out:** #feature (bug handler minus Sentry, manual
  kickoff), #analytics (MCP + chat), #pm (chat + 10am cron + `task.md`).
- Memory woven in throughout.

## Open decisions (need Matt)
1. **Where it runs** (VPS vs. a box at home) + **supervisor mechanism** (systemd + watchdog /
   Docker / small parent process).
2. **Agent core**: Claude Agent SDK as a library vs. reuse OpenClaw's guts vs. custom loop.
   *Lean: Agent SDK as a dependency — keeps the codebase small and fully owned (so it stays
   self-rewritable) while getting the loop + tools + MCP for free.*
3. **Models**: Anthropic-only?
4. **The work repo(s)** the bug pipeline drives — language / test runner / how many.
