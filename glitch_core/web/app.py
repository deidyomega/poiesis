from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from glitch_core.config import GlitchEnv
from glitch_core.db import Database
from glitch_core.migrations.runner import run_migrations
from glitch_core.web import auth
from glitch_core.web.engine import PageEngine
from glitch_core.web.middleware import ThemeMiddleware
from glitch_core.web.theming import PRESET_THEMES

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
TEMPLATES_CUSTOM_DIR = WEB_DIR / "templates_custom"
PAGES_DIR = WEB_DIR / "pages"
PAGES_CUSTOM_DIR = WEB_DIR / "pages_custom"


def create_app(db: Database, env: GlitchEnv) -> FastAPI:
    """Assemble the FastAPI app: templates, pages, theme, auth, health.

    The Database is connected (and migrations applied) inside the lifespan so the
    aiosqlite connection is bound to the serving event loop.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.connect()
        await run_migrations(db)
        yield
        await db.close()

    app = FastAPI(title="Glitch", version="0.2.0", lifespan=lifespan)

    template_dirs = [str(TEMPLATES_DIR)]
    if TEMPLATES_CUSTOM_DIR.exists():
        template_dirs.insert(0, str(TEMPLATES_CUSTOM_DIR))
    templates = Jinja2Templates(directory=template_dirs)
    templates.env = templates.env.overlay(cache_size=0)
    templates.env.globals["theme"] = PRESET_THEMES["default"]
    templates.env.globals["nav"] = {}

    page_engine = PageEngine(PAGES_DIR, PAGES_CUSTOM_DIR)
    page_engine.discover_pages()

    app.include_router(auth.router)
    for page_router in page_engine.get_routers():
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
    app.add_middleware(ThemeMiddleware, db=db, page_engine=page_engine, templates=templates)
    app.add_middleware(auth.AuthMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=env.effective_session_secret())

    app.state.db = db
    app.state.env = env
    app.state.templates = templates
    app.state.page_engine = page_engine
    return app
