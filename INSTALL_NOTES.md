# Install Notes

Pain points encountered during real deployments. Fuel for the bootstrap agent.

## Deployment 1 — GF Instance (2026-04-03)

Server: TBD
OS: TBD

### Pain Points

1. **Firebase CLI login on headless/SSH server.** `firebase login` tries to redirect to `localhost` which doesn't exist on a remote box. Fix: `firebase login --no-localhost` — gives a URL to open on your local machine, returns a code to paste back. The bootstrap agent should detect headless (no DISPLAY, SSH_TTY set) and use this automatically.

2. **Firebase deploy fails silently after bootstrap.** Bootstrap seeds Firestore fine (uses service account directly), but `firebase deploy --only firestore` fails because no `.firebaserc` exists (gitignored). The CLI doesn't know which project to target. Fix: run `firebase use <project-id>` first, or pass `--project <id>` explicitly. Bootstrap should do `firebase use` automatically before deploying rules.

