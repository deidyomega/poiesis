"""Builtin tool registry — system tools that need runtime deps.

Each function here takes a PydanticAI Agent and attaches a tool to it.
The agent's tools list in Firestore references these by ID:
  tools: ["write_journal", "spawn_sub_agent", "workspace_write", ...]
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Callable

from pydantic_ai import Agent, RunContext

logger = logging.getLogger(__name__)


def _attach_write_journal(agent: Agent) -> None:
    """Attach the write_journal tool to an agent."""

    @agent.tool
    async def write_journal(
        ctx: RunContext,
        observation: str,
        topic: str | None = None,
        importance: float = 0.5,
    ) -> str:
        """Log an observation about the user or conversation to persistent memory.

        Call this when the user reveals genuinely NEW information worth remembering.

        Args:
            observation: What you noticed (e.g. "User's name is Matt").
            topic: Optional category (e.g. "identity", "preference", "fact").
            importance: 0.0 to 1.0. Use 1.0 for identity/name, 0.5 for casual facts.
        """
        from glitch_core.schemas import JournalEntry

        db = ctx.deps.db
        if db is None:
            return "Journal write skipped — no database connection."

        # Capture the last ~5 messages for context — so compaction knows
        # WHY this observation was made, not just WHAT it says
        context_messages: list[str] = []
        try:
            session_id = ctx.deps.session_id
            if session_id and db:
                msgs_ref = (
                    db.collection("sessions")
                    .document(session_id)
                    .collection("messages")
                    .order_by("created_at", direction="DESCENDING")
                    .limit(5)
                )
                async for doc in msgs_ref.stream():
                    data = doc.to_dict()
                    role = data.get("role", "?")
                    content = data.get("content", "")
                    if content:
                        context_messages.append(f"[{role}] {content[:300]}")
                context_messages.reverse()  # chronological order
        except Exception:
            pass  # context is best-effort, don't fail the journal write

        journal_id = f"j_{uuid.uuid4().hex[:12]}"
        entry = JournalEntry(
            journal_id=journal_id,
            session_id=ctx.deps.session_id,
            content=observation,
            context_messages=context_messages,
            topic=topic,
            importance=importance,
        )

        await db.collection("journals").document(journal_id).set(entry.model_dump())
        logger.info("Journal entry written: %s — %s", journal_id, observation[:80])
        return f"Successfully recorded to journal: {observation[:80]}"


def _attach_spawn_sub_agent(agent: Agent) -> None:
    """Attach the spawn_sub_agent tool to an agent."""

    @agent.tool
    async def spawn_sub_agent(
        ctx: RunContext,
        agent_id: str,
        prompt: str,
    ) -> str:
        """Delegate a task to a specialized sub-agent worker.

        The task is written to Firestore and picked up by a worker node.
        Results appear in the chat when the worker finishes.

        Call list_agents first if you're unsure which agents are available.

        Args:
            agent_id: The sub-agent to delegate to. Use list_agents to discover available agents.
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
            available = [a.agent_id for a in ctx.deps.all_agents if a.enabled]
            return f"Unknown agent '{agent_id}'. Available: {available}"

        # Content rating enforcement
        from glitch_core.schemas import ContentRating
        my_rating = ctx.deps.agent_config.content_rating if ctx.deps.agent_config else "sfw"
        my_rating_val = my_rating.value if hasattr(my_rating, "value") else str(my_rating)
        target_rating_val = target_cfg.content_rating.value if hasattr(target_cfg.content_rating, "value") else str(target_cfg.content_rating)

        if target_rating_val == "nsfw" and my_rating_val == "sfw":
            return (
                f"Cannot dispatch to '{agent_id}' — it handles NSFW content "
                f"and this agent is SFW. The user should chat with that agent directly."
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
                "content_rating": target_rating_val,
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


def _attach_workspace_write(agent: Agent) -> None:
    @agent.tool
    async def workspace_write(ctx: RunContext, path: str, content: str) -> str:
        """Write a file to the workspace (user's project zone).

        Use for scripts, websites, data files — anything the user wants BUILT.
        The workspace does not affect the running system.

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


def _attach_workspace_read(agent: Agent) -> None:
    @agent.tool
    async def workspace_read(ctx: RunContext, path: str) -> str:
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


def _attach_workspace_list(agent: Agent) -> None:
    @agent.tool
    async def workspace_list(ctx: RunContext, path: str = ".") -> str:
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


def _attach_workspace_run(agent: Agent) -> None:
    @agent.tool
    async def workspace_run(
        ctx: RunContext,
        script_path: str,
        args: list[str] | None = None,
        timeout: int = 300,
        interpreter: str | None = None,
    ) -> str:
        """Run a script from the workspace.

        The interpreter is auto-detected from file extension (.py → python3,
        .js → node, .sh → bash, etc.) or can be specified explicitly.

        Args:
            script_path: Path to script relative to workspace root.
            args: Optional command-line arguments.
            timeout: Max execution time in seconds (default 300).
            interpreter: Override interpreter (e.g. "node", "bash"). Auto-detected from extension if not set.
        """
        ws = ctx.deps.workspace
        if ws is None:
            return "Workspace not available."
        result = ws.run_script(script_path, args, timeout, interpreter=interpreter)
        parts = [f"Exit code: {result.exit_code}"]
        if result.timed_out:
            parts.append(f"TIMED OUT after {timeout}s")
        if result.stdout:
            parts.append(f"stdout:\n{result.stdout}")
        if result.stderr:
            parts.append(f"stderr:\n{result.stderr}")
        return "\n".join(parts)


def _attach_workspace_delete(agent: Agent) -> None:
    @agent.tool
    async def workspace_delete(ctx: RunContext, path: str) -> str:
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


def _attach_create_tool(agent: Agent) -> None:
    @agent.tool
    async def create_tool(
        ctx: RunContext,
        filename: str,
        code: str,
        description: str,
    ) -> str:
        """Create a new tool for the Glitch system (System zone).

        Goes through the SafeFileWriter pipeline: syntax check, AST scan,
        subprocess import test, git commit, hot-reload.

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


def _attach_read_tool(agent: Agent) -> None:
    @agent.tool
    async def read_tool(ctx: RunContext, tool_name: str) -> str:
        """Read the source code of a custom tool.

        Args:
            tool_name: The tool name without extension (e.g. "weather_fetcher").
        """
        sw = ctx.deps.safe_writer
        if sw is None:
            return "SafeFileWriter not available."
        code = sw.read_tool(tool_name)
        if code is None:
            return f"Tool '{tool_name}' not found. Use list_tools to see available tools."
        return f"**{tool_name}.py:**\n```python\n{code}\n```"


def _attach_update_tool(agent: Agent) -> None:
    @agent.tool
    async def update_tool(ctx: RunContext, tool_name: str, code: str, description: str = "") -> str:
        """Update an existing custom tool. Read the current code first with read_tool.

        Goes through the same validation pipeline as create_tool.

        Args:
            tool_name: The tool name without extension (e.g. "weather_fetcher").
            code: The complete updated Python source code.
            description: Updated description (optional).
        """
        if not ctx.deps.ouroboros_enabled:
            return "Ouroboros is disabled. Enable it in System > Feature Flags."
        sw = ctx.deps.safe_writer
        if sw is None:
            return "SafeFileWriter not available."
        result = sw.write_tool(f"{tool_name}.py", code)
        if result.success:
            # Update Firestore registration if description changed
            if description:
                db = ctx.deps.db
                if db:
                    await db.collection("tools").document(tool_name).update({
                        "description": description,
                        "updated_at": datetime.utcnow(),
                    })
            return f"Tool '{tool_name}' updated and hot-reloaded."
        else:
            errors = "\n".join(f"- {f.error}" for f in result.validation_failures) if result.validation_failures else result.error
            return f"Tool update failed:\n{errors}"


def _attach_delete_tool(agent: Agent) -> None:
    @agent.tool
    async def delete_tool(ctx: RunContext, tool_name: str) -> str:
        """Delete a custom tool.

        Args:
            tool_name: The tool name without extension (e.g. "weather_fetcher").
        """
        if not ctx.deps.ouroboros_enabled:
            return "Ouroboros is disabled. Enable it in System > Feature Flags."
        sw = ctx.deps.safe_writer
        if sw is None:
            return "SafeFileWriter not available."
        result = sw.delete_tool(tool_name)
        if result.success:
            db = ctx.deps.db
            if db:
                await db.collection("tools").document(tool_name).delete()
            return f"Tool '{tool_name}' deleted."
        return f"Failed to delete tool: {result.error}"


def _attach_list_tools(agent: Agent) -> None:
    @agent.tool
    async def list_tools(ctx: RunContext) -> str:
        """List all custom tools that have been created.
        """
        sw = ctx.deps.safe_writer
        if sw is None:
            return "SafeFileWriter not available."
        tools = sw.list_tools()
        if not tools:
            return "No custom tools exist yet."
        lines = []
        for t in tools:
            lines.append(f"- **{t['name']}** ({t['size']} bytes)")
        return "Custom tools:\n" + "\n".join(lines)


def _attach_create_page(agent: Agent) -> None:
    @agent.tool
    async def create_page(
        ctx: RunContext,
        page_filename: str,
        page_code: str,
        template_filename: str,
        template_code: str,
    ) -> str:
        """Create a new web page for the Glitch UI (System zone).

        Generates a FastAPI route module + Jinja2 template, validates both,
        and hot-reloads into the running web server.

        The Python module MUST define:
        - router = APIRouter(prefix="/your_prefix")
        - PAGE_META = PageMeta(title="...", icon="🔧", nav_section="custom", nav_order=50, route_prefix="/your_prefix")
        The icon MUST be a single emoji (e.g. "🧪", "📊", "🎨"), NOT a Font Awesome class.

        IMPORTANT: Always set nav_section="custom" so the page appears under the Custom section in the sidebar.
        Import PageMeta from: from glitch_core.web.engine import PageMeta

        Route handlers MUST use this exact pattern:
            templates = request.app.state.templates
            return templates.TemplateResponse(request, "template_name.html", context={...})
        First arg is request, second is template name, third is optional context.
        WRONG: TemplateResponse("name.html", {"request": request})
        RIGHT: TemplateResponse(request, "name.html")
        NEVER use await — TemplateResponse is NOT a coroutine.
        NEVER create your own Jinja2Templates instance. NEVER import templates from elsewhere.

        Args:
            page_filename: Python module name (e.g. "mood_tracker.py").
            page_code: Full Python source for the FastAPI route module.
            template_filename: Jinja2 template name (e.g. "mood_tracker.html").
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


def _attach_read_page(agent: Agent) -> None:
    @agent.tool
    async def read_page(ctx: RunContext, page_name: str) -> str:
        """Read the current source code of a custom page.

        Returns the Python route code AND the Jinja2 template code.
        Use this to see what a page currently does before modifying it.

        Args:
            page_name: The page name without extension (e.g. "mood_tracker").
        """
        sw = ctx.deps.safe_writer
        if sw is None:
            return "SafeFileWriter not available."
        result = sw.read_page(page_name)
        if result is None:
            return f"Custom page '{page_name}' not found. Use list_pages to see available pages."
        parts = [f"**{page_name}.py:**\n```python\n{result['page_code']}\n```"]
        if result.get("template_code"):
            parts.append(f"\n**{page_name}.html:**\n```html\n{result['template_code']}\n```")
        return "\n".join(parts)


def _attach_update_page(agent: Agent) -> None:
    @agent.tool
    async def update_page(
        ctx: RunContext,
        page_name: str,
        page_code: str,
        template_code: str,
    ) -> str:
        """Update an existing custom page. Read the current code first with read_page.

        Goes through the same validation pipeline as create_page.
        The page hot-reloads immediately after a successful update.

        IMPORTANT: Always call read_page first to see the current code,
        then modify it and pass the COMPLETE updated code here.

        Args:
            page_name: The page name without extension (e.g. "mood_tracker").
            page_code: The complete updated Python route module source.
            template_code: The complete updated Jinja2 template source.
        """
        if not ctx.deps.ouroboros_enabled:
            return "Ouroboros is disabled. Enable it in System > Feature Flags."
        sw = ctx.deps.safe_writer
        if sw is None:
            return "SafeFileWriter not available."
        result = sw.write_page(f"{page_name}.py", page_code, f"{page_name}.html", template_code)
        if result.success:
            return f"Page '{page_name}' updated and hot-reloaded."
        else:
            errors = "\n".join(f"- {f.error}" for f in result.validation_failures) if result.validation_failures else result.error
            return f"Page update failed:\n{errors}"


def _attach_delete_page(agent: Agent) -> None:
    @agent.tool
    async def delete_page(ctx: RunContext, page_name: str) -> str:
        """Delete a custom page and its template.

        Args:
            page_name: The page name without extension (e.g. "mood_tracker").
        """
        if not ctx.deps.ouroboros_enabled:
            return "Ouroboros is disabled. Enable it in System > Feature Flags."
        sw = ctx.deps.safe_writer
        if sw is None:
            return "SafeFileWriter not available."
        result = sw.delete_page(page_name)
        if result.success:
            return f"Page '{page_name}' deleted."
        return f"Failed to delete page: {result.error}"


def _attach_list_pages(agent: Agent) -> None:
    @agent.tool
    async def list_pages(ctx: RunContext) -> str:
        """List all custom pages that have been created.
        """
        sw = ctx.deps.safe_writer
        if sw is None:
            return "SafeFileWriter not available."
        pages = sw.list_pages()
        if not pages:
            return "No custom pages exist yet."
        lines = []
        for p in pages:
            tmpl = "✓" if p["has_template"] else "✗"
            lines.append(f"- **{p['name']}** (template: {tmpl})")
        return "Custom pages:\n" + "\n".join(lines)


def _attach_read_soul(agent: Agent) -> None:
    @agent.tool
    async def read_soul(ctx: RunContext, agent_id: str) -> str:
        """Read an agent's soul (system prompt).

        Args:
            agent_id: The agent whose soul to read (e.g. "router", "coder", "researcher").
        """
        db = ctx.deps.db
        if db is None:
            return "No database connection."
        doc = await db.collection("agents").document(agent_id).get()
        if not doc.exists:
            available = [a.agent_id for a in ctx.deps.all_agents]
            return f"Agent '{agent_id}' not found. Available: {available}"
        data = doc.to_dict()
        soul = data.get("system_prompt", "")
        return f"**{data.get('name', agent_id)}** soul ({len(soul)} chars):\n\n{soul}" if soul else f"Agent '{agent_id}' has no soul configured."


def _attach_edit_soul(agent: Agent) -> None:
    @agent.tool
    async def edit_soul(ctx: RunContext, agent_id: str, new_soul: str) -> str:
        """Edit an agent's soul (system prompt). Automatically versions the previous soul.

        The agent will hot-reload with the new soul immediately — no restart needed.

        Args:
            agent_id: The agent whose soul to edit (e.g. "router", "coder", "researcher").
            new_soul: The complete new system prompt. This REPLACES the entire soul.
        """
        from datetime import datetime

        db = ctx.deps.db
        if db is None:
            return "No database connection."

        agent_ref = db.collection("agents").document(agent_id)
        doc = await agent_ref.get()
        if not doc.exists:
            available = [a.agent_id for a in ctx.deps.all_agents]
            return f"Agent '{agent_id}' not found. Available: {available}"

        data = doc.to_dict()
        old_soul = data.get("system_prompt", "")

        # Snapshot previous soul to version history
        if old_soul:
            # Count existing versions
            version = 1
            async for _ in agent_ref.collection("soul_history").stream():
                version += 1

            await agent_ref.collection("soul_history").document(f"v{version}").set({
                "version": version,
                "content": old_soul,
                "saved_at": datetime.utcnow(),
                "edited_by": ctx.deps.agent_config.agent_id if ctx.deps.agent_config else "unknown",
            })

        # Update the soul — on_snapshot will hot-reload the agent
        await agent_ref.update({
            "system_prompt": new_soul,
            "updated_at": datetime.utcnow(),
        })

        return f"Soul updated for '{agent_id}'. Previous version saved as v{version if old_soul else 0}. The agent will use the new soul immediately."


def _attach_set_reminder(agent: Agent) -> None:
    @agent.tool
    async def set_reminder(
        ctx: RunContext,
        message: str,
        minutes: float,
    ) -> str:
        """Set a reminder that will appear in the chat after a delay.

        ONLY call this when the user EXPLICITLY asks for a reminder.
        Do NOT create reminders unless the user says "remind me" or similar.
        Do NOT create duplicate reminders — if one is already set, tell the user.

        The reminder message is pre-composed NOW and delivered later.
        Write it in a friendly, natural tone as if you're reminding the user.

        Args:
            message: The reminder text the user will see (e.g. "Hey! Time to check the laundry!")
            minutes: How many minutes from now to fire the reminder.
        """
        from glitch_core.schemas import Reminder

        db = ctx.deps.db
        if db is None:
            return "Cannot set reminder — no database connection."

        if minutes <= 0:
            return "Reminder must be in the future."
        if minutes > 10080:  # 7 days
            return "Reminder too far out — max 7 days (10080 minutes)."

        reminder_id = f"rem_{uuid.uuid4().hex[:12]}"
        fire_at = datetime.utcnow() + __import__("datetime").timedelta(minutes=minutes)

        reminder = Reminder(
            reminder_id=reminder_id,
            session_id=ctx.deps.session_id,
            agent_id=ctx.deps.agent_config.agent_id if ctx.deps.agent_config else "",
            message=message,
            fire_at=fire_at,
        )

        await db.collection("reminders").document(reminder_id).set(reminder.model_dump())

        # Format for display
        if minutes < 60:
            time_str = f"{int(minutes)} minute{'s' if minutes != 1 else ''}"
        elif minutes < 1440:
            hours = minutes / 60
            time_str = f"{hours:.1f} hour{'s' if hours != 1 else ''}"
        else:
            days = minutes / 1440
            time_str = f"{days:.1f} day{'s' if days != 1 else ''}"

        return f"Reminder set for {time_str} from now (ID: {reminder_id})"


def _attach_list_reminders(agent: Agent) -> None:
    @agent.tool
    async def list_reminders(ctx: RunContext) -> str:
        """List all pending (unfired) reminders for this session.
        """
        db = ctx.deps.db
        if db is None:
            return "No database connection."

        from google.cloud.firestore_v1.base_query import FieldFilter

        reminders = []
        query = (
            db.collection("reminders")
            .where(filter=FieldFilter("fired", "==", False))
            .where(filter=FieldFilter("session_id", "==", ctx.deps.session_id))
            .limit(20)
        )
        async for doc in query.stream():
            data = doc.to_dict()
            fire_at = data.get("fire_at", "?")
            message = data.get("message", "")[:80]
            reminders.append(f"- {doc.id}: \"{message}\" (fires at {fire_at})")

        if not reminders:
            return "No pending reminders."
        return f"Pending reminders:\n" + "\n".join(reminders)


def _attach_cancel_reminder(agent: Agent) -> None:
    @agent.tool
    async def cancel_reminder(ctx: RunContext, reminder_id: str) -> str:
        """Cancel a pending reminder.

        Args:
            reminder_id: The reminder ID to cancel.
        """
        db = ctx.deps.db
        if db is None:
            return "No database connection."

        doc = await db.collection("reminders").document(reminder_id).get()
        if not doc.exists:
            return f"Reminder '{reminder_id}' not found."

        data = doc.to_dict()
        if data.get("fired"):
            return f"Reminder '{reminder_id}' already fired."

        await db.collection("reminders").document(reminder_id).delete()
        return f"Reminder '{reminder_id}' cancelled."


# ── Agent Management Tools ────────────────────────────────────────────────


def _attach_list_agents(agent: Agent) -> None:
    @agent.tool
    async def list_agents(ctx: RunContext) -> str:
        """List all available agents with their ID, name, description, model, and status.

        Use this to discover which agents exist before spawning sub-agents
        or when the user asks about available agents.
        """
        db = ctx.deps.db
        if db is None:
            return "No database connection."
        agents: list[dict] = []
        async for doc in db.collection("agents").stream():
            if doc.id == "_placeholder":
                continue
            data = doc.to_dict()
            agents.append({
                "agent_id": doc.id,
                "name": data.get("name", doc.id),
                "description": data.get("description", ""),
                "model": data.get("model", "unknown"),
                "content_rating": data.get("content_rating", "sfw"),
                "enabled": data.get("enabled", True),
                "tools": len(data.get("tools", [])),
            })
        if not agents:
            return "No agents found."
        lines = []
        for a in sorted(agents, key=lambda x: x["agent_id"]):
            status = "✅" if a["enabled"] else "⛔"
            lines.append(
                f"{status} **{a['name']}** (`{a['agent_id']}`) — {a['description']}\n"
                f"   Model: {a['model']} | Rating: {a['content_rating']} | Tools: {a['tools']}"
            )
        return "\n\n".join(lines)


def _attach_read_agent(agent: Agent) -> None:
    @agent.tool
    async def read_agent(ctx: RunContext, agent_id: str) -> str:
        """Read the full configuration of a specific agent.

        Returns all fields including system_prompt (soul), tools, model, etc.

        Args:
            agent_id: The agent to read (e.g. "router", "coder", "researcher").
        """
        db = ctx.deps.db
        if db is None:
            return "No database connection."
        doc = await db.collection("agents").document(agent_id).get()
        if not doc.exists:
            available = [a.agent_id for a in ctx.deps.all_agents]
            return f"Agent '{agent_id}' not found. Available: {available}"
        data = doc.to_dict()
        parts = [
            f"**Agent: {data.get('name', agent_id)}** (`{agent_id}`)",
            f"**Description:** {data.get('description', 'none')}",
            f"**Model:** {data.get('model', 'unknown')}",
            f"**Model Tier:** {data.get('model_tier', 'fast')}",
            f"**Content Rating:** {data.get('content_rating', 'sfw')}",
            f"**Enabled:** {data.get('enabled', True)}",
            f"**Tools:** {', '.join(data.get('tools', [])) or 'none'}",
            f"**Timeout:** {data.get('timeout_seconds', 120)}s",
            f"\n**System Prompt (Soul):**\n{data.get('system_prompt', '(empty)')}",
        ]
        return "\n".join(parts)


def _attach_create_agent(agent: Agent) -> None:
    @agent.tool
    async def create_agent(
        ctx: RunContext,
        agent_id: str,
        name: str,
        description: str,
        system_prompt: str,
        content_rating: str = "sfw",
        tools: list[str] | None = None,
    ) -> str:
        """Create a new agent that will be immediately available for use.

        The agent hot-reloads into the system — no restart needed.
        It inherits the same model as the router agent.

        Think carefully about the system_prompt — it's the agent's soul.
        Write a rich personality and clear capability description.

        Args:
            agent_id: Unique ID, lowercase with underscores (e.g. "marketing_buddy").
            name: Human-readable name (e.g. "Marketing Buddy").
            description: Short description of what this agent does (1-2 sentences).
            system_prompt: The agent's full system prompt / soul. Be detailed and specific.
            content_rating: "sfw" or "nsfw" (default "sfw").
            tools: Optional list of tool IDs to give the agent. Defaults to basic tools
                   (web_search, write_journal, workspace_read, workspace_list).
                   Use list_tools to see available custom tools.
        """
        db = ctx.deps.db
        if db is None:
            return "No database connection."

        # Validate agent_id format
        if not agent_id.replace("_", "").isalnum() or agent_id != agent_id.lower():
            return "agent_id must be lowercase alphanumeric with underscores only."

        # Check for duplicates
        existing = await db.collection("agents").document(agent_id).get()
        if existing.exists:
            return f"Agent '{agent_id}' already exists. Use update_agent to modify it."

        # Inherit model from the calling agent (router)
        model = ctx.deps.agent_config.model if ctx.deps.agent_config else "anthropic:claude-sonnet-4-20250514"

        # Default tools for new agents
        if tools is None:
            tools = ["web_search", "write_journal", "workspace_read", "workspace_list"]

        if content_rating not in ("sfw", "nsfw"):
            return "content_rating must be 'sfw' or 'nsfw'."

        await db.collection("agents").document(agent_id).set({
            "agent_id": agent_id,
            "name": name,
            "description": description,
            "model": model,
            "system_prompt": system_prompt,
            "model_tier": "fast",
            "output_type": "text",
            "tools": tools,
            "timeout_seconds": 120,
            "affinity": "any",
            "required_capabilities": [],
            "content_rating": content_rating,
            "enabled": True,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        })

        logger.info("Agent created via tool: %s", agent_id)
        return (
            f"Agent '{name}' (`{agent_id}`) created and hot-loaded.\n"
            f"Model: {model} | Tools: {', '.join(tools)} | Rating: {content_rating}\n"
            f"It's now available for spawn_sub_agent or direct chat."
        )


def _attach_update_agent(agent: Agent) -> None:
    @agent.tool
    async def update_agent(
        ctx: RunContext,
        agent_id: str,
        name: str | None = None,
        description: str | None = None,
        system_prompt: str | None = None,
        tools: list[str] | None = None,
        content_rating: str | None = None,
        enabled: bool | None = None,
    ) -> str:
        """Update an existing agent's configuration. Only the fields you provide are changed.

        Use read_agent first to see the current config, then update specific fields.
        Changes take effect immediately via hot-reload.

        If updating the system_prompt (soul), prefer edit_soul instead — it versions the old one.

        Args:
            agent_id: The agent to update.
            name: New display name (optional).
            description: New description (optional).
            system_prompt: New system prompt / soul (optional — prefer edit_soul for versioning).
            tools: New tool list — replaces the entire list (optional).
            content_rating: "sfw" or "nsfw" (optional).
            enabled: True/False to enable/disable (optional).
        """
        db = ctx.deps.db
        if db is None:
            return "No database connection."

        doc = await db.collection("agents").document(agent_id).get()
        if not doc.exists:
            available = [a.agent_id for a in ctx.deps.all_agents]
            return f"Agent '{agent_id}' not found. Available: {available}"

        updates: dict[str, Any] = {"updated_at": datetime.utcnow()}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        if system_prompt is not None:
            updates["system_prompt"] = system_prompt
        if tools is not None:
            updates["tools"] = tools
        if content_rating is not None:
            if content_rating not in ("sfw", "nsfw"):
                return "content_rating must be 'sfw' or 'nsfw'."
            updates["content_rating"] = content_rating
        if enabled is not None:
            updates["enabled"] = enabled

        if len(updates) <= 1:  # only updated_at
            return "No changes specified."

        await db.collection("agents").document(agent_id).update(updates)
        changed = [k for k in updates if k != "updated_at"]
        logger.info("Agent updated via tool: %s (fields: %s)", agent_id, changed)
        return f"Agent '{agent_id}' updated: {', '.join(changed)}. Changes are live."


def _attach_delete_agent(agent: Agent) -> None:
    @agent.tool
    async def delete_agent(ctx: RunContext, agent_id: str) -> str:
        """Delete an agent permanently.

        Cannot delete the default agent or the agent you're currently running as.

        Args:
            agent_id: The agent to delete.
        """
        db = ctx.deps.db
        if db is None:
            return "No database connection."

        # Prevent self-deletion
        my_id = ctx.deps.agent_config.agent_id if ctx.deps.agent_config else None
        if agent_id == my_id:
            return f"Cannot delete yourself ('{agent_id}'). That's not allowed."

        # Prevent deleting the default agent
        from glitch_core.config import get_default_agent_id
        default_id = await get_default_agent_id(db)
        if agent_id == default_id:
            return f"Cannot delete the default agent ('{default_id}'). Change the default first."

        doc = await db.collection("agents").document(agent_id).get()
        if not doc.exists:
            return f"Agent '{agent_id}' not found."

        agent_name = doc.to_dict().get("name", agent_id)
        await db.collection("agents").document(agent_id).delete()
        logger.info("Agent deleted via tool: %s", agent_id)
        return f"Agent '{agent_name}' (`{agent_id}`) has been deleted."


# ── Registry ───────────────────────────────────────────────────────────────

# web_search is a model-native builtin handled in create_chat_agent, not via _attach.
# It's in the registry so the UI shows it as a checkbox option.
def _noop_attach(agent: Agent) -> None:
    pass  # Handled by create_chat_agent via WebSearchTool


BUILTIN_TOOLS: dict[str, Callable[[Agent], None]] = {
    "web_search": _noop_attach,
    "write_journal": _attach_write_journal,
    "spawn_sub_agent": _attach_spawn_sub_agent,
    "workspace_write": _attach_workspace_write,
    "workspace_read": _attach_workspace_read,
    "workspace_list": _attach_workspace_list,
    "workspace_run": _attach_workspace_run,
    "workspace_delete": _attach_workspace_delete,
    "create_tool": _attach_create_tool,
    "read_tool": _attach_read_tool,
    "update_tool": _attach_update_tool,
    "delete_tool": _attach_delete_tool,
    "list_tools": _attach_list_tools,
    "create_page": _attach_create_page,
    "read_page": _attach_read_page,
    "update_page": _attach_update_page,
    "delete_page": _attach_delete_page,
    "list_pages": _attach_list_pages,
    "read_soul": _attach_read_soul,
    "edit_soul": _attach_edit_soul,
    "set_reminder": _attach_set_reminder,
    "list_reminders": _attach_list_reminders,
    "cancel_reminder": _attach_cancel_reminder,
    "list_agents": _attach_list_agents,
    "read_agent": _attach_read_agent,
    "create_agent": _attach_create_agent,
    "update_agent": _attach_update_agent,
    "delete_agent": _attach_delete_agent,
}

# ── Tool Groups ────────────────────────────────────────────────────────────
# UI presents these as toggles. Each group expands to individual tool IDs.

TOOL_GROUPS: dict[str, dict] = {
    "manage_pages": {
        "label": "Manage Pages",
        "description": "Create, read, update, delete custom web pages",
        "tools": ["create_page", "read_page", "update_page", "delete_page", "list_pages"],
    },
    "manage_workspace": {
        "label": "Manage Workspace",
        "description": "Read, write, run, and delete files in the workspace",
        "tools": ["workspace_write", "workspace_read", "workspace_list", "workspace_run", "workspace_delete"],
    },
    "manage_tools": {
        "label": "Manage Custom Tools",
        "description": "Create, read, update, delete custom tools (Ouroboros-generated)",
        "tools": ["create_tool", "read_tool", "update_tool", "delete_tool", "list_tools"],
    },
    "manage_souls": {
        "label": "Manage Souls",
        "description": "Read and edit agent personalities",
        "tools": ["read_soul", "edit_soul"],
    },
    "manage_reminders": {
        "label": "Manage Reminders",
        "description": "Set, list, and cancel reminders",
        "tools": ["set_reminder", "list_reminders", "cancel_reminder"],
    },
    "web_search": {
        "label": "Web Search",
        "description": "Search the web for information (model-native)",
        "tools": ["web_search"],
    },
    "write_journal": {
        "label": "Write Journal",
        "description": "Log observations to persistent memory",
        "tools": ["write_journal"],
    },
    "spawn_sub_agent": {
        "label": "Spawn Sub-Agent",
        "description": "Delegate tasks to other agents",
        "tools": ["spawn_sub_agent"],
    },
    "manage_agents": {
        "label": "Manage Agents",
        "description": "Create, read, update, delete agents at runtime",
        "tools": ["list_agents", "read_agent", "create_agent", "update_agent", "delete_agent"],
    },
}


def groups_to_tools(group_ids: list[str]) -> list[str]:
    """Expand group IDs to individual tool IDs."""
    tools: list[str] = []
    for gid in group_ids:
        if gid in TOOL_GROUPS:
            tools.extend(TOOL_GROUPS[gid]["tools"])
        else:
            tools.append(gid)  # Pass through unknown IDs (dynamic tools)
    return tools


def tools_to_groups(tool_ids: list[str]) -> list[str]:
    """Determine which groups are fully enabled from a list of tool IDs."""
    tool_set = set(tool_ids)
    groups: list[str] = []
    for gid, group in TOOL_GROUPS.items():
        if set(group["tools"]).issubset(tool_set):
            groups.append(gid)
    return groups


# Default tools for the initial router agent (seeded during bootstrap)
DEFAULT_ROUTER_TOOLS = [
    "web_search",
    "write_journal",
    "spawn_sub_agent",
    "workspace_write",
    "workspace_read",
    "workspace_list",
    "workspace_run",
    "workspace_delete",
    "create_tool",
    "read_tool",
    "update_tool",
    "delete_tool",
    "list_tools",
    "create_page",
    "read_page",
    "update_page",
    "delete_page",
    "list_pages",
    "read_soul",
    "edit_soul",
    "set_reminder",
    "list_reminders",
    "cancel_reminder",
    "list_agents",
    "read_agent",
    "create_agent",
    "update_agent",
    "delete_agent",
]
