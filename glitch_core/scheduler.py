"""Durable scheduler: fires a prompt into a channel on a daily/interval cadence.

The buildable slice of the workflow runtime. Runs as a background task in the app
lifespan; idempotent via each schedule's last_run, so a restart never double-fires.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from glitch_core import store
from glitch_core.agent import run_turn
from glitch_core.config import GlitchEnv
from glitch_core.db import Database

logger = logging.getLogger(__name__)

FIRE_TIMEOUT = 180  # hard cap on a single scheduled turn, so a nudge can't run away


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_due(schedule: dict, now_utc: datetime) -> bool:
    """Whether a schedule should fire at now_utc, given its last_run."""
    last = _parse(schedule.get("last_run"))
    kind = schedule.get("kind", "daily")

    if kind == "interval":
        interval = schedule.get("interval_seconds")
        if not interval:
            return False
        return last is None or (now_utc - last) >= timedelta(seconds=interval)

    # daily at_hour:at_minute in the schedule's tz
    hour = schedule.get("at_hour")
    if hour is None:
        return False
    minute = schedule.get("at_minute") or 0
    tz = _tz(schedule.get("tz", "UTC"))
    now_local = now_utc.astimezone(tz)
    target_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    target_utc = target_local.astimezone(timezone.utc)
    if now_utc < target_utc:
        return False  # not yet today
    return last is None or last < target_utc


async def _fire(db: Database, env: GlitchEnv, schedule: dict) -> None:
    channel = await store.get_channel(db, schedule["channel_id"])
    if channel is None:
        logger.warning("Schedule %s targets missing channel %s", schedule["id"], schedule["channel_id"])
        return
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in await store.list_messages(db, channel["id"], limit=20)
        if m["content"]
    ]
    content, segments = "", None
    try:
        async with asyncio.timeout(FIRE_TIMEOUT):
            async with aclosing(
                run_turn(
                    db=db, channel=channel, history=history, user_message=schedule["prompt"],
                    message_id=None, repo_root=str(env.repo_root), max_turns=8,
                )
            ) as turn:
                async for ev in turn:
                    if ev["type"] == "done":
                        content, segments = ev["content"], ev["segments"]
                    elif ev["type"] == "error":
                        content = f"(scheduled task failed: {ev['message']})"
    except TimeoutError:
        logger.warning("Schedule %s timed out after %ss", schedule["id"], FIRE_TIMEOUT)
        content = content or ""
    if content:
        await store.add_message(
            db, channel["id"], "agent", content,
            segments=segments, notification=bool(schedule.get("notify")),
        )
        logger.info("Fired schedule %s into #%s", schedule["id"], channel["id"])


async def run_scheduler(db: Database, env: GlitchEnv, poll_seconds: int = 30) -> None:
    logger.info("Scheduler started (poll=%ss)", poll_seconds)
    while True:
        try:
            now = datetime.now(timezone.utc)
            for schedule in await store.list_enabled_schedules(db):
                if is_due(schedule, now):
                    await _fire(db, env, schedule)
                    await store.set_schedule_last_run(db, schedule["id"], now.isoformat())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(poll_seconds)
