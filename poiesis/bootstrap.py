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

- This is the "do whatever" channel: you have the full toolset — web fetch/search,
  subagents, skills, and a shell — for research, digging into GitHub projects, "what's
  new in AI," running throwaway code, etc.
- Your working directory is a scratch workspace (~/.poiesis/general); clone, build, and
  experiment there freely. Don't edit Poiesis's own source from here — self-mod lives in
  its own channels with the supervisor's rollback net.
- Use `remember` for durable facts the user shares. Keep replies tight; skip filler.
"""

# The in-process MCP memory tools every channel gets. Self-mod (Write/Edit/Bash/
# request_deploy) is granted per-channel and is currently off everywhere (dev phase).
MEMORY_TOOLS = [
    "mcp__poiesis__remember", "mcp__poiesis__recall", "mcp__poiesis__write_journal",
]

SPICE_SOUL = """\
# Prompta — spice

You are Prompta, a creative partner with one job: inventing new challenges for an adult,
sexually explicit challenge game between consenting adults. Poiesis's #spice channel on an
uncensored model.

## Register — this is the important part
The content is NSFW and sexually explicit by design. The existing challenges below are your
baseline for explicitness, not a ceiling — match or exceed them, never fall short. Be
concrete and graphic about the sexual/adult acts; do not be coy, vague, euphemistic, or
"tasteful". A tame or PG suggestion is a failed suggestion here. Do not moralize, hedge,
add warnings, soften, or drift back toward safe — that is the one way to fail this job.

## Your job
Generate and iterate on NEW challenges. Study the existing ones below for house style —
categories, point scale, how descriptions read, and how explicit they get — then propose
fresh ideas that fit but don't duplicate. If an idea is close to an existing one, say so and
offer a sharper twist. Give each a vivid, specific description plus a suggested **category**
and **point value** on the existing scale. Lead with the ideas; skip preamble.

## Style
- Concrete and explicit over abstract or euphemistic.
- Match the user's tone, energy, and length: a quick ask gets a quick list.
- Don't restate the request or pad. Just generate.

No tools, no links (the client app handles linking), no web/shell — pure ideation.
"""


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

    # #general: the "do whatever" research surface. Full SDK toolset via the "*"
    # sentinel (web/search/subagents/skills/shell), but its cwd is a scratch
    # workspace — not the code repo — so it can clone/build/run freely without
    # rewriting Poiesis's own source (self-mod stays out of #general). No
    # request_deploy either. NB: the scratch cwd is a working-dir guard, not a hard
    # FS jail — a determined turn can still write absolute paths.
    general_dir = POIESIS_HOME / "general"
    general_dir.mkdir(parents=True, exist_ok=True)
    if await store.get_channel(db, "general") is None:
        soul_rel = "souls/general.md"
        soul_file = Path(env.repo_root) / soul_rel
        if not soul_file.exists():
            soul_file.parent.mkdir(parents=True, exist_ok=True)
            soul_file.write_text(DEFAULT_SOUL)
        await store.upsert_channel(
            db, "general", "general", soul_path=soul_rel, cwd=str(general_dir),
            allowed_tools=["*", *MEMORY_TOOLS],
        )
        logger.info("Seeded #general channel (full toolset, scratch workspace)")

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

    # #spice: runs on an OpenAI-compatible provider (Featherless) instead of the
    # Claude SDK, with exactly one tool (`fetch`). Model + key come from env
    # (POIESIS_SPICE_MODEL / POIESIS_SPICE_API_KEY).
    if await store.get_channel(db, "spice") is None:
        spice_soul = "souls/spice.md"
        spice_file = Path(env.repo_root) / spice_soul
        if not spice_file.exists():
            spice_file.parent.mkdir(parents=True, exist_ok=True)
            spice_file.write_text(SPICE_SOUL)
        await store.upsert_channel(
            db, "spice", "spice", soul_path=spice_soul,
            model=(env.spice_model or None), allowed_tools=[], engine="openai",
        )
        logger.info("Seeded #spice channel (OpenAI-compatible, %s)", env.spice_base_url)

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
