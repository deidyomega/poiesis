"""Chat surface: channel rail, per-channel thread, SSE streaming, deploy card."""

from __future__ import annotations

import json
import time
from contextlib import aclosing

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from glitch_core import store
from glitch_core.agent import run_turn
from glitch_core.web.engine import PageMeta

router = APIRouter()
PAGE_META = PageMeta(title="Chat", icon="💬", nav_section="core", nav_order=10, route_prefix="/")

DEFAULT_CHANNEL = "general"


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


async def _render(request: Request, channel_id: str) -> HTMLResponse:
    db = request.app.state.db
    templates = request.app.state.templates
    channels = await store.list_channels(db)
    channel = await store.get_channel(db, channel_id)
    if channel is None and channels:
        channel = channels[0]
        channel_id = channel["id"]
    messages = await store.list_messages(db, channel_id) if channel else []
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "channels": channels,
            "channel": channel,
            "channel_id": channel_id,
            "messages": messages,
        },
    )


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return await _render(request, DEFAULT_CHANNEL)


@router.get("/c/{channel_id}", response_class=HTMLResponse)
async def channel_view(request: Request, channel_id: str) -> HTMLResponse:
    return await _render(request, channel_id)


@router.post("/chat/send")
async def send(request: Request, channel_id: str = Form(...), message: str = Form(...)):
    db = request.app.state.db
    message = message.strip()
    if not message:
        return JSONResponse({"error": "empty"}, status_code=400)
    user_id = await store.add_message(db, channel_id, "user", message)
    agent_id = await store.add_message(db, channel_id, "agent", "")
    return JSONResponse({"user_id": user_id, "agent_id": agent_id})


@router.get("/chat/stream/{agent_msg_id}")
async def stream(request: Request, agent_msg_id: str):
    db = request.app.state.db
    env = request.app.state.env

    agent_msg = await store.get_message(db, agent_msg_id)
    if agent_msg is None:
        return JSONResponse({"error": "unknown message"}, status_code=404)
    channel_id = agent_msg["channel_id"]
    channel = await store.get_channel(db, channel_id)

    msgs = await store.list_messages(db, channel_id)
    prior = [m for m in msgs if m["id"] != agent_msg_id]
    user_idx = next((i for i in range(len(prior) - 1, -1, -1) if prior[i]["role"] == "user"), None)
    user_message = prior[user_idx]["content"] if user_idx is not None else ""
    history = (
        [{"role": m["role"], "content": m["content"]} for m in prior[:user_idx]]
        if user_idx is not None
        else []
    )

    async def gen():
        accumulated = ""
        segments = None
        cancelled = False
        last_persist = 0.0
        try:
            async with aclosing(
                run_turn(
                    db=db,
                    channel=channel,
                    history=history,
                    user_message=user_message,
                    message_id=agent_msg_id,
                    repo_root=str(env.repo_root),
                )
            ) as turn:
                async for ev in turn:
                    if await request.is_disconnected():
                        cancelled = True
                        break
                    t = ev["type"]
                    if t == "text":
                        accumulated += ev["delta"]
                        yield _sse({"t": "text", "delta": ev["delta"]})
                    elif t == "thinking":
                        yield _sse({"t": "thinking", "delta": ev["delta"]})
                    elif t == "tool_call":
                        yield _sse({"t": "tool", "name": ev["name"]})
                    elif t == "tool_result":
                        yield _sse({"t": "tool_result", "name": ev["name"]})
                    elif t == "error":
                        yield _sse({"t": "error", "message": ev["message"]})
                    elif t == "done":
                        accumulated = ev["content"] or accumulated
                        segments = ev["segments"]
                        cancelled = ev["cancelled"]
                    now = time.time()
                    if now - last_persist > 0.6:
                        await store.update_message(db, agent_msg_id, content=accumulated)
                        last_persist = now
        finally:
            await store.update_message(
                db, agent_msg_id, content=accumulated, segments=segments, cancelled=cancelled
            )
        yield _sse({"t": "done", "content": accumulated, "segments": segments, "cancelled": cancelled})

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/chat/{channel_id}/deploys")
async def channel_deploys(request: Request, channel_id: str) -> JSONResponse:
    db = request.app.state.db
    rows = await store.list_deploys(db, limit=10)
    rows = [r for r in rows if r.get("channel_id") == channel_id]
    return JSONResponse({"deploys": rows})


@router.get("/chat/{channel_id}/messages")
async def channel_messages(request: Request, channel_id: str, after: str = "") -> JSONResponse:
    """New messages after an ISO timestamp — drives the live poller for proactive msgs."""
    db = request.app.state.db
    rows = await store.messages_after(db, channel_id, after)
    return JSONResponse({"messages": rows})


@router.post("/chat/clear")
async def clear(request: Request, channel_id: str = Form(...)):
    db = request.app.state.db
    await store.clear_channel(db, channel_id)
    return RedirectResponse(url=f"/c/{channel_id}" if channel_id != DEFAULT_CHANNEL else "/", status_code=303)
