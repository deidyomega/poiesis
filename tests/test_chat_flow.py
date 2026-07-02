"""End-to-end decoupling through the real ASGI app: a turn survives the stream and can
be re-followed after it finishes (the "close laptop, reopen, read the answer" path)."""

from __future__ import annotations

import asyncio
import json
import types

import httpx
import pytest

from poiesis import store
from poiesis.db import Database
from poiesis.migrations.runner import run_migrations
from poiesis.web import turns as turns_mod
from poiesis.web.app import create_app


async def _fake_run_turn(**kwargs):
    for w in ("Hel", "lo"):
        await asyncio.sleep(0.02)
        yield {"type": "text", "delta": w}
    yield {"type": "done", "content": "Hello",
           "segments": [{"type": "text", "content": "Hello"}],
           "usage": {}, "cancelled": False, "session_id": "s1"}


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(turns_mod, "run_turn", _fake_run_turn)
    db = Database(tmp_path / "t.db")
    await db.connect()
    await run_migrations(db)               # skip lifespan; migrate + seed ourselves
    await store.upsert_channel(db, "general", "general")
    env = types.SimpleNamespace(
        admin_username="admin", admin_password="pw", admin_password_hash="",
        repo_root=str(tmp_path), tz="UTC", spice_model="",
        effective_session_secret=lambda: "test-secret",
    )
    app = create_app(db, env)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        await client.post("/login", data={"username": "admin", "password": "pw"},
                          follow_redirects=False)
        yield client, db
    await db.close()


async def _read_stream(client, aid):
    events = []
    async with client.stream("GET", f"/chat/stream/{aid}") as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
                if events[-1].get("t") == "done":
                    break
    return events


async def test_send_follow_and_reconnect_after_finish(ctx):
    client, db = ctx
    r = await client.post("/chat/send", data={"channel_id": "general", "message": "hi"})
    aid = r.json()["agent_id"]

    # First follow: catch-up then the live stream to completion.
    events = await asyncio.wait_for(_read_stream(client, aid), timeout=5)
    assert events[0]["t"] == "catchup"
    done = events[-1]
    assert done["t"] == "done" and done["content"] == "Hello"
    assert done["segments"] == [{"type": "text", "content": "Hello"}]

    # Persisted as done.
    m = await store.get_message(db, aid)
    assert m["status"] == "done" and m["content"] == "Hello"

    # Reconnect AFTER it finished (the reopen-the-laptop path) — still get the full answer.
    again = await asyncio.wait_for(_read_stream(client, aid), timeout=5)
    assert again[-1]["t"] == "done" and again[-1]["content"] == "Hello"


async def test_unknown_message_404(ctx):
    client, _ = ctx
    r = await client.get("/chat/stream/msg_does_not_exist")
    assert r.status_code == 404
