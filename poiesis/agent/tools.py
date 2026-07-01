"""In-process SDK MCP tools: memory, journal, and self-mod deploy.

Built per-turn as closures so each tool captures the live db + channel context
without module-level globals (safe under concurrent channel turns).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from poiesis import gitops, store
from poiesis.config import POIESIS_HOME
from poiesis.db import Database

SERVER_NAME = "poiesis"

TASKS_FILE = POIESIS_HOME / "pm" / "task.md"

# Fully-qualified MCP tool names the SDK expects in allowed_tools.
MCP_TOOLS = [
    f"mcp__{SERVER_NAME}__remember",
    f"mcp__{SERVER_NAME}__recall",
    f"mcp__{SERVER_NAME}__write_journal",
    f"mcp__{SERVER_NAME}__read_tasks",
    f"mcp__{SERVER_NAME}__write_tasks",
    f"mcp__{SERVER_NAME}__request_deploy",
]


def _text(s: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": s}]}


def build_mcp_server(
    db: Database,
    channel_id: str,
    message_id: str | None,
    repo_root: str | Path,
):
    """Return an in-process MCP server config with context-bound tools."""

    @tool(
        "remember",
        "Save a durable fact about the user to long-term memory.",
        {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
    )
    async def remember(args: dict[str, Any]) -> dict[str, Any]:
        await store.add_memory(db, channel_id, args["content"])
        return _text("Saved to memory.")

    @tool(
        "recall",
        "Search long-term memory. Omit query to list everything remembered.",
        {"type": "object", "properties": {"query": {"type": "string"}}},
    )
    async def recall(args: dict[str, Any]) -> dict[str, Any]:
        q = (args or {}).get("query")
        rows = await (
            store.search_memories(db, channel_id, q) if q else store.list_memories(db, channel_id)
        )
        if not rows:
            return _text("(no memories)")
        return _text("\n".join(f"- {r['content']}" for r in rows))

    @tool(
        "write_journal",
        "Record a short observation worth keeping for later.",
        {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]},
    )
    async def write_journal(args: dict[str, Any]) -> dict[str, Any]:
        await store.add_journal(db, channel_id, args["note"])
        return _text("Journaled.")

    @tool(
        "read_tasks",
        "Read the current task list (markdown).",
        {"type": "object", "properties": {"note": {"type": "string"}}},
    )
    async def read_tasks(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _text(TASKS_FILE.read_text())
        except OSError:
            return _text("(no task list yet)")

    @tool(
        "write_tasks",
        "Replace the entire task list with new markdown content.",
        {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
    )
    async def write_tasks(args: dict[str, Any]) -> dict[str, Any]:
        TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        TASKS_FILE.write_text(args["content"])
        return _text("Task list updated.")

    @tool(
        "request_deploy",
        "Commit your code changes to this app and request a supervised restart + "
        "health-check (auto-rolls back to last-green if the new code won't boot). "
        "Call this after you've edited and verified a change to the app itself.",
        {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
    )
    async def request_deploy(args: dict[str, Any]) -> dict[str, Any]:
        summary = args["summary"]
        rollback = await store.get_setting(db, "last_green_sha") or gitops.current_sha(repo_root)
        target = gitops.commit_all(repo_root, f"self-mod: {summary}")
        did = await store.create_deploy(
            db,
            channel_id=channel_id,
            message_id=message_id,
            summary=summary,
            target_sha=target,
            rollback_sha=rollback,
            status="requested",
        )
        return _text(
            f"Deploy {did} queued (commit {str(target)[:8]}). The supervisor will restart "
            "and health-check; it rolls back to last-green automatically if boot fails."
        )

    return create_sdk_mcp_server(
        SERVER_NAME, "1.0.0",
        tools=[remember, recall, write_journal, read_tasks, write_tasks, request_deploy],
    )
