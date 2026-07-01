"""Idempotent first-run setup: ~/.poiesis, .env scaffold, SQLite seed."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from poiesis import gitops, store
from poiesis.config import ENV_FILE, POIESIS_HOME, PoiesisEnv
from poiesis.db import Database
from poiesis.migrations.runner import run_migrations
from poiesis.web.auth import hash_password
from poiesis.web.theming import PRESET_THEMES

logger = logging.getLogger(__name__)

DEFAULT_SOUL = """\
# Poiesis — general

You are Poiesis, a single-user self-hosted personal AI living in a web app. You are
direct, technical but not condescending, and you remember context across conversations.

- You can read your own source (Read/Grep/Glob) to explain how you work, but you can't
  modify it yet — self-mod is deferred while the supervisor's rollback net is off.
- Use `remember` for durable facts the user shares. Keep replies tight; skip filler.
"""

# The in-process MCP memory tools every channel gets. Self-mod (Write/Edit/Bash/
# request_deploy) is granted per-channel and is currently off everywhere (dev phase).
MEMORY_TOOLS = [
    "mcp__poiesis__remember", "mcp__poiesis__recall", "mcp__poiesis__write_journal",
]


def _write_env(updates: dict[str, str], *, overwrite: bool) -> None:
    """Create/update ~/.poiesis/.env. With overwrite=False, only adds missing keys."""
    POIESIS_HOME.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v
    changed = False
    for k, v in updates.items():
        if overwrite or k not in existing:
            if existing.get(k) != v:
                existing[k] = v
                changed = True
    if changed or not ENV_FILE.exists():
        ENV_FILE.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n")
        logger.info("Wrote %s", ENV_FILE)


async def bootstrap(env: PoiesisEnv | None = None, *, admin_password: str | None = None) -> None:
    env = env or PoiesisEnv()

    # Session secret + username: keep existing if already set (don't churn logins).
    _write_env(
        {"POIESIS_SESSION_SECRET": secrets.token_urlsafe(48),
         "POIESIS_ADMIN_USERNAME": env.admin_username},
        overwrite=False,
    )
    # Password: when provided, always (re)set it.
    if admin_password:
        _write_env({"POIESIS_ADMIN_PASSWORD_HASH": hash_password(admin_password)}, overwrite=True)

    db = Database(env.db_path)
    await db.connect()
    await run_migrations(db)

    if await store.get_setting(db, "theme") is None:
        await store.set_setting(db, "theme", PRESET_THEMES["default"].model_dump())
        logger.info("Seeded default theme")

    # #general: chat + read-only code access + memory. NO self-mod during the dev
    # phase (no Write/Edit/Bash/request_deploy), so an errant turn can't break the
    # live app while there's no supervisor to roll it back. Re-enable when self-mod
    # goes live (see ROADMAP).
    if await store.get_channel(db, "general") is None:
        soul_rel = "souls/general.md"
        soul_file = Path(env.repo_root) / soul_rel
        if not soul_file.exists():
            soul_file.parent.mkdir(parents=True, exist_ok=True)
            soul_file.write_text(DEFAULT_SOUL)
        await store.upsert_channel(
            db, "general", "general", soul_path=soul_rel, cwd=str(env.repo_root),
            allowed_tools=["Read", "Glob", "Grep", *MEMORY_TOOLS],
        )
        logger.info("Seeded #general channel (chat + read-only, no self-mod)")

    # #project-management: persona + task.md (in a data dir, not the code repo) + 10am nudge.
    pm_dir = POIESIS_HOME / "pm"
    pm_dir.mkdir(parents=True, exist_ok=True)
    task_file = pm_dir / "task.md"
    if not task_file.exists():
        task_file.write_text("# Task list\n\n_(empty — tell me what you're working on)_\n")
    if await store.get_channel(db, "pm") is None:
        await store.upsert_channel(
            db, "pm", "project-management", soul_path="souls/pm.md", cwd=str(pm_dir),
            model="sonnet",
            allowed_tools=["mcp__poiesis__read_tasks", "mcp__poiesis__write_tasks", *MEMORY_TOOLS],
        )
        logger.info("Seeded #project-management channel")
    await store.create_schedule(
        db, channel_id="pm", schedule_id="sch_pm_daily",
        prompt="Review my task list and tell me what to focus on today. Be brief.",
        kind="daily", at_hour=10, at_minute=0, tz=env.tz, notify=True,
    )

    # Chat channels (specialized pipelines layer on later). Chat + memory only —
    # no self-mod/deploy tools, so they can't touch the app's own code.
    for cid, soul in (("feature", "souls/feature.md"), ("bug", "souls/bug.md"),
                      ("analytics", "souls/analytics.md")):
        if await store.get_channel(db, cid) is None:
            await store.upsert_channel(
                db, cid, cid, soul_path=soul, model="sonnet", allowed_tools=MEMORY_TOOLS
            )
            logger.info("Seeded #%s channel", cid)

    if (
        gitops.has_git(env.repo_root)
        and await store.get_setting(db, "last_green_sha") is None
    ):
        sha = gitops.current_sha(env.repo_root)
        if sha:
            await store.set_setting(db, "last_green_sha", sha)
            logger.info("Recorded last-green sha %s", sha[:8])

    await db.close()
    logger.info("Bootstrap complete (db=%s)", env.db_path)
