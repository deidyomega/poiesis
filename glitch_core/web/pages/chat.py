from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from glitch_core.config import DEFAULT_AGENT_ID
from glitch_core.web.engine import PageMeta

PAGE_META = PageMeta(
    title="Chat",
    icon="💬",
    nav_section="core",
    nav_order=15,
    route_prefix="/chat",
)

router = APIRouter(prefix="/chat")


@router.get("", response_class=HTMLResponse)
async def chat_page(request: Request, session: str = "") -> HTMLResponse:
    """Chat interface — routes to the correct agent based on session."""
    db = request.app.state.db
    templates = request.app.state.templates

    # Load available agents for the picker
    agents = []
    if db is not None:
        async for doc in db.collection("agents").limit(50).stream():
            if doc.id == "_placeholder":
                continue
            data = doc.to_dict()
            data["agent_id"] = doc.id
            if data.get("enabled"):
                agents.append(data)

    # If no session specified, use or create a default router session
    if not session:
        session = "default"

    # Ensure session exists
    if db is not None:
        session_ref = db.collection("sessions").document(session)
        session_doc = await session_ref.get()
        if not session_doc.exists:
            await session_ref.set({
                "session_id": session,
                "agent_id": DEFAULT_AGENT_ID,
                "created_at": datetime.utcnow(),
            })

        session_data = session_doc.to_dict() if session_doc.exists else {}
        current_agent_id = session_data.get("agent_id", DEFAULT_AGENT_ID)

        # Load messages
        messages = []
        msgs_ref = (
            session_ref.collection("messages")
            .order_by("created_at")
            .limit(30)
        )
        async for doc in msgs_ref.stream():
            data = doc.to_dict()
            data["message_id"] = doc.id
            messages.append(data)
    else:
        messages = []
        current_agent_id = DEFAULT_AGENT_ID

    # Load all sessions for the sidebar
    sessions = []
    if db is not None:
        async for doc in db.collection("sessions").limit(50).stream():
            if doc.id == "_placeholder":
                continue
            data = doc.to_dict()
            data["session_id"] = doc.id
            sessions.append(data)

    return templates.TemplateResponse(request, "chat.html", context={
        "messages": messages,
        "session_id": session,
        "current_agent_id": current_agent_id,
        "agents": agents,
        "sessions": sessions,
    })


@router.get("/firebase-config", response_class=JSONResponse)
async def firebase_config(request: Request) -> JSONResponse:
    """Serve Firebase config for the client-side JS SDK."""
    from glitch_core.config import GlitchEnv
    env = GlitchEnv()
    return JSONResponse({
        "projectId": env.firebase_project,
    })


@router.post("/new-session")
async def new_session(request: Request, agent_id: str = Form(DEFAULT_AGENT_ID)) -> RedirectResponse:
    """Create a new chat session with a specific agent."""
    db = request.app.state.db

    session_id = f"s_{uuid.uuid4().hex[:8]}"

    if db is not None:
        await db.collection("sessions").document(session_id).set({
            "session_id": session_id,
            "agent_id": agent_id,
            "created_at": datetime.utcnow(),
        })

    return RedirectResponse(url=f"/chat?session={session_id}", status_code=303)


@router.post("/send", response_class=HTMLResponse)
async def send_message(
    request: Request,
    message: str = Form(...),
    session_id: str = Form("default"),
) -> HTMLResponse:
    """Write a user message to Firestore. The daemon listener picks it up."""
    db = request.app.state.db

    if db is not None and message.strip():
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        await (
            db.collection("sessions")
            .document(session_id)
            .collection("messages")
            .document(msg_id)
            .set({
                "message_id": msg_id,
                "session_id": session_id,
                "role": "user",
                "content": message.strip(),
                "content_rating": "sfw",
                "attachments": [],
                "metadata": {},
                "created_at": datetime.utcnow(),
            })
        )

    return HTMLResponse(content="", status_code=204)


@router.post("/clear-session")
async def clear_session(request: Request, session_id: str = Form(...)) -> RedirectResponse:
    """Clear all messages from a session but keep the session alive."""
    db = request.app.state.db

    if db is not None:
        session_ref = db.collection("sessions").document(session_id)

        for sub in ["messages", "run_logs"]:
            batch = db.batch()
            count = 0
            async for doc in session_ref.collection(sub).stream():
                batch.delete(session_ref.collection(sub).document(doc.id))
                count += 1
                if count % 500 == 0:
                    await batch.commit()
                    batch = db.batch()
            if count % 500 != 0:
                await batch.commit()

    return RedirectResponse(url=f"/chat?session={session_id}", status_code=303)


@router.post("/delete-session")
async def delete_session(request: Request, session_id: str = Form(...)) -> RedirectResponse:
    """Delete a session and all its subcollections."""
    db = request.app.state.db

    if db is not None and session_id != "default":
        session_ref = db.collection("sessions").document(session_id)

        # Batch delete subcollections
        for sub in ["messages", "sub_tasks", "run_logs"]:
            batch = db.batch()
            count = 0
            async for doc in session_ref.collection(sub).stream():
                batch.delete(session_ref.collection(sub).document(doc.id))
                count += 1
                if count % 500 == 0:
                    await batch.commit()
                    batch = db.batch()
            if count % 500 != 0:
                await batch.commit()

        # Delete session doc
        await session_ref.delete()

    return RedirectResponse(url="/chat", status_code=303)
