"""Detached turn execution — generation decoupled from the browser connection.

A turn runs as an app-owned asyncio task, not inside the SSE request. It streams
tokens to any connected followers AND persists to SQLite as it goes, so the DB is
the source of truth: close the laptop mid-reply and the task finishes and saves;
reopen and the page reads it done. The stream endpoint is now a *follower* that can
attach, detach (on disconnect), and reattach (on reload) without affecting the turn.

Survives every client disconnect while the server is up. It does NOT survive a
server restart mid-turn (the task is in-process) — reset_generating_messages()
cleans those up on boot. True cross-restart durability is the web/engine split.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import aclosing
from typing import Any

from poiesis import store
from poiesis.agent import run_turn

logger = logging.getLogger(__name__)

_PERSIST_INTERVAL = 0.6   # seconds between incremental DB writes
_RETAIN_AFTER_DONE = 30   # keep a finished turn in the registry this long for late reconnects


class Turn:
    """Live state of one in-flight turn + its set of follower queues."""

    def __init__(self) -> None:
        self.accumulated = ""
        self.segments: list[dict[str, Any]] | None = None
        self.session_id: str | None = None
        self.done = False
        self.subscribers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None

    def emit(self, ev: dict[str, Any]) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(ev)
            except Exception:  # noqa: BLE001 — a slow/broken follower must never stall the turn
                pass


class TurnManager:
    """Owns the active turns. Attached to app.state; not tied to any request."""

    def __init__(self, db, env) -> None:
        self.db = db
        self.env = env
        self.turns: dict[str, Turn] = {}

    def get(self, agent_msg_id: str) -> Turn | None:
        return self.turns.get(agent_msg_id)

    def start(self, agent_msg_id: str) -> Turn:
        turn = Turn()
        self.turns[agent_msg_id] = turn
        turn.task = asyncio.create_task(self._run(agent_msg_id, turn))
        return turn

    async def cancel(self, agent_msg_id: str) -> bool:
        turn = self.turns.get(agent_msg_id)
        if turn and turn.task and not turn.task.done():
            turn.task.cancel()
            return True
        return False

    async def _run(self, agent_msg_id: str, turn: Turn) -> None:
        db, env = self.db, self.env
        accumulated = ""
        segments: list[dict[str, Any]] | None = None
        session_id: str | None = None
        error_msg: str | None = None
        try:
            agent_msg = await store.get_message(db, agent_msg_id)
            channel = await store.get_channel(db, agent_msg["channel_id"])
            msgs = await store.list_messages(db, agent_msg["channel_id"])
            prior = [m for m in msgs if m["id"] != agent_msg_id]
            uidx = next((i for i in range(len(prior) - 1, -1, -1) if prior[i]["role"] == "user"), None)
            user_message = prior[uidx]["content"] if uidx is not None else ""
            history = ([{"role": m["role"], "content": m["content"]} for m in prior[:uidx]]
                       if uidx is not None else [])

            last_persist = 0.0
            async with aclosing(run_turn(
                db=db, channel=channel, history=history, user_message=user_message,
                message_id=agent_msg_id, repo_root=str(env.repo_root), tz=env.tz,
            )) as gen:
                async for ev in gen:
                    t = ev["type"]
                    if t == "text":
                        accumulated += ev["delta"]
                        turn.accumulated = accumulated
                        turn.emit({"t": "text", "delta": ev["delta"]})
                    elif t == "tool_call":
                        turn.emit({"t": "tool", "name": ev["name"],
                                   "args": ev.get("args", ""), "id": ev.get("id")})
                    elif t == "tool_result":
                        turn.emit({"t": "tool_result", "name": ev["name"],
                                   "result": ev.get("result", ""), "id": ev.get("id")})
                    elif t == "error":
                        error_msg = ev["message"]
                        session_id = ev.get("session_id") or session_id
                        turn.emit({"t": "error", "message": error_msg})
                    elif t == "done":
                        accumulated = ev["content"] or accumulated
                        segments = ev["segments"]
                        session_id = ev.get("session_id") or session_id
                    now = time.time()
                    if now - last_persist > _PERSIST_INTERVAL:
                        await store.update_message(db, agent_msg_id, content=accumulated)
                        last_persist = now

            if error_msg and not (accumulated or "").strip():
                accumulated = f"⚠️ Turn failed: {error_msg}"
            status = "error" if (error_msg and not segments) else "done"
            turn.accumulated, turn.segments, turn.session_id = accumulated, segments, session_id
            await store.update_message(db, agent_msg_id, content=accumulated,
                                       segments=segments, session_id=session_id, status=status)
            turn.emit({"t": "done", "content": accumulated, "segments": segments,
                       "cancelled": False, "session_id": session_id})

        except asyncio.CancelledError:
            # Explicit Stop: persist whatever we have, mark cancelled, tell followers.
            await store.update_message(db, agent_msg_id, content=turn.accumulated,
                                       segments=turn.segments, status="cancelled")
            turn.emit({"t": "done", "content": turn.accumulated, "segments": turn.segments,
                       "cancelled": True, "session_id": turn.session_id})
            raise
        except Exception as e:  # noqa: BLE001 — persist the failure, don't crash the app
            logger.exception("detached turn %s failed", agent_msg_id)
            content = turn.accumulated or f"⚠️ Turn failed: {type(e).__name__}: {e}"
            await store.update_message(db, agent_msg_id, content=content, status="error")
            turn.emit({"t": "done", "content": content, "segments": turn.segments,
                       "cancelled": False, "session_id": turn.session_id})
        finally:
            turn.done = True
            turn.emit({"t": "__end__"})
            # Keep briefly so a reload landing right after completion can still attach.
            loop = asyncio.get_event_loop()
            loop.call_later(_RETAIN_AFTER_DONE, self.turns.pop, agent_msg_id, None)
