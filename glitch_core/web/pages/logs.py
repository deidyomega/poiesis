from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from glitch_core.web.engine import PageMeta

PAGE_META = PageMeta(
    title="Run Logs",
    icon="📋",
    nav_section="system",
    nav_order=25,
    route_prefix="/logs",
)

router = APIRouter(prefix="/logs")


@router.get("", response_class=HTMLResponse)
async def logs_page(request: Request, session: str = "") -> HTMLResponse:
    """List all run logs across sessions."""
    db = request.app.state.db
    templates = request.app.state.templates

    logs = []
    sessions = []

    if db is not None:
        # Get sessions for the filter dropdown
        async for doc in db.collection("sessions").limit(50).stream():
            if doc.id == "_placeholder":
                continue
            data = doc.to_dict()
            data["session_id"] = doc.id
            sessions.append(data)

        # Load logs from selected session or all sessions
        target_sessions = [session] if session else [s["session_id"] for s in sessions]

        for sid in target_sessions[:10]:
            logs_ref = (
                db.collection("sessions")
                .document(sid)
                .collection("run_logs")
                .order_by("created_at", direction="DESCENDING")
                .limit(20)
            )
            async for doc in logs_ref.stream():
                data = doc.to_dict()
                data["log_id"] = doc.id
                data["session_id"] = sid
                logs.append(data)

        logs.sort(key=lambda l: l.get("created_at", ""), reverse=True)
        logs = logs[:50]

    return templates.TemplateResponse(request, "logs.html", context={
        "logs": logs,
        "sessions": sessions,
        "selected_session": session,
    })


@router.get("/{session_id}/{log_id}", response_class=HTMLResponse)
async def log_detail(request: Request, session_id: str, log_id: str) -> HTMLResponse:
    """View full PydanticAI trace for a single run."""
    db = request.app.state.db
    templates = request.app.state.templates

    doc = await (
        db.collection("sessions")
        .document(session_id)
        .collection("run_logs")
        .document(log_id)
        .get()
    )

    if not doc.exists:
        return HTMLResponse(content="Log not found", status_code=404)

    log_data = doc.to_dict()
    log_data["log_id"] = log_id
    log_data["session_id"] = session_id

    # Parse the all_messages JSON string into structured data
    raw_messages = log_data.get("all_messages", "[]")
    try:
        if isinstance(raw_messages, str):
            parsed = json.loads(raw_messages)
        else:
            parsed = raw_messages
    except (json.JSONDecodeError, TypeError):
        parsed = []

    # Split into history (context) vs this exchange (the actual run)
    # PydanticAI puts the message_history first, then the new exchange.
    # The new exchange starts where the system prompt appears (if present)
    # or we can detect it by looking for the run_id on messages.
    #
    # Simpler heuristic: everything before the last user-prompt is history,
    # the last user-prompt and everything after is "this exchange".
    last_user_idx = 0
    for i, msg in enumerate(parsed):
        parts = msg.get("parts", [])
        for part in parts:
            if part.get("part_kind") == "user-prompt":
                last_user_idx = i

    history = parsed[:last_user_idx] if last_user_idx > 0 else []
    exchange = parsed[last_user_idx:] if parsed else []

    return templates.TemplateResponse(request, "log_detail.html", context={
        "log": log_data,
        "history": history,
        "exchange": exchange,
    })
