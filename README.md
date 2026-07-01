# Poiesis

A single-user, self-hosted personal AI that runs as a web app **and edits its own code
when you ask it to**. You chat with it in channels; ask it to change the app and it
rewrites its own source, commits, restarts, health-checks, and rolls back to the last
known-good commit if the change won't boot.

Built on the [Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/), SQLite, and
FastAPI + HTMX (no build step). See [rebuild.md](rebuild.md) for the full design.

## Quick start

```bash
uv sync
uv run poiesis bootstrap --password 'your-admin-password'
uv run poiesis start --app-only      # web app only (no self-mod restarts)
# or: uv run poiesis start            # + supervisor: enables self-mod deploy/rollback
```

Open http://127.0.0.1:8080 and sign in as `admin`. Model auth comes from the `claude`
CLI — `claude login` (subscription) or a raw `ANTHROPIC_API_KEY`.

## Layout

| Path | What |
|------|------|
| `poiesis/agent/` | Agent turn runner over the Claude Agent SDK (streaming, in-process tools) |
| `poiesis/web/` | FastAPI app, auth, SSE chat, channel-centric UI |
| `poiesis/supervisor.py` | Blue-ish/green deploy + `git reset --hard last-green` rollback |
| `poiesis/scheduler.py` | Durable cron/interval tasks (e.g. the PM morning nudge) |
| `poiesis/db.py`, `store.py`, `migrations/` | SQLite persistence |
| `souls/*.md` | Per-channel personas (file-backed; edit → effect next turn) |
| `deploy/` | Cloudflare Tunnel + systemd + runbook for `agent.mattharris.tech` |

## Channels

`general` (chat + read-only code access — self-mod is deferred; see `ROADMAP.md`),
`project-management` (task list + 10am nudge), and `feature` / `bug` / `analytics`
(chat now; specialized pipelines later — see `rebuild.md`).

## Develop

```bash
uv sync --extra dev       # installs pytest + pytest-asyncio + ruff
uv run --extra dev pytest
```

Bump every dependency to its latest compatible release (strip floors → let `uv`
re-resolve the whole graph → repin), then **test before committing**:

```bash
uv run python scripts/upgrade_deps.py   # --dry-run to preview the uv commands
```
