# Poiesis — roadmap & working notes

Living handoff doc. [`rebuild.md`](rebuild.md) is the architecture/design spec; this is
"where we are, what's next, how to run it, and the gotchas worth not re-learning." Read this
first when picking the project back up.

## Where we are (2026-07-02)

Working single-user app, all on `main`, ~29 tests passing. Live on the public internet at
**`agents.mattharris.tech`** (Cloudflare Tunnel from a separate LAN box → this app box at
`192.168.1.6:8000`).

Five channels in a channel-centric nav:
- **#general** — the "do whatever" research surface: full Claude Code toolset (web/search/
  subagents/shell), sandboxed to a scratch workspace (`~/.poiesis/general`), not the repo.
- **#pm** (Atlas) — task list + the 10am nudge.
- **#feature / #bug / #analytics** — thinking spaces; real pipelines deferred.
- **#spice** (Prompta) — an **OpenAI-compatible** channel (not the Claude SDK) for generating
  adult challenge ideas; runs on OpenRouter (`sao10k/l3.3-euryale-70b`) or a LAN Ollama, with
  an in-UI model picker.

Big things shipped since the v2 base:
- **Two engines behind one `run_turn`** — Claude Agent SDK + OpenAI-compatible (Ollama/
  OpenRouter). Channel `engine` column dispatches; `chat.py`/scheduler untouched.
- **Generation is decoupled from the browser** (see [detached turns](#detached-turns-how-chat-works-now) below).
- **Per-channel memory** (was global and leaked across channels).
- **Model picker** in the #spice header (cross-provider, per-channel).
- Soul editor in Settings; raw per-turn transcript viewer; grouped think/tool boxes with
  live tool args/results; durable file logging (`~/.poiesis/poiesis.log`).

## How we work (for now)

- **Commit directly to `main`, no feature branches.**
- **Self-mod is deferred.** Build features the normal way — edit repo, commit, restart.
- **Dev runtime:** `screen` + `poiesis start --app-only` on the box. Templates/souls take
  effect on browser refresh; Python needs a restart; migrations run on boot.
- Machines are FRUs — dev lives on the box, not the laptop.

## Next up (in order — MOBILE is the current priority)

1. **Clean mobile UI (Android PWA).** The bar is *Discord-quality mobile UX*, not a shrunk
   desktop site. Two parts: (a) a genuinely good mobile layout — channel drawer, polished
   message list, keyboard-aware composer, safe areas, touch feedback; (b) the PWA shell —
   manifest + service worker + install. Android-only + single-user keeps it tight (no iOS
   cruft). Detached turns already make backgrounding safe.
2. **Web Push** — one VAPID keypair, one stored subscription, `pywebpush` in the scheduler,
   so the 10am nudge reaches a locked phone. (Depends on the PWA shell.)
3. **CF Access + token SSO** on `agents.mattharris.tech` — trust the `Cf-Access-Jwt-Assertion`
   header → auto-mint the session → kill the login screen on every device.

## Later (deliberately deferred)

- **#bug pipeline** (the keystone; greenfield — the OpenClaw version was mediocre, take the
  *concepts* not the code): Sentry webhook → staged bots with typed handoffs + git-worktree-
  per-job → PR + triple-R, against the AnnuityOS Django monorepo.
- **web/engine process split + true blue/green** — the other half of durability: detached
  turns survive client disconnects but not a server restart; long #bug jobs need to survive
  self-redeploys. Same fix unlocks both.
- **#feature / #analytics** real capabilities — feature: coding vs the work repo; analytics:
  MCP connectors (GA, Recurly, Datadog).
- **`--reload` dev runner** so Python edits are also just-refresh.

## Detached turns (how chat works now)

Generation is an app-owned task, not the browser's request. `poiesis/web/turns.py::TurnManager`
runs each turn to completion and persists to SQLite regardless of who's connected; `/chat/stream`
is a *follower* (attach → `catchup` resync → detach on disconnect → reattach on reload). The DB
is the source of truth (`messages.status` = generating/done/cancelled/error). Survives every
*client* disconnect (refresh, laptop-close, flaky net, mobile background); a *server* restart
mid-turn is marked errored on boot (`reset_generating_messages`) — full cross-restart durability
is the web/engine split above.

## Gotchas worth remembering

- **SDK tool capability:** set `ClaudeAgentOptions.tools` explicitly — left None, the SDK loads
  the *entire* Claude Code preset, and `bypassPermissions` means `allowed_tools` doesn't gate
  it. The `"*"` sentinel in a channel's allowed_tools opts into the full preset (#general).
- **#spice / OpenAI engine:** config in `~/.poiesis/.env` (`POIESIS_SPICE_*`). OpenRouter niche
  models are often **single-provider** → they 429 with no fallback; prefer multi-provider (check
  `/api/v1/models/<id>/endpoints`); `max_retries=4` rides transient throttles. Abliteration
  removes refusals, not the safe-style prior — **command** the explicit register in the soul.
- **Model auth (Claude):** the `claude` CLI provides it (`claude login` or `ANTHROPIC_API_KEY`).
  Overriding `HOME` breaks the CLI's auth.
- **SSE behind Cloudflare:** CF drops idle connections (~100s); the stream sends keepalives every
  15s. Keep `Cache-Control: no-cache, no-transform` + `X-Accel-Buffering: no` on the stream.
- **Test isolation:** bootstrap/config write to the real `~/.poiesis` via `Path.home()`; patch
  `Path.home` in tests, never `rm -rf ~/.poiesis`.
- **Storage split:** tasks = markdown (`~/.poiesis/pm/task.md`, outside repo → rollback-safe);
  memories = SQLite (per-channel); challenges = fetched + cached at boot; souls = git-tracked.
- **Timezone:** `POIESIS_TZ=America/Phoenix` (MST, no DST).

## Deploy

Public at `agents.mattharris.tech` via Cloudflare Tunnel (cloudflared on a separate LAN box →
`http://192.168.1.6:8000`). App binds `0.0.0.0`. Full runbook: [`deploy/README.md`](deploy/README.md).
