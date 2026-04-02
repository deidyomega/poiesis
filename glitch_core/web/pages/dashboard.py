from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from glitch_core.web.engine import PageMeta

PAGE_META = PageMeta(
    title="Dashboard",
    icon="📊",
    nav_section="core",
    nav_order=10,
    route_prefix="/",
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Landing page with system overview."""
    db = request.app.state.db
    templates = request.app.state.templates

    worker_count = 0
    last_compaction = "Never"
    last_compaction_utc = "Never"
    version = "0.1.0"

    if db is not None:
        # Count workers
        async for doc in db.collection("workers").stream():
            if doc.id != "_placeholder":
                worker_count += 1

        # Last compaction
        comp_ref = db.collection("compaction_runs").order_by(
            "completed_at", direction="DESCENDING"
        ).limit(1)
        async for doc in comp_ref.stream():
            data = doc.to_dict()
            completed = data.get("completed_at")
            if completed:
                last_compaction_utc = completed.isoformat() if hasattr(completed, "isoformat") else str(completed)
                last_compaction = last_compaction_utc

        # Version from meta
        meta_doc = await db.collection("meta").document("project").get()
        if meta_doc.exists:
            meta = meta_doc.to_dict()
            version = meta.get("version", version)

    return templates.TemplateResponse(request, "dashboard.html", context={
        "worker_count": worker_count,
        "last_compaction": last_compaction,
        "last_compaction_utc": last_compaction_utc,
        "version": version,
    })
