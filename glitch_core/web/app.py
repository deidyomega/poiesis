from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from glitch_core.web.engine import PageEngine
from glitch_core.web.middleware import ThemeMiddleware
from glitch_core.web.theming import PRESET_THEMES

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
TEMPLATES_CUSTOM_DIR = WEB_DIR / "templates_custom"
PAGES_DIR = WEB_DIR / "pages"
PAGES_CUSTOM_DIR = WEB_DIR / "pages_custom"


def create_app(db: Any = None) -> FastAPI:
    """Assemble the FastAPI application with all routes, middleware, and templates."""
    app = FastAPI(title="Poiesis", version="0.1.0")

    # Jinja2 templates — multi-directory search (custom templates override core)
    template_dirs = [str(TEMPLATES_DIR)]
    if TEMPLATES_CUSTOM_DIR.exists():
        template_dirs.insert(0, str(TEMPLATES_CUSTOM_DIR))

    templates = Jinja2Templates(directory=template_dirs)

    # Disable Jinja2 template cache to avoid unhashable globals issue
    # Setting cache_size=0 prevents Jinja2 from trying to hash globals
    templates.env = templates.env.overlay(cache_size=0)

    # Set default theme in Jinja2 globals — middleware updates these per-request
    default_theme = PRESET_THEMES["default"]
    templates.env.globals["theme"] = default_theme
    templates.env.globals["nav"] = {}

    # Page engine — discover and register all page modules
    page_engine = PageEngine(PAGES_DIR, PAGES_CUSTOM_DIR)
    page_engine.discover_pages()

    # Mount all discovered page routers
    for page_router in page_engine.get_routers():
        app.include_router(page_router)

    # Theme middleware — loads theme from Firestore and injects into request.state
    if db is not None:
        app.add_middleware(
            ThemeMiddleware,
            db=db,
            page_engine=page_engine,
            templates=templates,
        )

    # Handle deleted custom pages gracefully (route still mounted, template gone)
    @app.exception_handler(jinja2.exceptions.TemplateNotFound)
    async def template_not_found_handler(request: Request, exc: jinja2.exceptions.TemplateNotFound) -> HTMLResponse:
        return HTMLResponse(
            content=f"<h1>404 — Page Not Found</h1><p>Template <code>{exc.name}</code> no longer exists.</p>",
            status_code=404,
        )

    # Store references on app state for access in routes
    app.state.db = db
    app.state.templates = templates
    app.state.page_engine = page_engine

    return app
