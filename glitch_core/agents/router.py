from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from glitch_core.schemas import AgentConfig

logger = logging.getLogger(__name__)


class AgentDeps(BaseModel):
    """Dependencies injected into any agent at runtime."""
    agent_config: AgentConfig | None = None
    all_agents: list[AgentConfig] = []
    core_memories: list[dict[str, Any]] = []
    session_id: str = ""
    db: Any = None
    workspace: Any = None
    safe_writer: Any = None
    ouroboros_enabled: bool = False

    model_config = {"arbitrary_types_allowed": True}


def create_chat_agent(agent_cfg: AgentConfig) -> Agent[AgentDeps, str]:
    """Create a PydanticAI Agent for direct chat from an AgentConfig.

    Tools are attached based on the agent's `tools` list — no special-casing.
    The system prompt is built dynamically from the agent's soul + core memories.
    """
    from glitch_core.agents.builtin_tools import BUILTIN_TOOLS

    # Check if web_search is in the tools list — it's a model-native builtin, not a custom tool
    builtin_tools = []
    if "web_search" in (agent_cfg.tools or []):
        from pydantic_ai.capabilities.web_search import WebSearchTool
        builtin_tools.append(WebSearchTool())

    agent = Agent(
        agent_cfg.model,
        output_type=str,
        deps_type=AgentDeps,
        defer_model_check=True,
        end_strategy="early",
        builtin_tools=builtin_tools,
    )

    @agent.system_prompt
    async def build_system_prompt(ctx: RunContext[AgentDeps]) -> str:
        """Build the agent's system prompt from its soul + shared memories."""
        deps = ctx.deps
        parts: list[str] = []

        # Agent's soul
        if deps.agent_config and deps.agent_config.system_prompt:
            parts.append(deps.agent_config.system_prompt)

        # Shared core memories
        if deps.core_memories:
            parts.append("\n## Core Memories\n")
            for mem in deps.core_memories:
                category = mem.get("category", "other")
                content = mem.get("content", "")
                parts.append(f"- [{category}] {content}")
        else:
            parts.append("\n## Core Memories\nNo memories stored yet.")

        # If this agent has spawn_sub_agent, list available sub-agents
        tool_ids = deps.agent_config.tools if deps.agent_config else []
        if "spawn_sub_agent" in tool_ids and deps.all_agents:
            my_rating = deps.agent_config.content_rating if deps.agent_config else "sfw"
            my_rating_val = my_rating.value if hasattr(my_rating, "value") else str(my_rating)
            dispatchable = [
                a for a in deps.all_agents
                if a.agent_id != (deps.agent_config.agent_id if deps.agent_config else "")
                and a.enabled
                and (my_rating_val == "nsfw" or (a.content_rating.value if hasattr(a.content_rating, "value") else str(a.content_rating)) == "sfw")
            ]
            if dispatchable:
                parts.append("\n## Available Sub-Agents\n")
                parts.append("You can delegate tasks using the spawn_sub_agent tool:\n")
                for a in dispatchable:
                    triggers = ", ".join(a.triggers) if a.triggers else "manual"
                    parts.append(
                        f"- **{a.name}** (`{a.agent_id}`): {a.description}\n"
                        f"  Triggers: {triggers} | Model: {a.model} | "
                        f"Timeout: {a.timeout_seconds}s"
                    )

        # Tool execution rules
        parts.append("\n## CRITICAL: Tool Execution Rules")
        parts.append("When you need to use a tool, call it IMMEDIATELY. Do NOT write any text response before calling the tool.")
        parts.append("After the tool returns, THEN respond to the user with the result.")
        parts.append("If you write text before a tool call, the tool may not execute.")
        parts.append("")
        parts.append("CHAIN TOOL CALLS: If a task requires multiple steps (e.g. write a file then run it),")
        parts.append("call ALL the tools you need in sequence. Do not stop after one tool call.")
        parts.append("Example: user says 'write a script and run it' → call workspace_write, then workspace_run, then summarize both results.")
        parts.append("Complete the ENTIRE task before responding with text.")

        # Journal guidelines
        parts.append("\n## Journal Guidelines")
        parts.append("Use write_journal ONLY when the user reveals genuinely NEW information worth remembering:")
        parts.append("- New personal facts (name, job, relationships, preferences)")
        parts.append("- New project details or goals not already in your memories")
        parts.append("- Corrections to existing memories")
        parts.append("")
        parts.append("Do NOT journal:")
        parts.append("- Short acknowledgments ('love it', 'thanks', 'cool')")
        parts.append("- Information already in your Core Memories above")
        parts.append("- Conversational filler or small talk")

        return "\n".join(parts)

    # Attach tools based on the agent's tools list
    from glitch_core.agents import _attach_dynamic_tools

    builtin_ids = []
    dynamic_ids = []

    for tool_id in (agent_cfg.tools or []):
        if tool_id in BUILTIN_TOOLS:
            BUILTIN_TOOLS[tool_id](agent)
            builtin_ids.append(tool_id)
        else:
            dynamic_ids.append(tool_id)

    # Load dynamic tools from tools/ directory
    if dynamic_ids:
        _attach_dynamic_tools(agent, dynamic_ids)

    logger.debug("Attached tools for %s: builtin=%s dynamic=%s", agent_cfg.agent_id, builtin_ids, dynamic_ids)

    return agent
