from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from glitch_core.web.engine import PageMeta
from glitch_core.web.theming import PRESET_THEMES

PAGE_META = PageMeta(
    title="Themes",
    icon="🎨",
    nav_section="system",
    nav_order=30,
    route_prefix="/theme",
)

router = APIRouter(prefix="/theme")


@router.get("", response_class=HTMLResponse)
async def theme_page(request: Request) -> HTMLResponse:
    """Theme management page."""
    templates = request.app.state.templates

    return templates.TemplateResponse(request, "theme.html", context={
        "presets": PRESET_THEMES,
    })


@router.get("/picker", response_class=HTMLResponse)
async def theme_picker(request: Request) -> HTMLResponse:
    """Theme picker modal content."""
    templates = request.app.state.templates

    return templates.TemplateResponse(request, "components/theme_picker.html", context={
        "presets": PRESET_THEMES,
    })


@router.post("/apply")
async def apply_theme(request: Request, theme_name: str = Form(...)) -> RedirectResponse:
    """Apply a preset theme."""
    db = request.app.state.db

    if theme_name not in PRESET_THEMES:
        return HTMLResponse(content="Unknown theme", status_code=400)

    new_theme = PRESET_THEMES[theme_name]

    if db is not None:
        # Save current theme to history
        current_doc = await db.collection("meta").document("theme").get()
        if current_doc.exists:
            current = current_doc.to_dict()
            hist_id = f"theme_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            await db.collection("theme_history").document(hist_id).set({
                **current,
                "archived_at": datetime.utcnow(),
            })

        # Apply new theme to Firestore
        await db.collection("meta").document("theme").set(new_theme.model_dump())

    # Flag the middleware to bust its cache on the next request
    request.app.state._theme_bust = True

    # Redirect forces a full GET → middleware reloads theme → page renders with new colors
    return RedirectResponse(url="/theme", status_code=303)


@router.post("/generate", response_class=HTMLResponse)
async def generate_theme(request: Request, prompt: str = Form("")) -> HTMLResponse:
    """AI theme generation stub — Phase 2."""
    return HTMLResponse(
        content='<div class="p-4 text-sm text-glitch-muted">AI theme generation is not yet implemented. Coming in Phase 2.</div>',
        status_code=200,
    )
