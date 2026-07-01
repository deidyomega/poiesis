"""ASGI entrypoint. Run with: uvicorn poiesis.asgi:app

Builds the app from ~/.poiesis/.env (PoiesisEnv). The lifespan connects + migrates
the SQLite DB on the serving loop.
"""

from __future__ import annotations

from poiesis.config import PoiesisEnv
from poiesis.db import Database
from poiesis.web.app import create_app


def make_app():
    # Auth is whatever the `claude` CLI already uses: a claude.ai login
    # (subscription — cheaper) or, if you'd rather pay per token, a raw
    # ANTHROPIC_API_KEY exported in the environment. We don't force either.
    env = PoiesisEnv()
    db = Database(env.db_path)
    return create_app(db, env)


app = make_app()
