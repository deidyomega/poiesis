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
async def generate_theme_route(request: Request, prompt: str = Form("")) -> HTMLResponse:
    """Generate a theme from a natural language description via AI."""
    if not prompt.strip():
        return HTMLResponse(
            content='<div class="p-4 text-sm text-glitch-error">Please describe the theme you want.</div>',
            status_code=200,
        )

    db = request.app.state.db

    # Build a lightweight coder agent for theme generation
    try:
        from glitch_core.agents import load_agents_from_firestore, create_agent_from_config
        agents = await load_agents_from_firestore(db)
        coder_cfg = next((a for a in agents if a.agent_id == "coder"), None)
        if coder_cfg is None:
            # Fall back to router config
            coder_cfg = next((a for a in agents if a.agent_id == "router"), None)
        if coder_cfg is None:
            return HTMLResponse(
                content='<div class="p-4 text-sm text-glitch-error">No agent available for theme generation.</div>',
                status_code=200,
            )
        coder_agent = create_agent_from_config(coder_cfg)
    except Exception as e:
        return HTMLResponse(
            content=f'<div class="p-4 text-sm text-glitch-error">Failed to load agent: {e}</div>',
            status_code=200,
        )

    from glitch_core.ouroboros.theme_generator import generate_theme
    theme = await generate_theme(coder_agent, db, prompt)

    if theme is None:
        return HTMLResponse(
            content='<div class="p-4 text-sm text-glitch-error">Theme generation failed. Try a different description.</div>',
            status_code=200,
        )

    # Bust the theme cache so middleware picks up the new theme
    request.app.state._theme_bust = True

    return HTMLResponse(
        content=(
            f'<div class="p-4 text-sm text-glitch-text">'
            f'Theme "<strong>{theme.display_name}</strong>" generated and applied! '
            f'<a href="/theme" class="underline text-glitch-accent">Refresh to see it.</a>'
            f'</div>'
        ),
        status_code=200,
    )
