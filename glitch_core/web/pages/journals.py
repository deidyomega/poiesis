from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from glitch_core.web.engine import PageMeta

PAGE_META = PageMeta(
    title="Journals",
    icon="📓",
    nav_section="core",
    nav_order=40,
    route_prefix="/journals",
)

router = APIRouter(prefix="/journals")


@router.get("", response_class=HTMLResponse)
async def journals_page(
    request: Request,
    search: str = "",
    topic: str = "",
    include_archived: bool = False,
) -> HTMLResponse:
    """Journal browser with search and topic filter."""
    db = request.app.state.db
    templates = request.app.state.templates

    entries = []
    topics = set()

    if db is not None:
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = db.collection("journals").order_by("created_at", direction="DESCENDING").limit(50)
        if not include_archived:
            query = query.where(filter=FieldFilter("archived", "==", False))

        async for doc in query.stream():
            if doc.id == "_placeholder":
                continue
            data = doc.to_dict()
            if topic and data.get("topic") != topic:
                continue
            if search and search.lower() not in data.get("content", "").lower():
                continue

            data["journal_id"] = doc.id
            entries.append(data)
            if data.get("topic"):
                topics.add(data["topic"])

    return templates.TemplateResponse(request, "journals.html", context={
        "entries": entries,
        "topics": sorted(topics),
        "search": search,
        "selected_topic": topic,
        "include_archived": include_archived,
    })
