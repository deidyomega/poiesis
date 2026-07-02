"""Chat surface: channel rail, per-channel thread, SSE streaming, deploy card."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from poiesis import store
from poiesis.agent import models

router = APIRouter()

DEFAULT_CHANNEL = "general"

# Where the Agent SDK writes per-turn session transcripts (one <session_id>.jsonl each,
# under a per-cwd project dir). We glob by session id, so the dir escaping doesn't matter.
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
_SESSION_RE = re.compile(r"^[A-Za-z0-9-]+$")  # guard glob against pattern injection


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _find_transcript(session_id: str | None) -> Path | None:
    if not session_id or not _SESSION_RE.match(session_id):
        return None
    hits = sorted(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def _parse_transcript(path: Path) -> list[dict]:
    """Flatten a session .jsonl into display blocks: prompt / thinking / tool / result / reply."""
    blocks: list[dict] = []
    for line in path.read_text().splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") not in ("user", "assistant"):
            continue
        content = o.get("message", {}).get("content")
        if isinstance(content, str):
            if content.strip():
                blocks.append({"who": o["type"], "label": o["type"], "body": content})
            continue
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and (b.get("text") or "").strip():
                who = o["type"]
                blocks.append({"who": who, "label": "prompt" if who == "user" else "reply", "body": b["text"]})
            elif bt == "thinking" and (b.get("thinking") or "").strip():
                blocks.append({"who": "thinking", "label": "thinking", "body": b["thinking"]})
            elif bt == "tool_use":
                blocks.append({"who": "tool", "label": f"tool · {b.get('name', '?')}",
                               "body": json.dumps(b.get("input", {}), indent=2)})
            elif bt == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c)
                blocks.append({"who": "result", "label": "result", "body": str(c)})
    return blocks


async def _render(request: Request, channel_id: str) -> HTMLResponse:
    db = request.app.state.db
    templates = request.app.state.templates
    channels = await store.list_channels(db)
    channel = await store.get_channel(db, channel_id)
    if channel is None and channels:
        channel = channels[0]
        channel_id = channel["id"]
    messages = await store.list_messages(db, channel_id) if channel else []
    # Model picker: only for OpenAI-compatible channels (#spice). Preselect the preset
    # matching the channel's effective model (channel override, else the env default).
    show_picker = bool(channel and channel.get("engine") == "openai")
    env = request.app.state.env
    current_model = (channel.get("model") if channel else None) or env.spice_model
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "channels": channels,
            "channel": channel,
            "channel_id": channel_id,
            "messages": messages,
            "model_options": models.SPICE_MODELS if show_picker else None,
            "current_model_id": models.id_for_model(current_model) if show_picker else None,
        },
    )


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return await _render(request, DEFAULT_CHANNEL)


@router.get("/c/{channel_id}", response_class=HTMLResponse)
async def channel_view(request: Request, channel_id: str) -> HTMLResponse:
    return await _render(request, channel_id)


_SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


@router.post("/chat/send")
async def send(request: Request, channel_id: str = Form(...), message: str = Form(...)):
    db = request.app.state.db
    message = message.strip()
    if not message:
        return JSONResponse({"error": "empty"}, status_code=400)
    user_id = await store.add_message(db, channel_id, "user", message)
    agent_id = await store.add_message(db, channel_id, "agent", "", status="generating")
    # Start the turn as a detached, client-independent task: it runs to completion and
    # persists to the DB no matter who's connected. The stream endpoint only *follows* it.
    request.app.state.turns.start(agent_id)
    return JSONResponse({"user_id": user_id, "agent_id": agent_id})


@router.post("/chat/{agent_msg_id}/cancel")
async def cancel(request: Request, agent_msg_id: str) -> JSONResponse:
    """Explicit Stop — cancels the detached turn (closing the stream no longer stops it)."""
    stopped = await request.app.state.turns.cancel(agent_msg_id)
    return JSONResponse({"cancelled": stopped})


@router.get("/chat/stream/{agent_msg_id}")
async def stream(request: Request, agent_msg_id: str):
    """Follow a detached turn: attach, stream live tokens, detach on disconnect (the turn
    keeps running server-side). If the turn already finished, replay its final DB state."""
    db = request.app.state.db
    turn = request.app.state.turns.get(agent_msg_id)

    if turn is None:
        # Not active in this process — done, or reconnecting after it finished. Serve the
        # final state from the DB as a one-shot so a reload still renders the full reply.
        m = await store.get_message(db, agent_msg_id)
        if m is None:
            return JSONResponse({"error": "unknown message"}, status_code=404)

        async def once():
            yield _sse({"t": "done", "content": m["content"], "segments": m["segments"],
                        "cancelled": m["status"] == "cancelled", "session_id": m.get("session_id")})

        return StreamingResponse(once(), media_type="text/event-stream", headers=_SSE_HEADERS)

    # Active: subscribe + snapshot atomically (no await between → no lost/duplicated tokens),
    # then catch the follower up and stream live.
    q: asyncio.Queue = asyncio.Queue()
    snapshot = turn.accumulated
    already_done = turn.done
    turn.subscribers.add(q)

    async def gen():
        try:
            yield _sse({"t": "catchup", "content": snapshot})  # reconnect sync; "" for fresh turns
            if already_done:
                yield _sse({"t": "done", "content": turn.accumulated, "segments": turn.segments,
                            "cancelled": False, "session_id": turn.session_id})
                return
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break  # detach only — the turn keeps running
                    yield ": keepalive\n\n"  # SSE comment; ignored by EventSource
                    continue
                if ev.get("t") == "__end__":
                    break
                yield _sse(ev)
        finally:
            turn.subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


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


@router.get("/transcript/{message_id}", response_class=HTMLResponse)
async def transcript(request: Request, message_id: str) -> HTMLResponse:
    """Full raw Agent-SDK transcript for one agent turn (untruncated tool I/O + thinking)."""
    db = request.app.state.db
    m = await store.get_message(db, message_id)
    if m is None:
        return HTMLResponse("<h1>404</h1><p>Unknown message.</p>", status_code=404)
    sid = m.get("session_id")
    path = _find_transcript(sid)
    return request.app.state.templates.TemplateResponse(
        request,
        "transcript.html",
        {
            "message": m,
            "session_id": sid,
            "path": str(path) if path else None,
            "blocks": _parse_transcript(path) if path else [],
        },
    )


@router.post("/chat/{channel_id}/model")
async def set_model(request: Request, channel_id: str, model_id: str = Form(...)):
    """Point an OpenAI-compatible channel at a picked model/endpoint (from the UI dropdown)."""
    db = request.app.state.db
    preset = models.by_id(model_id)
    if preset is None:
        return JSONResponse({"error": "unknown model"}, status_code=400)
    await store.set_channel_model(db, channel_id, preset["model"], preset["base_url"])
    return JSONResponse({"ok": True, "model": preset["model"], "label": preset["label"]})


@router.post("/chat/clear")
async def clear(request: Request, channel_id: str = Form(...)):
    db = request.app.state.db
    await store.clear_channel(db, channel_id)
    return RedirectResponse(url=f"/c/{channel_id}" if channel_id != DEFAULT_CHANNEL else "/", status_code=303)
