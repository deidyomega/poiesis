from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from glitch_core.schemas import AgentConfig, GlitchConfig, JournalEntry

logger = logging.getLogger(__name__)


class AgentDeps(BaseModel):
    """Dependencies injected into any agent at runtime."""
    agent_config: AgentConfig | None = None
    all_agents: list[AgentConfig] = []
    core_memories: list[dict[str, Any]] = []
    session_id: str = ""
    db: Any = None
    workspace: Any = None  # Workspace instance for free-form file ops
    safe_writer: Any = None  # SafeFileWriter for system zone ops
    ouroboros_enabled: bool = False

    model_config = {"arbitrary_types_allowed": True}


def create_chat_agent(agent_cfg: AgentConfig, is_router: bool = False) -> Agent[AgentDeps, str]:
    """Create a PydanticAI Agent for direct chat from an AgentConfig.

    If is_router=True, the agent gets dispatch tools (spawn_sub_agent).
    All agents get write_journal for memory.
    The system prompt is built dynamically from the agent's soul + core memories.
    """
    agent = Agent(
        agent_cfg.model,
        output_type=str,
        deps_type=AgentDeps,
        defer_model_check=True,
        end_strategy="exhaustive",
    )

    @agent.system_prompt
    async def build_system_prompt(ctx: RunContext[AgentDeps]) -> str:
        """Build the agent's system prompt from its soul + shared memories."""
        deps = ctx.deps
        parts: list[str] = []

        # Agent's soul (system_prompt from Firestore)
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

        # Router-specific: list available sub-agents for dispatch
        if is_router and deps.all_agents:
            # Only show agents this router can dispatch to (matching content rating)
            my_rating = deps.agent_config.content_rating if deps.agent_config else "sfw"
            dispatchable = [
                a for a in deps.all_agents
                if a.agent_id != (deps.agent_config.agent_id if deps.agent_config else "router")
                and a.enabled
                and (my_rating == "nsfw" or a.content_rating == "sfw")
            ]
            if dispatchable:
                parts.append("\n## Available Sub-Agents\n")
                parts.append("You can delegate tasks to these agents using the spawn_sub_agent tool:\n")
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
        parts.append("WRONG: 'Let me create that tool for you!' → create_tool(...)")
        parts.append("RIGHT: create_tool(...) → 'Done! The weather tool is now available.'")
        parts.append("If you write text before a tool call, the tool will NOT execute.")

        parts.append("\n## Journal Guidelines")
        parts.append("Use write_journal ONLY when the user reveals genuinely NEW information worth remembering:")
        parts.append("- New personal facts (name, job, relationships, preferences)")
        parts.append("- New project details or goals not already in your memories")
        parts.append("- Corrections to existing memories")
        parts.append("- Strong opinions or preferences stated clearly")
        parts.append("")
        parts.append("Do NOT journal:")
        parts.append("- Short acknowledgments ('love it', 'thanks', 'cool')")
        parts.append("- Information already in your Core Memories above")
        parts.append("- Rephrasing of things you already know")
        parts.append("- Conversational filler or small talk")

        return "\n".join(parts)

    # Router gets the dispatch tool
    if is_router:
        @agent.tool
        async def spawn_sub_agent(
            ctx: RunContext[AgentDeps],
            agent_id: str,
            prompt: str,
        ) -> str:
            """Delegate a task to a specialized sub-agent worker.

            The task is written to Firestore and picked up by a worker node.
            Results appear in the chat when the worker finishes.

            Args:
                agent_id: The sub-agent to delegate to (e.g., 'coder', 'researcher', 'sysadmin').
                prompt: The detailed task prompt for the sub-agent.
            """
            db = ctx.deps.db
            if db is None:
                return "Cannot dispatch — no database connection."

            # Find target agent config
            target_cfg = None
            for a in ctx.deps.all_agents:
                if a.agent_id == agent_id:
                    target_cfg = a
                    break

            if target_cfg is None:
                available = [a.agent_id for a in ctx.deps.all_agents if a.agent_id != "router" and a.enabled]
                return f"Unknown agent '{agent_id}'. Available: {available}"

            # Content rating enforcement: SFW router can't dispatch to NSFW agents
            my_rating = ctx.deps.agent_config.content_rating if ctx.deps.agent_config else "sfw"
            if target_cfg.content_rating == "nsfw" and my_rating == "sfw":
                return (
                    f"Cannot dispatch to '{agent_id}' — it handles NSFW content "
                    f"and this router is SFW. The user should chat with that agent directly."
                )

            from glitch_core.schemas import TaskAffinity, TaskCommand, TaskRouting

            routing = TaskRouting(
                command=TaskCommand.CUSTOM,
                agent_id=agent_id,
                model_tier=target_cfg.model_tier,
                affinity=TaskAffinity(target_cfg.affinity.value) if isinstance(target_cfg.affinity, str) else target_cfg.affinity,
                target_worker=None,
                required_capabilities=target_cfg.required_capabilities,
                fallback_agent=target_cfg.fallback_agent,
                fallback_window_seconds=target_cfg.fallback_window_seconds,
            )

            task_id = f"task_{uuid.uuid4().hex[:12]}"
            session_id = ctx.deps.session_id

            await (
                db.collection("sessions")
                .document(session_id)
                .collection("sub_tasks")
                .document(task_id)
                .set({
                    "task_id": task_id,
                    "session_id": session_id,
                    "prompt": prompt,
                    "routing": routing.model_dump(),
                    "status": "pending",
                    "content_rating": str(target_cfg.content_rating.value) if hasattr(target_cfg.content_rating, "value") else str(target_cfg.content_rating),
                    "priority": 0,
                    "created_at": datetime.utcnow(),
                    "claimed_by": None,
                    "claimed_at": None,
                    "started_at": None,
                    "completed_at": None,
                    "result": None,
                    "error": None,
                })
            )

            logger.info("Dispatched task %s to agent %s", task_id, agent_id)
            return f"Task dispatched to {target_cfg.name} agent (task_id={task_id}). The result will appear in chat when ready."

    # All agents get the journal tool
    @agent.tool
    async def write_journal(
        ctx: RunContext[AgentDeps],
        observation: str,
        topic: str | None = None,
        importance: float = 0.5,
    ) -> str:
        """Log an observation about the user or conversation to persistent memory.

        Call this whenever the user shares personal information, preferences,
        or anything worth remembering across conversations.

        Args:
            observation: What you noticed or want to remember (e.g. "User's name is Matt").
            topic: Optional topic category (e.g. "identity", "preference", "fact").
            importance: How important this is (0.0 to 1.0). Use 1.0 for identity/name, 0.5 for casual facts.
        """
        db = ctx.deps.db
        if db is None:
            return "Journal write skipped — no database connection."

        journal_id = f"j_{uuid.uuid4().hex[:12]}"
        entry = JournalEntry(
            journal_id=journal_id,
            session_id=ctx.deps.session_id,
            content=observation,
            topic=topic,
            importance=importance,
        )

        await db.collection("journals").document(journal_id).set(entry.model_dump())
        logger.info("Journal entry written: %s — %s", journal_id, observation[:80])
        return f"Successfully recorded to journal: {observation[:80]}"

    # Router gets workspace tools (free-form user zone)
    if is_router:
        @agent.tool
        async def workspace_write(
            ctx: RunContext[AgentDeps],
            path: str,
            content: str,
        ) -> str:
            """Write a file to the workspace (user's project zone).

            Use for scripts, websites, data files, configs — anything the user wants BUILT.
            The workspace is free-form and does not affect the running system.

            Args:
                path: Relative path within workspace (e.g. "scripts/hello.py").
                content: File content to write.
            """
            ws = ctx.deps.workspace
            if ws is None:
                return "Workspace not available."
            try:
                result = ws.write(path, content)
                return f"Written: {result.workspace_relative} ({result.size_bytes} bytes)"
            except PermissionError as e:
                return f"Blocked: {e}"

        @agent.tool
        async def workspace_read(ctx: RunContext[AgentDeps], path: str) -> str:
            """Read a file from the workspace.

            Args:
                path: Relative path within workspace.
            """
            ws = ctx.deps.workspace
            if ws is None:
                return "Workspace not available."
            try:
                return ws.read(path)
            except FileNotFoundError:
                return f"File not found: {path}"
            except PermissionError as e:
                return f"Blocked: {e}"

        @agent.tool
        async def workspace_list(ctx: RunContext[AgentDeps], path: str = ".") -> str:
            """List files in the workspace directory.

            Args:
                path: Directory to list (default: workspace root).
            """
            ws = ctx.deps.workspace
            if ws is None:
                return "Workspace not available."
            try:
                tree = ws.list(path)
                if not tree.files:
                    return "Empty directory."
                lines = []
                for f in tree.files:
                    icon = "📁" if f.is_dir else "📄"
                    size = f"{f.size_bytes}B" if not f.is_dir else ""
                    lines.append(f"{icon} {f.name} {size}")
                return "\n".join(lines)
            except PermissionError as e:
                return f"Blocked: {e}"

        @agent.tool
        async def workspace_run(
            ctx: RunContext[AgentDeps],
            script_path: str,
            args: list[str] | None = None,
            timeout: int = 300,
        ) -> str:
            """Run a Python script from the workspace.

            Args:
                script_path: Path to script relative to workspace root.
                args: Optional command-line arguments.
                timeout: Max execution time in seconds (default 300).
            """
            ws = ctx.deps.workspace
            if ws is None:
                return "Workspace not available."
            result = ws.run_script(script_path, args, timeout)
            parts = [f"Exit code: {result.exit_code}"]
            if result.timed_out:
                parts.append(f"TIMED OUT after {timeout}s")
            if result.stdout:
                parts.append(f"stdout:\n{result.stdout}")
            if result.stderr:
                parts.append(f"stderr:\n{result.stderr}")
            return "\n".join(parts)

        @agent.tool
        async def workspace_delete(ctx: RunContext[AgentDeps], path: str) -> str:
            """Delete a file or directory from the workspace.

            Args:
                path: Relative path within workspace.
            """
            ws = ctx.deps.workspace
            if ws is None:
                return "Workspace not available."
            try:
                deleted = ws.delete(path)
                return f"Deleted: {path}" if deleted else f"Not found: {path}"
            except PermissionError as e:
                return f"Blocked: {e}"

        @agent.tool
        async def create_tool(
            ctx: RunContext[AgentDeps],
            filename: str,
            code: str,
            description: str,
        ) -> str:
            """Create a new tool for the Glitch system (System zone).

            This goes through the full SafeFileWriter pipeline: syntax check,
            AST scan, subprocess import test, git commit, hot-reload.
            Use this when the user wants to extend Glitch's own capabilities.

            Args:
                filename: Tool filename (e.g. "weather_fetcher.py").
                code: The full Python source code for the tool module.
                description: What the tool does.
            """
            if not ctx.deps.ouroboros_enabled:
                return "Ouroboros is disabled. Enable it in System > Feature Flags."
            sw = ctx.deps.safe_writer
            if sw is None:
                return "SafeFileWriter not available."
            result = sw.write_tool(filename, code)
            if result.success:
                # Register in Firestore
                tool_id = filename.replace(".py", "")
                db = ctx.deps.db
                if db:
                    from glitch_core.schemas import ToolRegistration
                    reg = ToolRegistration(
                        tool_id=tool_id,
                        name=tool_id.replace("_", " ").title(),
                        description=description,
                        filename=filename if filename.endswith(".py") else f"{filename}.py",
                    )
                    await db.collection("tools").document(tool_id).set(reg.model_dump())
                return f"Tool '{filename}' created and hot-reloaded. Assign it to agents via /agents."
            else:
                errors = "\n".join(f"- {f.error}" for f in result.validation_failures) if result.validation_failures else result.error
                return f"Tool creation failed:\n{errors}"

        @agent.tool
        async def create_page(
            ctx: RunContext[AgentDeps],
            page_filename: str,
            page_code: str,
            template_filename: str,
            template_code: str,
        ) -> str:
            """Create a new web page for the Glitch UI (System zone).

            Generates a FastAPI route module + Jinja2 template, validates both,
            and hot-reloads into the running web server.

            Args:
                page_filename: Python module name (e.g. "my_page.py").
                page_code: Full Python source for the FastAPI route module.
                template_filename: Jinja2 template name (e.g. "my_page.html").
                template_code: Full HTML template source extending base.html.
            """
            if not ctx.deps.ouroboros_enabled:
                return "Ouroboros is disabled. Enable it in System > Feature Flags."
            sw = ctx.deps.safe_writer
            if sw is None:
                return "SafeFileWriter not available."
            result = sw.write_page(page_filename, page_code, template_filename, template_code)
            if result.success:
                return f"Page '{page_filename}' created and hot-reloaded. Check the nav."
            else:
                errors = "\n".join(f"- {f.error}" for f in result.validation_failures) if result.validation_failures else result.error
                return f"Page creation failed:\n{errors}"

    return agent
