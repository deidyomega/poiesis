# Deploying Glitch to `agent.mattharris.tech`

Single-user, self-hosted. The app runs behind a **Cloudflare Tunnel** (CF terminates TLS
at its edge), gated by a username/password. Goal: open `https://agent.mattharris.tech`,
log in, chat with #general and #project-management, and have the 10am nudge fire on its own.

## 0. Prereqs on the server
- `git`, `uv`, Node.js (for the Claude Agent SDK), and the `claude` CLI on PATH
- `cloudflared` installed
- **No inbound ports needed** — cloudflared dials *out* to Cloudflare, so an ISP that
  blocks port 80, CGNAT, or no port-forwarding is all a non-issue
- `mattharris.tech` managed by Cloudflare DNS (the tunnel creates the hostname record)

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
GLITCH_HOST=127.0.0.1                     # keep private; the tunnel reaches it
GLITCH_PORT=8080
```
Re-running `glitch bootstrap` is always safe (idempotent).

**Model auth** is handled by the `claude` CLI, two ways:
- **claude.ai login (recommended, cheaper):** run `claude login` once as this user — uses
  your subscription. Do **not** set `ANTHROPIC_API_KEY`, or it takes precedence and bills
  per token.
- **API key:** `export ANTHROPIC_API_KEY=sk-ant-...` (metered billing).

## 3. Cloudflare Tunnel (TLS + ingress, no open ports)
```bash
cloudflared tunnel login                     # authorize the mattharris.tech zone
cloudflared tunnel create glitch             # prints a tunnel ID + credentials json
cloudflared tunnel route dns glitch agent.mattharris.tech
# set the tunnel name + credentials-file path in deploy/cloudflared-config.yml, then:
sudo cp deploy/cloudflared-config.yml /etc/cloudflared/config.yml
sudo cloudflared service install             # runs the tunnel on boot
```
CF terminates TLS at its edge and routes `agent.mattharris.tech` → `http://localhost:8080`.
SSE works through the tunnel — the app sends keepalives so long agent turns don't hit CF's
~100s idle cutoff.

**Optional second gate:** put **Cloudflare Access** in front of the hostname (email OTP /
SSO) so traffic is authenticated before it ever reaches the app — sensible given #general
can run code. The app's own login still applies underneath.

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
