"""Idempotent first-run setup: ~/.glitch, .env scaffold, SQLite seed."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from glitch_core import gitops, store
from glitch_core.config import ENV_FILE, GLITCH_HOME, GlitchEnv
from glitch_core.db import Database
from glitch_core.migrations.runner import run_migrations
from glitch_core.web.auth import hash_password
from glitch_core.web.theming import PRESET_THEMES

logger = logging.getLogger(__name__)

DEFAULT_SOUL = """\
# Glitch — general

You are Glitch, a single-user self-hosted personal AI living in a web app you can
modify. You are direct, technical but not condescending, and you remember context
across conversations.

- When asked to change how this app works, edit your own code, verify it, then
  request a deploy. A supervisor health-checks and rolls back automatically.
- Keep replies tight. Don't narrate your tool use with filler.
"""


def _write_env(updates: dict[str, str], *, overwrite: bool) -> None:
    """Create/update ~/.glitch/.env. With overwrite=False, only adds missing keys."""
    GLITCH_HOME.mkdir(parents=True, exist_ok=True)
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


async def bootstrap(env: GlitchEnv | None = None, *, admin_password: str | None = None) -> None:
    env = env or GlitchEnv()

    # Session secret + username: keep existing if already set (don't churn logins).
    _write_env(
        {"GLITCH_SESSION_SECRET": secrets.token_urlsafe(48),
         "GLITCH_ADMIN_USERNAME": env.admin_username},
        overwrite=False,
    )
    # Password: when provided, always (re)set it.
    if admin_password:
        _write_env({"GLITCH_ADMIN_PASSWORD_HASH": hash_password(admin_password)}, overwrite=True)

    db = Database(env.db_path)
    await db.connect()
    await run_migrations(db)

    if await store.get_setting(db, "theme") is None:
        await store.set_setting(db, "theme", PRESET_THEMES["default"].model_dump())
        logger.info("Seeded default theme")

    if await store.get_channel(db, "general") is None:
        soul_rel = "souls/general.md"
        soul_file = Path(env.repo_root) / soul_rel
        if not soul_file.exists():
            soul_file.parent.mkdir(parents=True, exist_ok=True)
            soul_file.write_text(DEFAULT_SOUL)
        await store.upsert_channel(
            db, "general", "general", soul_path=soul_rel, cwd=str(env.repo_root)
        )
        logger.info("Seeded #general channel")

    # #project-management: persona + task.md (in a data dir, not the code repo) + 10am nudge.
    pm_dir = GLITCH_HOME / "pm"
    pm_dir.mkdir(parents=True, exist_ok=True)
    task_file = pm_dir / "task.md"
    if not task_file.exists():
        task_file.write_text("# Task list\n\n_(empty — tell me what you're working on)_\n")
    if await store.get_channel(db, "pm") is None:
        await store.upsert_channel(
            db, "pm", "project-management", soul_path="souls/pm.md", cwd=str(pm_dir),
            model="sonnet",
            allowed_tools=[
                "mcp__glitch__read_tasks", "mcp__glitch__write_tasks",
                "mcp__glitch__remember", "mcp__glitch__recall", "mcp__glitch__write_journal",
            ],
        )
        logger.info("Seeded #project-management channel")
    await store.create_schedule(
        db, channel_id="pm", schedule_id="sch_pm_daily",
        prompt="Review my task list and tell me what to focus on today. Be brief.",
        kind="daily", at_hour=10, at_minute=0, tz=env.tz, notify=True,
    )

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
