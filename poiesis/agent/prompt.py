"""System-prompt assembly: channel soul + remembered facts + tool guidance + the clock."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_SOUL = (
    "You are Poiesis, a single-user self-hosted personal AI. You are direct, "
    "technical but not condescending, and you remember context across conversations."
)

TOOL_GUIDANCE = """\
## Operating notes
- Use `remember` to save durable facts the user shares about themselves; use `recall` if you need to look something up.
- Use `write_journal` to jot a short observation worth keeping.
- When the user asks you to change THIS app's own behavior, edit the code in your working
  directory (Read/Edit/Write), run any relevant checks with Bash, then call `request_deploy`
  with a one-line summary. A supervisor restarts and health-checks the app and automatically
  rolls back if the new code fails to boot — so make focused, correct changes.
- Don't narrate tool calls with filler; just do the work and summarize the result."""


def _current_time(tz: str) -> str:
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return datetime.now(zone).strftime("%A, %B %-d, %Y at %-I:%M %p %Z")


def build_system_prompt(
    soul: str | None,
    memories: list[dict[str, Any]],
    tz: str = "UTC",
    tool_guidance: str | None = TOOL_GUIDANCE,
    extra_context: str | None = None,
) -> str:
    parts: list[str] = [(soul or "").strip() or DEFAULT_SOUL]
    if memories:
        lines = "\n".join(f"- {m['content']}" for m in memories)
        parts.append(f"## What you remember about the user\n{lines}")
    if extra_context and extra_context.strip():
        parts.append(extra_context.strip())
    if tool_guidance:
        parts.append(tool_guidance)
    # Volatile, so it goes last (keeps the stable prefix cache-friendly).
    parts.append(
        f"## Right now\nIt is {_current_time(tz)}. Factor the time of day into your replies."
    )
    return "\n\n".join(parts)


def build_prompt(history: list[dict[str, Any]], user_message: str, max_turns: int = 12) -> str:
    """Fold recent transcript into the prompt (stateless; the DB is source of truth)."""
    recent = [m for m in history if m.get("content")][-max_turns:]
    if not recent:
        return user_message
    lines = []
    for m in recent:
        who = "User" if m["role"] == "user" else "You"
        lines.append(f"{who}: {m['content']}")
    transcript = "\n".join(lines)
    return (
        f"<conversation_so_far>\n{transcript}\n</conversation_so_far>\n\n"
        f"User's new message:\n{user_message}"
    )
