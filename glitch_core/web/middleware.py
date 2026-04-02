from __future__ import annotations

import logging
import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from glitch_core.web.theming import PRESET_THEMES, GlitchTheme

logger = logging.getLogger(__name__)


class ThemeMiddleware(BaseHTTPMiddleware):
    """Loads current theme from Firestore (cached 60s) and injects into Jinja2 globals."""

    def __init__(self, app: Any, db: Any, page_engine: Any, templates: Any) -> None:
        super().__init__(app)
        self.db = db
        self.page_engine = page_engine
        self.templates = templates
        self._theme_cache: GlitchTheme | None = None
        self._cache_time: float = 0.0
        self._cache_ttl: float = 600.0  # 10 min — theme rarely changes

    async def _get_theme(self, force_refresh: bool = False) -> GlitchTheme:
        """Load theme from Firestore with 60-second in-memory cache."""
        now = time.time()
        if (
            not force_refresh
            and self._theme_cache is not None
            and (now - self._cache_time) < self._cache_ttl
        ):
            return self._theme_cache

        try:
            doc = await self.db.collection("meta").document("theme").get()
            if doc.exists:
                self._theme_cache = GlitchTheme.model_validate(doc.to_dict())
            else:
                self._theme_cache = PRESET_THEMES["default"]
        except Exception:
            logger.exception("Failed to load theme from Firestore, using default")
            self._theme_cache = PRESET_THEMES["default"]

        self._cache_time = now
        return self._theme_cache

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Inject theme and nav into Jinja2 template globals."""
        # Check if a theme change was flagged (e.g. by /theme/apply)
        force_refresh = getattr(request.app.state, "_theme_bust", False)
        if force_refresh:
            request.app.state._theme_bust = False

        theme = await self._get_theme(force_refresh=force_refresh)
        nav = self.page_engine.get_nav_items() if self.page_engine else {}

        # Update Jinja2 globals (cache is disabled so this is safe)
        self.templates.env.globals["theme"] = theme
        self.templates.env.globals["nav"] = nav

        response = await call_next(request)
        return response
