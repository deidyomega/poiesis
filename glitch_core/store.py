"""Data-access helpers over the SQLite Database. Shared by web, agent, supervisor."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from glitch_core.db import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ── Channels ────────────────────────────────────────────────────────────────

async def list_channels(db: Database) -> list[dict[str, Any]]:
    return await db.fetch_all("SELECT * FROM channels ORDER BY created_at")


async def get_channel(db: Database, channel_id: str) -> dict[str, Any] | None:
    return await db.fetch_one("SELECT * FROM channels WHERE id = ?", (channel_id,))


async def upsert_channel(
    db: Database,
    channel_id: str,
    name: str,
    *,
    soul_path: str | None = None,
    model: str | None = None,
    cwd: str | None = None,
    allowed_tools: list[str] | None = None,
) -> None:
    now = _now()
    tools_json = json.dumps(allowed_tools) if allowed_tools is not None else None
    await db.execute(
        """
        INSERT INTO channels (id, name, soul_path, model, cwd, allowed_tools, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, soul_path=excluded.soul_path, model=excluded.model,
            cwd=excluded.cwd, allowed_tools=excluded.allowed_tools, updated_at=excluded.updated_at
        """,
        (channel_id, name, soul_path, model, cwd, tools_json, now, now),
    )


# ── Messages ────────────────────────────────────────────────────────────────

async def list_messages(db: Database, channel_id: str, limit: int = 200) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT * FROM messages WHERE channel_id = ? ORDER BY created_at DESC LIMIT ?",
        (channel_id, limit),
    )
    rows.reverse()
    for r in rows:
        r["segments"] = json.loads(r["segments"]) if r.get("segments") else None
        r["cancelled"] = bool(r["cancelled"])
        r["notification"] = bool(r.get("notification"))
    return rows


async def get_message(db: Database, message_id: str) -> dict[str, Any] | None:
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (message_id,))
    if row:
        row["segments"] = json.loads(row["segments"]) if row.get("segments") else None
        row["cancelled"] = bool(row["cancelled"])
        row["notification"] = bool(row.get("notification"))
    return row


async def add_message(
    db: Database,
    channel_id: str,
    role: str,
    content: str = "",
    *,
    segments: list[dict[str, Any]] | None = None,
    notification: bool = False,
) -> str:
    mid = _id("msg")
    await db.execute(
        "INSERT INTO messages (id, channel_id, role, content, segments, cancelled, notification, created_at) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
        (mid, channel_id, role, content, json.dumps(segments) if segments else None,
         1 if notification else 0, _now()),
    )
    return mid


async def messages_after(db: Database, channel_id: str, after_iso: str) -> list[dict[str, Any]]:
    """Messages created strictly after an ISO timestamp (for the live poller)."""
    rows = await db.fetch_all(
        "SELECT * FROM messages WHERE channel_id = ? AND created_at > ? ORDER BY created_at",
        (channel_id, after_iso),
    )
    for r in rows:
        r["segments"] = json.loads(r["segments"]) if r.get("segments") else None
        r["cancelled"] = bool(r["cancelled"])
        r["notification"] = bool(r.get("notification"))
    return rows


async def update_message(
    db: Database,
    message_id: str,
    *,
    content: str | None = None,
    segments: list[dict[str, Any]] | None = None,
    cancelled: bool | None = None,
) -> None:
    sets, params = [], []
    if content is not None:
        sets.append("content = ?")
        params.append(content)
    if segments is not None:
        sets.append("segments = ?")
        params.append(json.dumps(segments))
    if cancelled is not None:
        sets.append("cancelled = ?")
        params.append(1 if cancelled else 0)
    if not sets:
        return
    params.append(message_id)
    await db.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id = ?", tuple(params))


async def clear_channel(db: Database, channel_id: str) -> None:
    await db.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,))


# ── Memory + journal ──────────────────────────────────────────────────────────

async def list_memories(db: Database) -> list[dict[str, Any]]:
    return await db.fetch_all("SELECT * FROM memories ORDER BY updated_at DESC")


async def search_memories(db: Database, query: str) -> list[dict[str, Any]]:
    return await db.fetch_all(
        "SELECT * FROM memories WHERE content LIKE ? ORDER BY updated_at DESC",
        (f"%{query}%",),
    )


async def add_memory(db: Database, content: str) -> str:
    mid = _id("mem")
    now = _now()
    await db.execute(
        "INSERT INTO memories (id, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (mid, content, now, now),
    )
    return mid


async def add_journal(db: Database, channel_id: str | None, content: str) -> str:
    jid = _id("jrn")
    await db.execute(
        "INSERT INTO journal (id, channel_id, content, created_at) VALUES (?, ?, ?, ?)",
        (jid, channel_id, content, _now()),
    )
    return jid


# ── Deploys ───────────────────────────────────────────────────────────────────

async def create_deploy(
    db: Database,
    *,
    channel_id: str | None,
    message_id: str | None,
    summary: str | None,
    target_sha: str | None,
    rollback_sha: str | None,
    status: str = "requested",
) -> str:
    did = _id("dep")
    now = _now()
    await db.execute(
        "INSERT INTO deploys (id, channel_id, message_id, summary, status, reason, "
        "target_sha, rollback_sha, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        (did, channel_id, message_id, summary, status, target_sha, rollback_sha, now, now),
    )
    return did


async def update_deploy(
    db: Database, deploy_id: str, *, status: str | None = None, reason: str | None = None,
    target_sha: str | None = None,
) -> None:
    sets, params = ["updated_at = ?"], [_now()]
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if reason is not None:
        sets.append("reason = ?")
        params.append(reason)
    if target_sha is not None:
        sets.append("target_sha = ?")
        params.append(target_sha)
    params.append(deploy_id)
    await db.execute(f"UPDATE deploys SET {', '.join(sets)} WHERE id = ?", tuple(params))


async def get_deploy(db: Database, deploy_id: str) -> dict[str, Any] | None:
    return await db.fetch_one("SELECT * FROM deploys WHERE id = ?", (deploy_id,))


async def list_deploys(db: Database, limit: int = 20) -> list[dict[str, Any]]:
    return await db.fetch_all("SELECT * FROM deploys ORDER BY created_at DESC LIMIT ?", (limit,))


async def pending_deploys(db: Database) -> list[dict[str, Any]]:
    """Deploys the supervisor still needs to act on."""
    return await db.fetch_all(
        "SELECT * FROM deploys WHERE status = 'requested' ORDER BY created_at"
    )


# ── Settings ──────────────────────────────────────────────────────────────────

async def get_setting(db: Database, key: str) -> Any | None:
    row = await db.fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
    return json.loads(row["value"]) if row else None


async def set_setting(db: Database, key: str, value: Any) -> None:
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


# ── Schedules ─────────────────────────────────────────────────────────────────

async def list_enabled_schedules(db: Database) -> list[dict[str, Any]]:
    return await db.fetch_all("SELECT * FROM schedules WHERE enabled = 1")


async def get_schedule(db: Database, schedule_id: str) -> dict[str, Any] | None:
    return await db.fetch_one("SELECT * FROM schedules WHERE id = ?", (schedule_id,))


async def create_schedule(
    db: Database,
    *,
    channel_id: str,
    prompt: str,
    kind: str = "daily",
    at_hour: int | None = None,
    at_minute: int = 0,
    interval_seconds: int | None = None,
    tz: str = "UTC",
    notify: bool = True,
    schedule_id: str | None = None,
) -> str:
    sid = schedule_id or _id("sch")
    await db.execute(
        "INSERT INTO schedules (id, channel_id, prompt, kind, at_hour, at_minute, "
        "interval_seconds, tz, notify, enabled, last_run, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (sid, channel_id, prompt, kind, at_hour, at_minute, interval_seconds, tz,
         1 if notify else 0, _now()),
    )
    return sid


async def set_schedule_last_run(db: Database, schedule_id: str, when_iso: str) -> None:
    await db.execute(
        "UPDATE schedules SET last_run = ? WHERE id = ?", (when_iso, schedule_id)
    )
