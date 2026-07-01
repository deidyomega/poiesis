"""Agent turn runner for OpenAI-compatible providers (e.g. Featherless, for #spice).

Mirrors `agent.core.run_turn`: an async generator yielding the same streaming events
(text / tool_call / tool_result / done) so the whole chat/SSE + segments UI works
unchanged. There's no Claude session here, so `session_id` is always None (no raw
transcript link). #spice gets exactly one tool: `fetch` (see spice_tools).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from openai import AsyncOpenAI

from poiesis import store
from poiesis.agent.prompt import build_system_prompt
from poiesis.agent.spice_tools import FETCH_TOOL_SCHEMA, run_fetch
from poiesis.config import PoiesisEnv
from poiesis.db import Database

logger = logging.getLogger(__name__)

SPICE_TOOL_GUIDANCE = """\
## Operating notes
- You have one tool, `fetch`: give it a URL and it GETs the page, turning JSON into
  readable markdown. Use it whenever the user points you at an API/endpoint or asks
  for data you'd need to look up over HTTP.
- You have no web search and no code/deploy tools — just chat and `fetch`.
- Don't narrate tool calls with filler; fetch what you need and summarize the result."""

_TOOL_RUNNERS: dict[str, Callable[[str], Any]] = {"fetch": run_fetch}


def _read_soul(repo_root: str | Path | None, soul_path: str | None) -> str | None:
    if not soul_path or not repo_root:
        return None
    try:
        return (Path(repo_root) / soul_path).read_text()
    except OSError:
        return None


def _summ(s: str, n: int = 300) -> str:
    return (s or "")[:n]


async def run_openai_turn(
    *,
    db: Database,
    channel: dict[str, Any],
    history: list[dict[str, Any]],
    user_message: str,
    message_id: str | None,
    repo_root: str | Path | None = None,
    should_cancel: Callable[[], bool] | None = None,
    max_turns: int = 8,
    tz: str = "UTC",
) -> AsyncIterator[dict[str, Any]]:
    env = PoiesisEnv()
    # Local OpenAI-compatible servers (Ollama) need no key, but the client still wants a
    # non-empty string — fall back to a harmless placeholder.
    api_key = env.spice_api_key or "ollama"
    base_url = channel.get("base_url") or env.spice_base_url
    model = channel.get("model") or env.spice_model

    if not model:
        yield {"type": "error", "message": "#spice has no model: set POIESIS_SPICE_MODEL in "
               "~/.poiesis/.env (or the channel's model).", "session_id": None}
        return

    soul = _read_soul(repo_root, channel.get("soul_path"))
    memories = await store.list_memories(db)
    system_prompt = build_system_prompt(soul, memories, tz=tz, tool_guidance=SPICE_TOOL_GUIDANCE)

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for m in history:
        if not m.get("content"):
            continue
        messages.append({"role": "assistant" if m["role"] == "agent" else "user",
                         "content": m["content"]})
    messages.append({"role": "user", "content": user_message})

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    segments: list[dict[str, Any]] = []
    full_text = ""
    cancelled = False

    try:
        for _round in range(max_turns):
            stream = await client.chat.completions.create(
                model=model, messages=messages, tools=[FETCH_TOOL_SCHEMA], stream=True,
            )
            text_seg: dict[str, Any] | None = None
            tool_acc: dict[int, dict[str, str]] = {}
            round_text = ""
            async for chunk in stream:
                if should_cancel and should_cancel():
                    cancelled = True
                    break
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    round_text += delta.content
                    full_text += delta.content
                    if text_seg is None:
                        text_seg = {"type": "text", "content": ""}
                        segments.append(text_seg)
                    text_seg["content"] += delta.content
                    yield {"type": "text", "delta": delta.content}
                for tc in (getattr(delta, "tool_calls", None) or []):
                    acc = tool_acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        acc["id"] = tc.id
                    if tc.function and tc.function.name:
                        acc["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        acc["args"] += tc.function.arguments
            if cancelled or not tool_acc:
                break  # final answer produced (or user stopped)

            # Emit the tool calls, record the assistant turn, execute, feed results back.
            assistant_tool_calls = []
            call_segs: dict[str, dict[str, Any]] = {}
            for idx in sorted(tool_acc):
                acc = tool_acc[idx]
                seg = {"type": "tool_call", "name": acc["name"],
                       "args_summary": _summ(acc["args"]), "result_summary": ""}
                segments.append(seg)
                call_segs[acc["id"]] = seg
                yield {"type": "tool_call", "name": acc["name"],
                       "args": seg["args_summary"], "id": acc["id"]}
                assistant_tool_calls.append({
                    "id": acc["id"], "type": "function",
                    "function": {"name": acc["name"], "arguments": acc["args"]},
                })
            messages.append({"role": "assistant", "content": round_text or None,
                             "tool_calls": assistant_tool_calls})
            text_seg = None  # any text after tools starts a fresh segment

            for idx in sorted(tool_acc):
                acc = tool_acc[idx]
                runner = _TOOL_RUNNERS.get(acc["name"])
                result = (await runner(acc["args"], env)) if runner \
                    else f"⚠️ unknown tool: {acc['name']}"
                seg = call_segs.get(acc["id"])
                if seg:
                    seg["result_summary"] = _summ(str(result))
                yield {"type": "tool_result", "name": acc["name"],
                       "id": acc["id"], "result": _summ(str(result))}
                messages.append({"role": "tool", "tool_call_id": acc["id"],
                                 "content": str(result)})
    except Exception as e:  # noqa: BLE001 — surface any provider/runtime failure to the UI
        logger.exception("#spice turn failed (model=%s)", model)
        yield {"type": "error", "message": f"{type(e).__name__}: {e}", "session_id": None}
        return

    segments = [s for s in segments
                if s["type"] == "tool_call" or (s.get("content") or "").strip()]
    yield {
        "type": "done",
        "content": full_text,
        "segments": segments,
        "usage": {},
        "cancelled": cancelled,
        "session_id": None,
    }
