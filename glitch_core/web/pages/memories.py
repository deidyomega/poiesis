from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from glitch_core.web.engine import PageMeta

PAGE_META = PageMeta(
    title="Memories",
    icon="🧠",
    nav_section="core",
    nav_order=30,
    route_prefix="/memories",
)

router = APIRouter(prefix="/memories")


@router.get("", response_class=HTMLResponse)
async def memories_page(request: Request, search: str = "", category: str = "") -> HTMLResponse:
    """Memory browser with search and category filter."""
    db = request.app.state.db
    templates = request.app.state.templates

    memories = []
    categories = set()

    if db is not None:
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = db.collection("core_memories").where(
            filter=FieldFilter("deleted", "==", False)
        ).limit(200)
        if category:
            query = query.where(filter=FieldFilter("category", "==", category))

        async for doc in query.stream():
            if doc.id == "_placeholder":
                continue
            data = doc.to_dict()
            if search and search.lower() not in data.get("content", "").lower():
                continue
            data["memory_id"] = doc.id
            memories.append(data)
            categories.add(data.get("category", "other"))

    memories.sort(key=lambda m: m.get("updated_at", ""), reverse=True)

    return templates.TemplateResponse(request, "memories.html", context={
        "memories": memories,
        "categories": sorted(categories),
        "search": search,
        "selected_category": category,
    })


@router.get("/{memory_id}/detail", response_class=HTMLResponse)
async def memory_detail(request: Request, memory_id: str) -> HTMLResponse:
    """Expanded memory view with edit form."""
    db = request.app.state.db
    templates = request.app.state.templates

    doc = await db.collection("core_memories").document(memory_id).get()
    memory = doc.to_dict() if doc.exists else {}
    memory["memory_id"] = memory_id

    return templates.TemplateResponse(request, "components/memory_detail.html", context={
        "memory": memory,
    })


@router.get("/{memory_id}/card", response_class=HTMLResponse)
async def memory_card(request: Request, memory_id: str) -> HTMLResponse:
    """Return a single memory card (for cancel from edit)."""
    db = request.app.state.db
    templates = request.app.state.templates

    doc = await db.collection("core_memories").document(memory_id).get()
    memory = doc.to_dict() if doc.exists else {}
    memory["memory_id"] = memory_id

    return templates.TemplateResponse(request, "components/memory_card.html", context={
        "memory": memory,
    })


@router.post("/{memory_id}/update", response_class=HTMLResponse)
async def update_memory(
    request: Request,
    memory_id: str,
    content: str = Form(...),
    category: str = Form("other"),
) -> HTMLResponse:
    """Update a memory's content and category."""
    db = request.app.state.db
    templates = request.app.state.templates

    doc_ref = db.collection("core_memories").document(memory_id)
    doc = await doc_ref.get()

    if doc.exists:
        old_data = doc.to_dict()
        await doc_ref.update({
            "previous_content": old_data.get("content", ""),
            "content": content,
            "category": category,
            "version": old_data.get("version", 1) + 1,
            "updated_at": datetime.utcnow(),
        })

    updated_doc = await doc_ref.get()
    memory = updated_doc.to_dict() if updated_doc.exists else {}
    memory["memory_id"] = memory_id

    return templates.TemplateResponse(request, "components/memory_card.html", context={
        "memory": memory,
    })


@router.post("/{memory_id}/rollback", response_class=HTMLResponse)
async def rollback_memory(request: Request, memory_id: str) -> HTMLResponse:
    """Revert a memory to its previous content."""
    db = request.app.state.db
    templates = request.app.state.templates

    doc_ref = db.collection("core_memories").document(memory_id)
    doc = await doc_ref.get()

    if doc.exists:
        data = doc.to_dict()
        previous = data.get("previous_content")
        if previous:
            await doc_ref.update({
                "content": previous,
                "previous_content": data.get("content", ""),
                "version": data.get("version", 1) + 1,
                "updated_at": datetime.utcnow(),
            })

    updated_doc = await doc_ref.get()
    memory = updated_doc.to_dict() if updated_doc.exists else {}
    memory["memory_id"] = memory_id

    return templates.TemplateResponse(request, "components/memory_card.html", context={
        "memory": memory,
    })


@router.post("/{memory_id}/delete", response_class=HTMLResponse)
async def delete_memory(request: Request, memory_id: str) -> HTMLResponse:
    """Soft-delete a memory by moving it to memories_deleted."""
    db = request.app.state.db

    doc_ref = db.collection("core_memories").document(memory_id)
    doc = await doc_ref.get()

    if doc.exists:
        data = doc.to_dict()
        data["deleted"] = True
        data["deleted_at"] = datetime.utcnow()
        await db.collection("memories_deleted").document(memory_id).set(data)
        await doc_ref.update({"deleted": True})

    return HTMLResponse(content="", status_code=200)
