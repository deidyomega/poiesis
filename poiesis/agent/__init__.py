"""Agent turn dispatch: route each channel to its engine.

`run_turn` keeps a single signature for the web/scheduler layers; the channel's
`engine` picks the backend — the Claude Agent SDK (default) or an OpenAI-compatible
provider (#spice on Featherless).
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from poiesis.agent.core import run_turn as run_claude_turn
from poiesis.agent.openai_turn import run_openai_turn

__all__ = ["run_turn", "run_claude_turn", "run_openai_turn"]


def run_turn(*, channel: dict[str, Any], **kwargs) -> AsyncIterator[dict[str, Any]]:
    engine = (channel.get("engine") or "claude").lower()
    if engine in ("openai", "featherless", "spice"):
        return run_openai_turn(channel=channel, **kwargs)
    return run_claude_turn(channel=channel, **kwargs)
