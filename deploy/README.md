# Deploying Glitch to `agent.mattharris.tech`

Single-user, self-hosted. The app runs behind Caddy (auto-TLS), gated by a
username/password. Goal: open `https://agent.mattharris.tech`, log in, chat with
#general and #project-management, and have the 10am nudge fire on its own.

## 0. Prereqs on the server
- `git`, `uv`, Node.js (for the Claude Agent SDK), and the `claude` CLI on PATH
- `caddy` installed and running
- Ports **80** and **443** open to the internet
- DNS: an **A/AAAA record** for `agent.mattharris.tech` → this server's IP

## 1. Get the code
```bash
cd ~ && git clone <your-remote> glitch_core && cd glitch_core
uv sync
```

## 2. Configure + bootstrap
```bash
uv run glitch bootstrap --password 'YOUR-ADMIN-PASSWORD'
```
This creates `~/.glitch/.env` (session secret, admin user + bcrypt hash), the SQLite
DB, the default theme, and the #general + #project-management channels with the 10am
schedule. Then add to `~/.glitch/.env`:
```
GLITCH_TZ=America/New_York               # so the PM nudge fires at YOUR 10am
GLITCH_HOST=127.0.0.1                     # keep private; Caddy faces the internet
GLITCH_PORT=8080
```
Re-running `glitch bootstrap` is always safe (idempotent).

**Model auth** is handled by the `claude` CLI, two ways:
- **claude.ai login (recommended, cheaper):** run `claude login` once as this user — uses
  your subscription. Do **not** set `ANTHROPIC_API_KEY`, or it takes precedence and bills
  per token.
- **API key:** `export ANTHROPIC_API_KEY=sk-ant-...` (metered billing).

## 3. Reverse proxy (TLS)
```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```
Caddy fetches a certificate automatically once DNS resolves to this host.

## 4. Run as a service
Edit `deploy/glitch.service` (the `User`, `WorkingDirectory`, `HOME`, and the node
path in `PATH`), then:
```bash
sudo cp deploy/glitch.service /etc/systemd/system/glitch.service
sudo systemctl daemon-reload && sudo systemctl enable --now glitch
journalctl -u glitch -f
```

## 5. Verify (the "operational" checklist)
- `curl -sf http://127.0.0.1:8080/healthz` → `{"status":"ok"}`
- Visit `https://agent.mattharris.tech` → redirected to `/login` → sign in
- #general: chat replies stream; ask it to remember something, confirm recall
- #project-management: tell it a task → it edits `~/.glitch/pm/task.md`
- The 10am nudge appears in #project-management on its own (or set a near-term
  `interval` schedule to test sooner)

## Notes
- **Self-mod is live in production.** Asking #general to change the app edits this repo,
  commits, restarts, and rolls back to last-green if it won't boot. Keep the working
  tree committed.
- Updating the code: `git pull && uv sync && sudo systemctl restart glitch`.
