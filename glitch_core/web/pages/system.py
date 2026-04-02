from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from glitch_core.schemas import FeatureFlags, ProjectMeta
from glitch_core.web.engine import PageMeta

PAGE_META = PageMeta(
    title="System",
    icon="⚙️",
    nav_section="system",
    nav_order=10,
    route_prefix="/system",
)

router = APIRouter(prefix="/system")


@router.get("", response_class=HTMLResponse)
async def system_page(request: Request) -> HTMLResponse:
    """System admin page with project settings, feature flags, and compaction history."""
    db = request.app.state.db
    templates = request.app.state.templates

    project_meta = ProjectMeta()
    feature_flags = FeatureFlags()
    compaction_runs = []
    agents = []

    if db is not None:
        # Project meta
        meta_doc = await db.collection("meta").document("project").get()
        if meta_doc.exists:
            meta = meta_doc.to_dict()
            project_meta = ProjectMeta.model_validate(meta)
            flags_raw = meta.get("feature_flags", {})
            if isinstance(flags_raw, dict):
                feature_flags = FeatureFlags.model_validate(flags_raw)

        # Available agents (for default_agent dropdown)
        async for doc in db.collection("agents").limit(50).stream():
            if doc.id == "_placeholder":
                continue
            data = doc.to_dict()
            agents.append({"agent_id": doc.id, "name": data.get("name", doc.id)})

        # Compaction runs
        comp_ref = db.collection("compaction_runs").order_by(
            "completed_at", direction="DESCENDING"
        ).limit(10)
        async for doc in comp_ref.stream():
            if doc.id == "_placeholder":
                continue
            data = doc.to_dict()
            data["run_id"] = doc.id
            compaction_runs.append(data)

    # Build flag list dynamically from the FeatureFlags model
    flag_items = []
    for field_name, field_info in FeatureFlags.model_fields.items():
        value = getattr(feature_flags, field_name)
        label = field_name.replace("_", " ").title()
        flag_items.append({
            "key": field_name,
            "label": label,
            "value": value,
            "description": field_info.description or "",
        })

    return templates.TemplateResponse(request, "system.html", context={
        "project_meta": project_meta,
        "flag_items": flag_items,
        "agents": agents,
        "compaction_runs": compaction_runs,
    })


@router.post("/flags/toggle")
async def toggle_flag(request: Request, flag: str = Form(...)) -> RedirectResponse:
    """Toggle a feature flag."""
    db = request.app.state.db

    if db is not None:
        if flag not in FeatureFlags.model_fields:
            return RedirectResponse(url="/system", status_code=303)

        meta_ref = db.collection("meta").document("project")
        meta_doc = await meta_ref.get()

        if meta_doc.exists:
            meta = meta_doc.to_dict()
            flags_raw = meta.get("feature_flags", {})
            current = flags_raw.get(flag, False)
            flags_raw[flag] = not current
            await meta_ref.update({"feature_flags": flags_raw})

    return RedirectResponse(url="/system", status_code=303)


@router.post("/settings/default-agent")
async def set_default_agent(request: Request, default_agent: str = Form(...)) -> RedirectResponse:
    """Change the default agent."""
    db = request.app.state.db

    if db is not None:
        await db.collection("meta").document("project").update({
            "default_agent": default_agent,
        })

    return RedirectResponse(url="/system", status_code=303)
