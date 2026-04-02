from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from glitch_core.web.engine import PageMeta

PAGE_META = PageMeta(
    title="Workers",
    icon="🖥️",
    nav_section="system",
    nav_order=20,
    route_prefix="/workers",
)

router = APIRouter(prefix="/workers")


@router.get("", response_class=HTMLResponse)
async def workers_page(request: Request) -> HTMLResponse:
    """Worker status page listing all registered workers."""
    db = request.app.state.db
    templates = request.app.state.templates

    workers = []

    if db is not None:
        async for doc in db.collection("workers").stream():
            if doc.id == "_placeholder":
                continue
            data = doc.to_dict()
            data["worker_id"] = doc.id

            # Determine online status (heartbeat within last 2 minutes)
            last_hb = data.get("last_heartbeat")
            if last_hb and isinstance(last_hb, datetime):
                data["online"] = (datetime.now(timezone.utc) - last_hb) < timedelta(minutes=2)
                data["last_heartbeat_utc"] = last_hb.isoformat()
            else:
                data["online"] = False
                data["last_heartbeat_utc"] = ""

            workers.append(data)

    workers.sort(key=lambda w: (not w.get("online", False), w.get("node_name", "")))

    return templates.TemplateResponse(request, "workers.html", context={
        "workers": workers,
    })
