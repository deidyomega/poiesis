"""ASGI entrypoint. Run with: uvicorn glitch_core.asgi:app

Builds the app from ~/.glitch/.env (GlitchEnv). The lifespan connects + migrates
the SQLite DB on the serving loop.
"""

from __future__ import annotations

import os

from glitch_core.config import GlitchEnv
from glitch_core.db import Database
from glitch_core.web.app import create_app


def make_app():
    env = GlitchEnv()
    # Bridge our prefixed key to what the Claude Agent SDK / claude CLI expects,
    # so a headless server authenticates without a separate `claude login`.
    if env.anthropic_api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", env.anthropic_api_key)
    db = Database(env.db_path)
    return create_app(db, env)


app = make_app()
