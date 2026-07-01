"""Chat surface: channel rail, per-channel thread, SSE streaming, deploy card."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from contextlib import aclosing
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from poiesis import store
from poiesis.agent import run_turn

router = APIRouter()
logger = logging.getLogger(__name__)

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
        session_id = None
        error_msg = None
        last_persist = 0.0

        # Run the turn in a producer task feeding a queue, so the SSE loop can emit
        # keepalive comments during long gaps (a tool running for minutes). Without
        # this, Cloudflare/proxies drop the idle connection (~100s) mid-turn.
        queue: asyncio.Queue = asyncio.Queue()

        async def produce():
            try:
                async with aclosing(
                    run_turn(
                        db=db, channel=channel, history=history, user_message=user_message,
                        message_id=agent_msg_id, repo_root=str(env.repo_root), tz=env.tz,
                    )
                ) as turn:
                    async for ev in turn:
                        await queue.put(ev)
            except Exception as e:  # noqa: BLE001 — surface to the stream
                # run_turn logs a traceback for failures inside its own query loop, but a
                # raise *before* that loop (soul/memory/mcp setup) escapes here with no
                # server-side stack. Log it so a blank/errored turn is always debuggable.
                logger.exception("agent turn setup failed for message %s", agent_msg_id)
                await queue.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                await queue.put({"type": "__end__"})

        producer = asyncio.create_task(produce())
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        cancelled = True
                        break
                    yield ": keepalive\n\n"  # SSE comment; ignored by EventSource
                    continue
                if ev.get("type") == "__end__":
                    break
                if await request.is_disconnected():
                    cancelled = True
                    break
                t = ev["type"]
                if t == "text":
                    accumulated += ev["delta"]
                    yield _sse({"t": "text", "delta": ev["delta"]})
                elif t == "tool_call":
                    yield _sse({"t": "tool", "name": ev["name"],
                                "args": ev.get("args", ""), "id": ev.get("id")})
                elif t == "tool_result":
                    yield _sse({"t": "tool_result", "name": ev["name"],
                                "result": ev.get("result", ""), "id": ev.get("id")})
                elif t == "error":
                    error_msg = ev["message"]
                    session_id = ev.get("session_id") or session_id
                    yield _sse({"t": "error", "message": error_msg})
                elif t == "done":
                    accumulated = ev["content"] or accumulated
                    segments = ev["segments"]
                    cancelled = ev["cancelled"]
                    session_id = ev.get("session_id") or session_id
                now = time.time()
                if now - last_persist > 0.6:
                    await store.update_message(db, agent_msg_id, content=accumulated)
                    last_persist = now
        finally:
            producer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await producer
            # If the turn errored with nothing usable captured, save the error text so the
            # bubble shows what went wrong instead of a silent blank (the old failure mode).
            if error_msg and not (accumulated or "").strip():
                accumulated = f"⚠️ Turn failed: {error_msg}"
            elif not (accumulated or "").strip() and not cancelled:
                # Turn succeeded but produced no text (tools/thinking only, empty, or
                # filtered). Bubble looks blank; leave a breadcrumb pointing at the
                # transcript so it's greppable in journald.
                logger.warning(
                    "agent turn %s produced empty content (session=%s)", agent_msg_id, session_id
                )
            await store.update_message(
                db, agent_msg_id, content=accumulated, segments=segments,
                cancelled=cancelled, session_id=session_id,
            )
        yield _sse({"t": "done", "content": accumulated, "segments": segments,
                    "cancelled": cancelled, "session_id": session_id})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        # no-transform stops the Cloudflare Tunnel edge from gzipping the stream
        # (which would buffer it for the browser and break per-chunk rendering);
        # X-Accel-Buffering disables nginx/proxy buffering.
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


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


@router.post("/chat/clear")
async def clear(request: Request, channel_id: str = Form(...)):
    db = request.app.state.db
    await store.clear_channel(db, channel_id)
    return RedirectResponse(url=f"/c/{channel_id}" if channel_id != DEFAULT_CHANNEL else "/", status_code=303)
