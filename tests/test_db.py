from __future__ import annotations

import pytest

from glitch_core import store
from glitch_core.db import Database
from glitch_core.migrations.runner import run_migrations


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "t.db")
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


async def test_migrations_idempotent(db):
    # Second run applies nothing.
    assert await run_migrations(db) == []


async def test_channel_and_message_roundtrip(db):
    await store.upsert_channel(db, "general", "general", cwd="/tmp/x")
    chans = await store.list_channels(db)
    assert len(chans) == 1 and chans[0]["cwd"] == "/tmp/x"

    await store.add_message(db, "general", "user", "hello")
    aid = await store.add_message(db, "general", "agent", "")
    await store.update_message(db, aid, content="hi", segments=[{"type": "text", "content": "hi"}])

    msgs = await store.list_messages(db, "general")
    assert [m["role"] for m in msgs] == ["user", "agent"]
    assert msgs[1]["segments"][0]["type"] == "text"

    await store.clear_channel(db, "general")
    assert await store.list_messages(db, "general") == []


async def test_memory_and_settings(db):
    await store.add_memory(db, "User prefers dark mode")
    await store.add_memory(db, "User lives in Denver")
    assert len(await store.list_memories(db)) == 2
    assert len(await store.search_memories(db, "dark")) == 1

    await store.set_setting(db, "last_green_sha", "abc123")
    assert await store.get_setting(db, "last_green_sha") == "abc123"
    assert await store.get_setting(db, "missing") is None


async def test_deploy_lifecycle(db):
    did = await store.create_deploy(
        db, channel_id="general", message_id=None, summary="add /ping",
        target_sha="t", rollback_sha="r",
    )
    assert len(await store.pending_deploys(db)) == 1
    await store.update_deploy(db, did, status="live")
    assert len(await store.pending_deploys(db)) == 0
    assert (await store.get_deploy(db, did))["status"] == "live"
