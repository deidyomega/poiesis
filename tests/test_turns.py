"""Detached turn execution — generation is decoupled from any client connection."""

from __future__ import annotations

import asyncio
import types

import pytest

from poiesis import store
from poiesis.db import Database
from poiesis.migrations.runner import run_migrations
from poiesis.web import turns


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "t.db")
    await database.connect()
    await run_migrations(database)
    await store.upsert_channel(database, "general", "general")
    yield database
    await database.close()


ENV = types.SimpleNamespace(repo_root="/tmp", tz="UTC")


async def _seed_turn(db) -> str:
    await store.add_message(db, "general", "user", "hi")
    return await store.add_message(db, "general", "agent", "", status="generating")


async def _fake_run_turn(**kwargs):
    for w in ("Hello", " world"):
        yield {"type": "text", "delta": w}
    yield {"type": "done", "content": "Hello world",
           "segments": [{"type": "text", "content": "Hello world"}],
           "usage": {}, "cancelled": False, "session_id": "sess1"}


# ── the whole point: a turn finishes and persists with NO client attached ────

async def test_turn_completes_without_any_subscriber(db, monkeypatch):
    monkeypatch.setattr(turns, "run_turn", _fake_run_turn)
    aid = await _seed_turn(db)
    mgr = turns.TurnManager(db, ENV)
    turn = mgr.start(aid)
    await turn.task  # nobody ever connected

    m = await store.get_message(db, aid)
    assert m["status"] == "done"
    assert m["content"] == "Hello world"
    assert m["session_id"] == "sess1"
    assert m["segments"] == [{"type": "text", "content": "Hello world"}]


async def test_subscriber_receives_stream(db, monkeypatch):
    monkeypatch.setattr(turns, "run_turn", _fake_run_turn)
    aid = await _seed_turn(db)
    mgr = turns.TurnManager(db, ENV)
    turn = mgr.start(aid)             # task created but not yet run (no await since)
    q: asyncio.Queue = asyncio.Queue()
    turn.subscribers.add(q)          # attach before the task runs → capture everything

    got = []
    while True:
        ev = await asyncio.wait_for(q.get(), timeout=2)
        got.append(ev)
        if ev.get("t") == "__end__":
            break
    await turn.task
    kinds = [e.get("t") for e in got]
    assert "text" in kinds
    assert any(e.get("t") == "done" and e["content"] == "Hello world" for e in got)


async def test_cancel_marks_cancelled_and_keeps_partial(db, monkeypatch):
    async def _blocking(**kwargs):
        yield {"type": "text", "delta": "partial"}
        await asyncio.sleep(10)  # block so we can cancel mid-flight
        yield {"type": "done", "content": "x", "segments": None, "cancelled": False}

    monkeypatch.setattr(turns, "run_turn", _blocking)
    aid = await _seed_turn(db)
    mgr = turns.TurnManager(db, ENV)
    turn = mgr.start(aid)
    await asyncio.sleep(0.05)         # let it emit "partial" and start blocking

    assert await mgr.cancel(aid) is True
    with pytest.raises(asyncio.CancelledError):
        await turn.task

    m = await store.get_message(db, aid)
    assert m["status"] == "cancelled"
    assert m["cancelled"] is True     # derived from status
    assert "partial" in m["content"]


async def test_reset_generating_on_boot(db):
    aid = await _seed_turn(db)        # left as 'generating'
    n = await store.reset_generating_messages(db)
    assert n == 1
    m = await store.get_message(db, aid)
    assert m["status"] == "error" and "interrupted" in m["content"]
