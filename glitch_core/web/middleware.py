from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from glitch_core import store
from glitch_core.db import Database
from glitch_core.web.theming import PRESET_THEMES, GlitchTheme

logger = logging.getLogger(__name__)


class ThemeMiddleware(BaseHTTPMiddleware):
    """Load the current theme from SQLite (cached) and inject into Jinja2 globals."""

    def __init__(self, app, db: Database, page_engine, templates) -> None:
        super().__init__(app)
        self.db = db
        self.page_engine = page_engine
        self.templates = templates
        self._theme_cache: GlitchTheme | None = None
        self._cache_time: float = 0.0
        self._cache_ttl: float = 600.0

    async def _get_theme(self, force_refresh: bool = False) -> GlitchTheme:
        now = time.time()
        if (
            not force_refresh
            and self._theme_cache is not None
            and (now - self._cache_time) < self._cache_ttl
        ):
            return self._theme_cache
        try:
            data = await store.get_setting(self.db, "theme")
            self._theme_cache = GlitchTheme.model_validate(data) if data else PRESET_THEMES["default"]
        except Exception:
            logger.exception("Failed to load theme, using default")
            self._theme_cache = PRESET_THEMES["default"]
        self._cache_time = now
        return self._theme_cache

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        force_refresh = getattr(request.app.state, "_theme_bust", False)
        if force_refresh:
            request.app.state._theme_bust = False
        theme = await self._get_theme(force_refresh=force_refresh)
        nav = self.page_engine.get_nav_items() if self.page_engine else {}
        self.templates.env.globals["theme"] = theme
        self.templates.env.globals["nav"] = nav
        return await call_next(request)
