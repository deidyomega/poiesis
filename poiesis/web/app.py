from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from poiesis import store
from poiesis.config import PoiesisEnv
from poiesis.db import Database
from poiesis.migrations.runner import run_migrations
from poiesis.web import auth, pwa
from poiesis.web.middleware import ThemeMiddleware
from poiesis.web.pages import chat, settings
from poiesis.web.theming import PRESET_THEMES
from poiesis.web.turns import TurnManager

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"

# Page routers, in nav order. Explicit — no discovery/generator engine (the v1
# "AI writes whole pages" machinery is gone; the nav is channel-driven, see nav.html).
PAGE_ROUTERS = [chat.router, settings.router]


def create_app(db: Database, env: PoiesisEnv) -> FastAPI:
    """Assemble the FastAPI app: templates, pages, theme, auth, health.

    The Database is connected (and migrations applied) inside the lifespan so the
    aiosqlite connection is bound to the serving event loop.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from poiesis.scheduler import run_scheduler

        await db.connect()
        await run_migrations(db)
        # Detached turns don't survive a process restart — mark any left mid-flight as errored.
        n_reset = await store.reset_generating_messages(db)
        if n_reset:
            logging.getLogger("poiesis.web.app").info(
                "Reset %d interrupted turn(s) from a prior run", n_reset)
        # Pre-load #spice's challenges into a cached markdown blob so its (slow, thinking)
        # model gets them in-context without a runtime tool round-trip. Refreshes each boot.
        from poiesis.agent.spice_tools import refresh_challenges
        try:
            n = await refresh_challenges(db, env)
            logging.getLogger("poiesis.web.app").info("Loaded %d challenge line(s) for #spice", n)
        except Exception:  # noqa: BLE001 — never block boot on the challenges fetch
            logging.getLogger("poiesis.web.app").exception("challenges refresh failed")
        scheduler_task = asyncio.create_task(run_scheduler(db, env))
        try:
            yield
        finally:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
            await db.close()

    app = FastAPI(title="Poiesis", version="0.2.0", lifespan=lifespan)

    templates = Jinja2Templates(directory=[str(TEMPLATES_DIR)])
    templates.env = templates.env.overlay(cache_size=0)
    templates.env.globals["theme"] = PRESET_THEMES["default"]

    app.include_router(auth.router)
    app.include_router(pwa.router)
    for page_router in PAGE_ROUTERS:
        app.include_router(page_router)

    @app.get("/healthz", response_class=JSONResponse)
    async def healthz() -> JSONResponse:
        # The app booting far enough to serve this already proves imports work;
        # a cheap DB round-trip proves persistence is wired. The supervisor polls this.
        await db.fetch_one("SELECT 1 AS ok")
        return JSONResponse({"status": "ok"})

    @app.exception_handler(jinja2.exceptions.TemplateNotFound)
    async def template_not_found_handler(request: Request, exc) -> HTMLResponse:
        return HTMLResponse(
            content=f"<h1>404 — Not Found</h1><p>Template <code>{exc.name}</code> is missing.</p>",
            status_code=404,
        )

    # Middleware stack (last added = outermost). Want: Session -> Auth -> Theme -> app.
    app.add_middleware(ThemeMiddleware, db=db, templates=templates)
    app.add_middleware(auth.AuthMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=env.effective_session_secret())

    app.state.db = db
    app.state.env = env
    app.state.templates = templates
    app.state.turns = TurnManager(db, env)  # owns detached, client-independent turns
    return app
