# Poiesis — roadmap & working notes

Living handoff doc. [`rebuild.md`](rebuild.md) is the architecture/design spec; this is
"where we are, what's next, how to run it, and the gotchas worth not re-learning." Read this
first when picking the project back up.

## Where we are (2026-07-01)

Working single-user app, clean v2 base, all on `main`: five channels (general,
project-management, feature, bug, analytics) in a channel-centric nav; **Atlas** is the PM
persona; self-mod proven (it added a `/ping` route to itself); durable scheduler + the 10am
PM nudge; SSE chat with keepalives; Cloudflare-Tunnel deploy artifacts. 15 tests pass.

Latest cleanup pass: purged the last v1 cruft — the page-discovery/custom-page "generator"
engine (`web/engine.py`, `pages_custom/`, `templates_custom/`) and ~22 orphan v1 templates;
routers are now imported explicitly. **#general is locked to chat + read-only code access +
memory** (no Write/Edit/Bash/request_deploy) so it can't break the live app while self-mod is
off. `pyproject`/README metadata de-v1'd. (Runtime cruft still on the box: `~/.poiesis/.env`,
`config.json`, `credentials.json` are all v1 — see below.)

## How we work (for now)

- **Commit directly to `main`, no feature branches, until the app is feature-complete.**
- **Self-mod is deferred.** Build features the normal way — human + Claude Code editing the
  repo, commit, restart. Turn self-mod on once the app is worth protecting.
- **Dev runtime:** `screen` (or tmux) + `poiesis start --app-only` (no supervisor). Edit on
  the box (or `git pull`), refresh the browser. Templates/souls take effect on refresh;
  Python needs a restart (until the `--reload` dev runner lands).
- **Commit before anything self-mod** — a rollback (`git reset --hard last-green`) eats
  uncommitted changes.

## Next up (in order)

1. **Soul editor in Settings** — read/write `souls/<path>` from the browser (git-commit the
   edit so it's rollback-safe). Souls are read each turn → live on save, no restart.
2. **`--reload` dev runner** — `poiesis start --app-only --reload` so Python edits are also
   just-refresh.
3. **Re-enable #general self-mod** once the supervisor's rollback net is on for real — grant
   back Write/Edit/Bash/request_deploy (see the locked-down seed in `bootstrap.py`).

## Later (deliberately deferred)

- **#bug pipeline** (the keystone; "took weeks" in OpenClaw — don't rush): Sentry webhook →
  staged bots with typed handoffs + git-worktree-per-job → PR + triple-R. Reuses the
  coding-agent + worktree machinery.
- **True blue/green + web/engine process split** — needed once long-running jobs exist, so a
  self-redeploy can't interrupt them.
- **#feature / #analytics real capabilities** — feature: coding against the work repo (Django
  monorepo); analytics: MCP connectors (GA, Recurly, Datadog).
- **Mobile** — Flutter + SSE + local notifications (backend already emits SSE + a
  `notification` flag; will want token auth instead of the session cookie).

## Gotchas worth remembering

- **Model auth:** the `claude` CLI provides it — `claude login` (subscription, cheaper) or a
  raw `ANTHROPIC_API_KEY`. Don't set the key if you want the subscription. Poiesis doesn't
  manage auth (the old `POIESIS_ANTHROPIC_API_KEY` bridge was removed).
- **SDK file sandbox:** the Agent SDK's built-in file tools only see *real* mounted dirs (a
  `/var/folders` temp dir showed up empty as `/home/user`). Self-mod works (real repo). For
  data the agent must touch reliably, use in-process MCP tools (like `read_tasks`), not the
  sandboxed file tools. Overriding `HOME` breaks the CLI's auth.
- **SSE behind Cloudflare:** CF drops idle connections (~100s); the stream sends keepalives
  every 15s so long turns survive. Keep the `X-Accel-Buffering: no` header on the stream.
- **Storage split:** tasks = markdown file (`~/.poiesis/pm/task.md`, outside the repo → safe
  from rollback); memories = SQLite rows; souls = git-tracked files in `souls/`.
- **Timezone:** set `POIESIS_TZ` (Matt = `America/Phoenix`, MST, no DST) so the agent's clock
  and the nudge use local time.

## Deploy

Cloudflare Tunnel (not Caddy — ISP blocks port 80). Full runbook: [`deploy/README.md`](deploy/README.md).
