from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from glitch_core.agents import DEFAULT_PROMPTS, OUTPUT_TYPE_MAP
from glitch_core.web.engine import PageMeta

PAGE_META = PageMeta(
    title="Agents",
    icon="🤖",
    nav_section="system",
    nav_order=15,
    route_prefix="/agents",
)

router = APIRouter(prefix="/agents")


@router.get("", response_class=HTMLResponse)
async def agents_page(request: Request) -> HTMLResponse:
    """List all configured agents."""
    db = request.app.state.db
    templates = request.app.state.templates

    agents = []
    if db is not None:
        async for doc in db.collection("agents").stream():
            if doc.id == "_placeholder":
                continue
            data = doc.to_dict()
            data["agent_id"] = doc.id
            agents.append(data)

    agents.sort(key=lambda a: a.get("name", ""))

    return templates.TemplateResponse(request, "agents.html", context={
        "agents": agents,
        "output_types": list(OUTPUT_TYPE_MAP.keys()),
        "preset_prompts": DEFAULT_PROMPTS,
    })


@router.get("/{agent_id}/edit", response_class=HTMLResponse)
async def edit_agent(request: Request, agent_id: str) -> HTMLResponse:
    """Edit an agent's configuration and soul."""
    db = request.app.state.db
    templates = request.app.state.templates

    doc = await db.collection("agents").document(agent_id).get()
    if not doc.exists:
        return HTMLResponse(content="Agent not found", status_code=404)

    agent = doc.to_dict()
    agent["agent_id"] = agent_id

    return templates.TemplateResponse(request, "agent_edit.html", context={
        "agent": agent,
        "output_types": list(OUTPUT_TYPE_MAP.keys()),
    })


@router.post("/{agent_id}/save")
async def save_agent(
    request: Request,
    agent_id: str,
    name: str = Form(...),
    description: str = Form(""),
    model: str = Form(...),
    system_prompt: str = Form(""),
    output_type: str = Form("text"),
    triggers: str = Form(""),
    affinity: str = Form("any"),
    required_capabilities: str = Form(""),
    content_rating: str = Form("sfw"),
    timeout_seconds: int = Form(120),
    enabled: str = Form("off"),
) -> RedirectResponse:
    """Save agent configuration changes."""
    db = request.app.state.db

    trigger_list = [t.strip() for t in triggers.split(",") if t.strip()]
    cap_list = [c.strip() for c in required_capabilities.split(",") if c.strip()]

    await db.collection("agents").document(agent_id).update({
        "name": name,
        "description": description,
        "model": model,
        "system_prompt": system_prompt,
        "output_type": output_type,
        "triggers": trigger_list,
        "affinity": affinity,
        "required_capabilities": cap_list,
        "content_rating": content_rating,
        "timeout_seconds": timeout_seconds,
        "enabled": enabled == "on",
        "updated_at": datetime.utcnow(),
    })

    return RedirectResponse(url="/agents", status_code=303)


@router.post("/create")
async def create_agent(
    request: Request,
    agent_id: str = Form(...),
    name: str = Form(...),
    model: str = Form(...),
    preset: str = Form(""),
) -> RedirectResponse:
    """Create a new agent."""
    db = request.app.state.db

    # Get preset system prompt if selected
    system_prompt = DEFAULT_PROMPTS.get(preset, "")

    await db.collection("agents").document(agent_id).set({
        "agent_id": agent_id,
        "name": name,
        "description": "",
        "model": model,
        "system_prompt": system_prompt,
        "model_tier": "fast",
        "output_type": "text",
        "triggers": [],
        "tools": [],
        "affinity": "any",
        "required_capabilities": [],
        "fallback_agent": None,
        "fallback_window_seconds": 300,
        "content_rating": "sfw",
        "timeout_seconds": 120,
        "enabled": True,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    })

    return RedirectResponse(url=f"/agents/{agent_id}/edit", status_code=303)


@router.post("/{agent_id}/toggle")
async def toggle_agent(request: Request, agent_id: str) -> RedirectResponse:
    """Toggle an agent's enabled state."""
    db = request.app.state.db

    doc = await db.collection("agents").document(agent_id).get()
    if doc.exists:
        data = doc.to_dict()
        await db.collection("agents").document(agent_id).update({
            "enabled": not data.get("enabled", True),
            "updated_at": datetime.utcnow(),
        })

    return RedirectResponse(url="/agents", status_code=303)


@router.post("/{agent_id}/delete")
async def delete_agent(request: Request, agent_id: str) -> RedirectResponse:
    """Delete an agent."""
    db = request.app.state.db
    await db.collection("agents").document(agent_id).delete()
    return RedirectResponse(url="/agents", status_code=303)
