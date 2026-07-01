"""Agent turn runner over the Claude Agent SDK.

`run_turn` is an async generator that yields streaming events for the chat/SSE
layer and, at the end, the final ordered segments + usage. Token-level deltas are
derived from the SDK's StreamEvents (the Anthropic event protocol).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    UserMessage,
    query,
)

from poiesis import store
from poiesis.agent.prompt import build_prompt, build_system_prompt
from poiesis.agent.tools import MCP_TOOLS, SERVER_NAME, build_mcp_server
from poiesis.db import Database

logger = logging.getLogger(__name__)

CODING_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "TodoWrite"]


def _clean_tool_name(name: str) -> str:
    prefix = f"mcp__{SERVER_NAME}__"
    return name[len(prefix):] if name.startswith(prefix) else name


def _read_soul(repo_root: str | Path, soul_path: str | None) -> str | None:
    if not soul_path:
        return None
    try:
        return (Path(repo_root) / soul_path).read_text()
    except OSError:
        return None


async def run_turn(
    *,
    db: Database,
    channel: dict[str, Any],
    history: list[dict[str, Any]],
    user_message: str,
    message_id: str | None,
    repo_root: str | Path,
    should_cancel: Callable[[], bool] | None = None,
    max_turns: int = 40,
    tz: str = "UTC",
) -> AsyncIterator[dict[str, Any]]:
    soul = _read_soul(repo_root, channel.get("soul_path"))
    memories = await store.list_memories(db)
    system_prompt = build_system_prompt(soul, memories, tz=tz)
    server = build_mcp_server(db, channel["id"], message_id, repo_root)

    if channel.get("allowed_tools"):
        allowed = json.loads(channel["allowed_tools"])
    else:
        allowed = (CODING_TOOLS + MCP_TOOLS) if channel.get("cwd") else list(MCP_TOOLS)

    # The SDK loads the bundled CLI's FULL default toolset (WebFetch, WebSearch,
    # Task subagents, Cron, ToolSearch, Skill, …) whenever `tools` is left None —
    # and bypassPermissions means `allowed_tools` won't gate them, so the model can
    # call anything. Worse, with that many tools the CLI *defers* our own MCP tools
    # behind ToolSearch, adding a round-trip that has been landing blank turns.
    # Pin the built-in capability set to exactly the non-MCP tools we grant
    # (empty list ⇒ MCP-only), and drop skills/subagents entirely.
    builtin_tools = [t for t in allowed if not t.startswith("mcp__")]

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={SERVER_NAME: server},
        tools=builtin_tools,
        allowed_tools=allowed,
        skills=None,
        agents={},
        permission_mode="bypassPermissions",
        cwd=str(channel.get("cwd") or repo_root),
        include_partial_messages=True,
        setting_sources=[],
        model=channel.get("model") or None,
        max_turns=max_turns,
    )
    prompt = build_prompt(history, user_message)

    segments: list[dict[str, Any]] = []
    open_blocks: dict[int, dict[str, Any]] = {}
    argbuf: dict[int, str] = {}
    tool_by_id: dict[str, dict[str, Any]] = {}
    content = ""
    usage: dict[str, Any] = {}
    cancelled = False
    session_id: str | None = None

    try:
        async for msg in query(prompt=prompt, options=options):
            if should_cancel and should_cancel():
                cancelled = True
                break

            if isinstance(msg, StreamEvent):
                e = msg.event or {}
                et = e.get("type")
                if et == "content_block_start":
                    idx = e.get("index")
                    cb = e.get("content_block", {}) or {}
                    ct = cb.get("type")
                    if ct == "text":
                        seg = {"type": "text", "content": ""}
                        segments.append(seg)
                        open_blocks[idx] = seg
                    elif ct == "thinking":
                        seg = {"type": "thinking", "content": ""}
                        segments.append(seg)
                        open_blocks[idx] = seg
                    elif ct == "tool_use":
                        seg = {
                            "type": "tool_call",
                            "name": _clean_tool_name(cb.get("name", "?")),
                            "args_summary": "",
                            "result_summary": "",
                        }
                        segments.append(seg)
                        open_blocks[idx] = seg
                        argbuf[idx] = ""
                        if cb.get("id"):
                            tool_by_id[cb["id"]] = seg
                elif et == "content_block_delta":
                    idx = e.get("index")
                    d = e.get("delta", {}) or {}
                    dt = d.get("type")
                    seg = open_blocks.get(idx)
                    if dt == "text_delta":
                        txt = d.get("text", "")
                        content += txt
                        if seg:
                            seg["content"] += txt
                        yield {"type": "text", "delta": txt}
                    elif dt == "thinking_delta":
                        th = d.get("thinking", "")
                        if seg:
                            seg["content"] += th
                        yield {"type": "thinking", "delta": th}
                    elif dt == "input_json_delta":
                        argbuf[idx] = argbuf.get(idx, "") + d.get("partial_json", "")
                elif et == "content_block_stop":
                    idx = e.get("index")
                    seg = open_blocks.get(idx)
                    if seg and seg["type"] == "tool_call":
                        seg["args_summary"] = argbuf.get(idx, "")[:300]
                        yield {"type": "tool_call", "name": seg["name"], "args": seg["args_summary"]}

            elif isinstance(msg, UserMessage):
                blocks = msg.content if isinstance(msg.content, list) else []
                for b in blocks:
                    if isinstance(b, ToolResultBlock):
                        seg = tool_by_id.get(b.tool_use_id)
                        res = b.content
                        if isinstance(res, list):
                            res = " ".join(
                                str(x.get("text", "")) if isinstance(x, dict) else str(x)
                                for x in res
                            )
                        res = str(res).strip()[:300]
                        if seg:
                            seg["result_summary"] = res
                        yield {"type": "tool_result", "name": seg["name"] if seg else "?"}

            elif isinstance(msg, ResultMessage):
                u = getattr(msg, "usage", None)
                if isinstance(u, dict):
                    usage = u
                session_id = getattr(msg, "session_id", None) or session_id

    except Exception as e:  # noqa: BLE001 — surface any SDK/runtime failure to the UI
        logger.exception("agent turn failed (session=%s)", session_id)
        yield {"type": "error", "message": f"{type(e).__name__}: {e}", "session_id": session_id}
        return

    # Drop empty text/thinking segments — the SDK emits redacted (text-less)
    # thinking blocks, and we don't want blank "Thinking…" boxes. Tool calls stay.
    segments = [
        s for s in segments
        if s["type"] == "tool_call" or (s.get("content") or "").strip()
    ]

    yield {
        "type": "done",
        "content": content,
        "segments": segments,
        "usage": usage,
        "cancelled": cancelled,
        "session_id": session_id,
    }
