"""ASGI entrypoint. Run with: uvicorn glitch_core.asgi:app

Builds the app from ~/.glitch/.env (GlitchEnv). The lifespan connects + migrates
the SQLite DB on the serving loop.
"""

from __future__ import annotations

from glitch_core.config import GlitchEnv
from glitch_core.db import Database
from glitch_core.web.app import create_app


def make_app():
    # Auth is whatever the `claude` CLI already uses: a claude.ai login
    # (subscription — cheaper) or, if you'd rather pay per token, a raw
    # ANTHROPIC_API_KEY exported in the environment. We don't force either.
    env = GlitchEnv()
    db = Database(env.db_path)
    return create_app(db, env)


app = make_app()
