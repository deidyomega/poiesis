"""PWA shell — manifest, service worker, and icons so Poiesis installs as a home-screen
app (standalone, chromeless). Android-first + single-user, so no iOS icon/splash zoo.

The service worker is minimal for now (its presence + a fetch handler make the app
installable); offline-shell caching and Web Push land in later passes.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, Response

from poiesis.web.theming import PRESET_THEMES

router = APIRouter()
STATIC = Path(__file__).parent / "static"

SERVICE_WORKER = """\
// Minimal service worker — presence + a fetch handler satisfy install criteria and
// enable standalone launch. Offline-shell caching and push come later.
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (e) => { /* network pass-through */ });
"""


@router.get("/manifest.webmanifest")
async def manifest() -> JSONResponse:
    theme = PRESET_THEMES["default"]
    return JSONResponse(
        {
            "name": theme.app_name,
            "short_name": theme.app_name,
            "description": "Single-user, self-hosted personal AI.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": theme.colors.bg,
            "theme_color": theme.colors.bg,
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
                 "purpose": "any maskable"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "any maskable"},
            ],
        },
        media_type="application/manifest+json",
    )


@router.get("/sw.js")
async def service_worker() -> Response:
    return Response(
        SERVICE_WORKER,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@router.get("/icon-{size}.png")
async def icon(size: str):
    path = STATIC / f"icon-{size}.png"
    if size not in ("192", "512") or not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="image/png")
