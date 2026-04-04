from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from typing import Any

import uvicorn

from google.cloud.firestore_v1 import Client as SyncClient
from google.oauth2 import service_account

from glitch_core.agents.router import AgentDeps, create_chat_agent
from glitch_core.config import DEFAULT_AGENT_ID, GlitchEnv, get_default_agent_id, get_firestore_client, load_yaml_config
from glitch_core.schemas import AgentConfig
from glitch_core.web.app import create_app

logger = logging.getLogger(__name__)


def _export_api_keys(env: GlitchEnv) -> None:
    """Export GLITCH_ API keys as the env vars PydanticAI expects."""
    key_map = {
        "ANTHROPIC_API_KEY": env.anthropic_api_key,
        "GEMINI_API_KEY": env.gemini_api_key,
        "OPENAI_API_KEY": env.openai_api_key,
        "MISTRAL_API_KEY": env.mistral_api_key,
        "GROQ_API_KEY": env.groq_api_key,
        "OLLAMA_BASE_URL": f"{env.ollama_host.rstrip('/')}/v1" if env.ollama_host else None,
    }
    for var, val in key_map.items():
        if val and var not in os.environ:
            os.environ[var] = val


class GlitchDaemon:
    """The main Glitch Core process running on the primary node."""

    def __init__(self, env: GlitchEnv | None = None) -> None:
        self.env = env or GlitchEnv()
        _export_api_keys(self.env)
        self.db = get_firestore_client(self.env)
        self.config = load_yaml_config()
        self._running = False
        self.agent_configs: list[AgentConfig] = []
        self.agent_registry: dict[str, Any] = {}

        # Chat agents keyed by agent_id — created dynamically from Firestore configs
        self._chat_agents: dict[str, Any] = {}
        self._default_agent_id: str = DEFAULT_AGENT_ID

        # Cached shared context
        self._memories_cache: list[dict[str, Any]] = []
        self._ouroboros_enabled: bool = False

        # Ouroboros components
        from glitch_core.ouroboros import SafeFileWriter, RuntimeCircuitBreaker, Workspace
        self.workspace = Workspace()
        self.safe_writer = SafeFileWriter()
        self.circuit_breaker = RuntimeCircuitBreaker(self.safe_writer)

        # Sync Firestore client for on_snapshot (runs in background threads)
        creds = service_account.Credentials.from_service_account_file(
            str(self.env.firebase_credentials)
        )
        self.sync_db = SyncClient(project=self.env.firebase_project, credentials=creds)

    async def start(self) -> None:
        """Start all daemon tasks concurrently."""
        self._running = True
        logger.info("Starting Glitch daemon on node: %s", self.env.node_name)

        # Load project meta
        self._default_agent_id = await get_default_agent_id(self.db)
        logger.info("Default agent: %s", self._default_agent_id)

        # Load feature flags
        try:
            meta_doc = await self.db.collection("meta").document("project").get()
            if meta_doc.exists:
                flags = meta_doc.to_dict().get("feature_flags", {})
                self._ouroboros_enabled = flags.get("ouroboros_enabled", False)
        except Exception:
            pass
        logger.info("Ouroboros: %s", "enabled" if self._ouroboros_enabled else "disabled")

        # Load agent configs from Firestore (source of truth)
        from glitch_core.agents import build_agent_registry, load_agents_from_firestore
        self.agent_configs = await load_agents_from_firestore(self.db)
        if self.agent_configs:
            logger.info("Loaded %d agents from Firestore", len(self.agent_configs))
            self.config.agents = [a for a in self.agent_configs if a.agent_id != self._default_agent_id]
        else:
            logger.warning("No agents in Firestore — using YAML config as fallback")

        # Build worker agent registry (for sub-agent task execution)
        worker_configs = [a for a in self.agent_configs if a.agent_id != self._default_agent_id]
        self.agent_registry = build_agent_registry(worker_configs, self.env)

        # Build chat agents for direct conversation (router + any agent)
        self._build_chat_agents()

        # Watch /agents/ for config changes — hot-rebuild agents without restart
        self._start_agent_watcher()

        tasks = [
            asyncio.create_task(self._agent_listener(), name="agent_listener"),
            asyncio.create_task(self._web_server(), name="web_server"),
            asyncio.create_task(self._self_register(), name="self_register"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._compaction_scheduler(), name="compaction"),
            asyncio.create_task(self._worker_loop(), name="worker_loop"),
            asyncio.create_task(self._reaper_loop(), name="reaper"),
            asyncio.create_task(self._reminder_watcher(), name="reminder_watcher"),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Daemon shutting down...")
        finally:
            self._running = False
            self.db.close()

    def _build_chat_agents(self) -> None:
        """Create PydanticAI chat agents for each agent config."""
        from glitch_core.agents import _can_run_model

        for cfg in self.agent_configs:
            if not cfg.enabled:
                continue
            if not _can_run_model(cfg.model, self.env):
                continue

            try:
                self._chat_agents[cfg.agent_id] = create_chat_agent(cfg)
                logger.info("Chat agent ready: %s (tools=%s)", cfg.agent_id, cfg.tools or [])
            except Exception:
                logger.exception("Failed to create chat agent: %s", cfg.agent_id)

    def _rebuild_agent(self, agent_id: str, data: dict) -> None:
        """Rebuild a single chat agent from updated Firestore data."""
        from glitch_core.agents import _can_run_model

        try:
            cfg = AgentConfig.model_validate(data)

            if not cfg.enabled:
                if agent_id in self._chat_agents:
                    del self._chat_agents[agent_id]
                    logger.info("Agent disabled: %s", agent_id)
                return

            if not _can_run_model(cfg.model, self.env):
                logger.info("Agent '%s' skipped — no API key for %s", agent_id, cfg.model)
                return

            self._chat_agents[agent_id] = create_chat_agent(cfg)

            # Update the config in our cached list
            self.agent_configs = [c for c in self.agent_configs if c.agent_id != agent_id]
            self.agent_configs.append(cfg)

            logger.info("Agent hot-reloaded: %s (tools=%s)", agent_id, cfg.tools or [])

        except Exception:
            logger.exception("Failed to rebuild agent: %s", agent_id)

    def _start_agent_watcher(self) -> None:
        """Watch /agents/ collection for changes and hot-rebuild affected agents."""
        import asyncio

        loop = asyncio.get_event_loop()
        initial_ids = {cfg.agent_id for cfg in self.agent_configs}

        def _on_agents_snapshot(doc_snapshot, changes, read_time):
            for change in changes:
                agent_id = change.document.id
                if agent_id == "_placeholder":
                    continue

                if change.type.name in ("ADDED", "MODIFIED"):
                    # Skip the initial snapshot (we already built these)
                    if change.type.name == "ADDED" and agent_id in initial_ids:
                        continue

                    data = change.document.to_dict()
                    data["agent_id"] = agent_id
                    loop.call_soon_threadsafe(self._rebuild_agent, agent_id, data)

                elif change.type.name == "REMOVED":
                    if agent_id in self._chat_agents:
                        del self._chat_agents[agent_id]
                        self.agent_configs = [c for c in self.agent_configs if c.agent_id != agent_id]
                        loop.call_soon_threadsafe(
                            logger.info, "Agent removed: %s", agent_id
                        )

        self._agent_watch = self.sync_db.collection("agents").on_snapshot(_on_agents_snapshot)
        logger.info("Watching /agents/ for config changes")

    async def _load_cached_context(self) -> None:
        """Load shared core memories once. Call again to refresh after compaction."""
        try:
            self._memories_cache = []
            from google.cloud.firestore_v1.base_query import FieldFilter
            query = self.db.collection("core_memories").where(
                filter=FieldFilter("deleted", "==", False)
            ).limit(500)
            async for doc in query.stream():
                if doc.id == "_placeholder":
                    continue
                self._memories_cache.append(doc.to_dict())
        except Exception:
            logger.exception("Failed to load core memories")
            self._memories_cache = []

        logger.info("Cached %d core memories", len(self._memories_cache))

    _subscribed_sessions: set[str] = set()

    def _subscribe_to_session(
        self,
        sid: str,
        msg_queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        watches: list,
        processed: dict[str, set[str]],
    ) -> None:
        """Subscribe to messages in a single session via on_snapshot."""
        if sid in self._subscribed_sessions:
            return  # Already subscribed
        self._subscribed_sessions.add(sid)

        if sid not in processed:
            processed[sid] = set()

        sync_msgs_ref = (
            self.sync_db.collection("sessions")
            .document(sid)
            .collection("messages")
            .order_by("created_at")
        )

        def _on_snapshot(doc_snapshot, changes, read_time):
            for change in changes:
                if change.type.name == "ADDED":
                    doc = change.document
                    loop.call_soon_threadsafe(
                        msg_queue.put_nowait,
                        (sid, doc.id, doc.to_dict()),
                    )

        watch = sync_msgs_ref.on_snapshot(_on_snapshot)
        watches.append(watch)
        logger.info("Subscribed to session: %s", sid)

    async def _agent_listener(self) -> None:
        """Watch Firestore for new user messages across ALL sessions via on_snapshot."""
        logger.info("Agent listener starting — multi-agent mode")

        await self._load_cached_context()

        # Track processed message IDs per session
        processed: dict[str, set[str]] = {}

        # Queue for snapshot events
        msg_queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()
        watches: list = []

        # Subscribe to existing sessions
        async for doc in self.db.collection("sessions").limit(100).stream():
            if doc.id == "_placeholder":
                continue

            # Pre-load existing message IDs
            processed[doc.id] = set()
            msgs_ref = (
                self.db.collection("sessions")
                .document(doc.id)
                .collection("messages")
                .order_by("created_at")
            )
            async for msg_doc in msgs_ref.stream():
                processed[doc.id].add(msg_doc.id)

            self._subscribe_to_session(doc.id, msg_queue, loop, watches, processed)

        logger.info("Subscribed to %d existing sessions", len(processed))

        # Also watch the sessions collection itself for NEW sessions
        def _on_session_snapshot(doc_snapshot, changes, read_time):
            for change in changes:
                if change.type.name == "ADDED":
                    sid = change.document.id
                    if sid != "_placeholder" and sid not in processed:
                        logger.info("New session detected: %s", sid)
                        self._subscribe_to_session(sid, msg_queue, loop, watches, processed)

        session_watch = self.sync_db.collection("sessions").on_snapshot(_on_session_snapshot)
        watches.append(session_watch)

        try:
            while self._running:
                try:
                    session_id, msg_id, data = await asyncio.wait_for(
                        msg_queue.get(), timeout=30
                    )
                except asyncio.TimeoutError:
                    continue

                if session_id not in processed:
                    processed[session_id] = set()

                if msg_id in processed[session_id]:
                    continue
                processed[session_id].add(msg_id)

                if data.get("role") != "user":
                    continue

                user_message = data.get("content", "")
                if not user_message:
                    continue

                logger.info("Processing message %s in session %s", msg_id, session_id)

                try:
                    await self._handle_message(user_message, session_id, msg_id)
                except Exception as e:
                    logger.exception("Error processing message %s", msg_id)
                    self.circuit_breaker.record_error(e)
                    err_id = f"msg_{uuid.uuid4().hex[:12]}"
                    processed[session_id].add(err_id)
                    await (
                        self.db.collection("sessions")
                        .document(session_id)
                        .collection("messages")
                        .document(err_id)
                        .set({
                            "message_id": err_id,
                            "session_id": session_id,
                            "role": "system",
                            "content": "Sorry, I encountered an error processing your message.",
                            "content_rating": "sfw",
                            "attachments": [],
                            "metadata": {},
                            "created_at": datetime.utcnow(),
                        })
                    )
        finally:
            for w in watches:
                w.unsubscribe()

    async def _handle_message(self, user_message: str, session_id: str, msg_id: str) -> None:
        """Process a user message — route to the correct agent based on the session's agent_id."""
        # Look up which agent this session belongs to
        session_doc = await self.db.collection("sessions").document(session_id).get()
        agent_id = self._default_agent_id
        if session_doc.exists:
            agent_id = session_doc.to_dict().get("agent_id", self._default_agent_id)

        # Get the chat agent
        chat_agent = self._chat_agents.get(agent_id)
        if chat_agent is None:
            logger.error("No chat agent for '%s' — falling back to router", agent_id)
            chat_agent = self._chat_agents.get(self._default_agent_id)
            if chat_agent is None:
                logger.error("No router agent available")
                return

        # Find this agent's config
        agent_cfg = None
        for cfg in self.agent_configs:
            if cfg.agent_id == agent_id:
                agent_cfg = cfg
                break

        # Build deps
        deps = AgentDeps(
            agent_config=agent_cfg,
            all_agents=self.agent_configs,
            core_memories=self._memories_cache,
            session_id=session_id,
            db=self.db,
            workspace=self.workspace,
            safe_writer=self.safe_writer,
            ouroboros_enabled=self._ouroboros_enabled,
        )

        # Load recent conversation history (most recent 20, then reverse to chronological)
        history_msgs = []
        msgs_ref = (
            self.db.collection("sessions")
            .document(session_id)
            .collection("messages")
            .order_by("created_at", direction="DESCENDING")
            .limit(20)
        )
        async for doc in msgs_ref.stream():
            data = doc.to_dict()
            role = data.get("role", "user")
            content = data.get("content", "")
            if role in ("user", "agent", "sub_agent") and content:
                history_msgs.append({"role": role, "content": content})
        history_msgs.reverse()

        # Build PydanticAI message history
        from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart

        messages: list[ModelMessage] = []
        for msg in history_msgs:
            if msg["role"] == "user":
                messages.append(ModelRequest(parts=[UserPromptPart(content=msg["content"])]))
            else:
                messages.append(ModelResponse(parts=[TextPart(content=msg["content"])]))

        # Create placeholder message for streaming
        resp_id = f"msg_{uuid.uuid4().hex[:12]}"
        resp_ref = (
            self.db.collection("sessions")
            .document(session_id)
            .collection("messages")
            .document(resp_id)
        )
        await resp_ref.set({
            "message_id": resp_id,
            "session_id": session_id,
            "role": "agent",
            "content": "",
            "streaming": True,
            "content_rating": str(agent_cfg.content_rating.value) if agent_cfg else "sfw",
            "attachments": [],
            "metadata": {"agent_id": agent_id},
            "created_at": datetime.utcnow(),
        })

        # Stream the agent response, updating Firestore periodically
        import time
        accumulated = ""
        last_flush = time.time()
        flush_interval = 0.6  # Update Firestore every 600ms

        async with chat_agent.run_stream(
            user_message,
            deps=deps,
            message_history=messages[:-1] if messages else [],
        ) as stream_result:
            # stream_text yields text deltas from each model response.
            # With exhaustive end_strategy, there may be multiple model calls
            # (text → tool → text), and stream_text covers all of them.
            async for chunk in stream_result.stream_text(delta=True):
                accumulated += chunk
                now = time.time()
                if now - last_flush >= flush_interval:
                    await resp_ref.update({"content": accumulated})
                    last_flush = now

            # get_output() is authoritative
            try:
                reply = await stream_result.get_output()
                if isinstance(reply, str) and reply != accumulated:
                    accumulated = reply
            except Exception:
                pass

            # Check if tools were called that need a follow-up.
            # Passive tools (write_journal, read_soul) don't need continuation.
            # Active tools (workspace_write, create_tool, spawn_sub_agent, etc.) do.
            PASSIVE_TOOLS = {"write_journal", "read_soul", "set_reminder", "list_reminders", "cancel_reminder", "read_page", "list_pages", "read_tool", "list_tools", "list_agents", "read_agent"}
            has_active_tool_calls = False
            try:
                for m in stream_result.all_messages():
                    for p in m.parts:
                        if hasattr(p, "part_kind") and p.part_kind == "tool-call":
                            tool_name = getattr(p, "tool_name", "") or ""
                            if tool_name not in PASSIVE_TOOLS:
                                has_active_tool_calls = True
                                break
                    if has_active_tool_calls:
                        break
            except Exception:
                pass

        # If tools were called, the stream only captured one round-trip.
        # Do a non-streaming follow-up with the full message history so the
        # model can chain tool calls and summarize all results.
        if has_active_tool_calls:
            try:
                # Signal the UI to show thinking animation
                await resp_ref.update({"thinking": True})

                follow_up = await chat_agent.run(
                    "Continue. Complete all remaining steps of the task and summarize the results.",
                    deps=deps,
                    message_history=stream_result.all_messages(),
                )
                follow_up_text = follow_up.output
                if isinstance(follow_up_text, str) and follow_up_text:
                    accumulated = accumulated.rstrip() + "\n\n" + follow_up_text
                    await resp_ref.update({
                        "content": accumulated,
                        "thinking": False,
                    })

                    # Merge usage from follow-up
                    stream_result = follow_up  # swap for usage/log extraction below
                else:
                    await resp_ref.update({"thinking": False})
            except Exception:
                logger.exception("Follow-up after tool calls failed")
                await resp_ref.update({"thinking": False})

        # Extract usage metadata
        usage = stream_result.usage()
        run_metadata = {
            "run_id": str(stream_result.run_id) if hasattr(stream_result, "run_id") else None,
            "agent_id": agent_id,
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "requests": usage.requests,
                "tool_calls": usage.tool_calls,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
            },
            "source_msg_id": msg_id,
        }

        logger.info(
            "[%s] Response: %d input, %d output tokens, %d tool calls",
            agent_id, usage.input_tokens or 0, usage.output_tokens or 0, usage.tool_calls or 0,
        )

        # Finalize the message — mark streaming complete
        await resp_ref.update({
            "content": accumulated,
            "streaming": False,
            "metadata": run_metadata,
        })

        # Store full run log for debugging
        try:
            all_messages_json = stream_result.all_messages_json()
            await (
                self.db.collection("sessions")
                .document(session_id)
                .collection("run_logs")
                .document(resp_id)
                .set({
                    "response_msg_id": resp_id,
                    "source_msg_id": msg_id,
                    "agent_id": agent_id,
                    "all_messages": all_messages_json.decode("utf-8")
                        if isinstance(all_messages_json, bytes)
                        else all_messages_json,
                    "usage": run_metadata["usage"],
                    "created_at": datetime.utcnow(),
                })
            )
        except Exception:
            logger.exception("Failed to write run log (non-fatal)")

        logger.info("Wrote response: %s (agent=%s)", resp_id, agent_id)

    async def _web_server(self) -> None:
        """Run the FastAPI web server."""
        app = create_app(db=self.db)
        app.state.workspace = self.workspace

        # Wire SafeFileWriter to the PageEngine and app for hot-reload
        if hasattr(app.state, "page_engine") and app.state.page_engine:
            self.safe_writer.page_engine = app.state.page_engine
            self.safe_writer.app = app
            logger.info("SafeFileWriter wired to PageEngine + app for hot-reload")
        host = self.env.host
        port = self.env.port
        if host == "0.0.0.0":
            logger.warning("Web server binding to 0.0.0.0 — no auth! Only safe behind a VPN/Tailscale.")
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
        )
        server = uvicorn.Server(config)
        await server.serve()

    async def _self_register(self) -> None:
        """Register this node as a worker in Firestore."""
        from glitch_core.workers.registration import register_worker
        await register_worker(self.db, self.env, self.agent_configs)

    async def _heartbeat_loop(self) -> None:
        """Update heartbeat every 30 seconds."""
        worker_id = self.env.node_name
        while self._running:
            await asyncio.sleep(30)
            try:
                await self.db.collection("workers").document(worker_id).update({
                    "last_heartbeat": datetime.utcnow(),
                    "online": True,
                })
            except Exception:
                logger.exception("Heartbeat update failed")

    async def _compaction_scheduler(self) -> None:
        """Run memory compaction on a schedule."""
        from glitch_core.compaction.pipeline import run_compaction
        from glitch_core.schemas import CompactionConfig

        await asyncio.sleep(10)
        logger.info("Compaction scheduler started")

        while self._running:
            try:
                doc = await self.db.collection("meta").document("compaction_config").get()
                config = CompactionConfig.model_validate(doc.to_dict()) if doc.exists else CompactionConfig()

                if not config.enabled:
                    await asyncio.sleep(3600)
                    continue

                result = await run_compaction(self.db, config)
                logger.info("Compaction run %s: %s", result.run_id, result.status)

                # Refresh memory cache after compaction
                if result.status == "completed":
                    await self._load_cached_context()

                await asyncio.sleep(6 * 3600)

            except Exception:
                logger.exception("Compaction scheduler error")
                await asyncio.sleep(300)

    async def _worker_loop(self) -> None:
        """Run the worker daemon for processing sub-agent tasks."""
        from glitch_core.workers.loop import WorkerDaemon

        if not self.agent_registry:
            logger.info("No worker agents in registry — worker loop idle")
            return

        worker_configs = [a for a in self.agent_configs if a.agent_id != self._default_agent_id]
        worker = WorkerDaemon(
            db=self.db,
            env=self.env,
            agent_configs=worker_configs,
            agent_registry=self.agent_registry,
        )
        await worker.run()

    async def _reaper_loop(self) -> None:
        """Reclaim stale tasks from dead workers."""
        from glitch_core.workers.reaper import reap_stale_tasks

        await asyncio.sleep(15)
        logger.info("Reaper loop started")

        while self._running:
            try:
                await reap_stale_tasks(self.db)
            except Exception:
                logger.exception("Reaper error")
            await asyncio.sleep(300)

    async def _reminder_watcher(self) -> None:
        """Check for and fire due reminders every 15 seconds."""
        from datetime import timezone

        await asyncio.sleep(5)
        logger.info("Reminder watcher started")

        while self._running:
            try:
                from google.cloud.firestore_v1.base_query import FieldFilter

                now = datetime.utcnow()

                # Query unfired reminders that are past their fire_at time
                query = (
                    self.db.collection("reminders")
                    .where(filter=FieldFilter("fired", "==", False))
                    .limit(20)
                )

                async for doc in query.stream():
                    data = doc.to_dict()
                    fire_at = data.get("fire_at")

                    if fire_at is None:
                        continue

                    # Handle timezone-aware timestamps from Firestore
                    if hasattr(fire_at, "tzinfo") and fire_at.tzinfo is not None:
                        fire_at_naive = fire_at.replace(tzinfo=None)
                    else:
                        fire_at_naive = fire_at

                    if fire_at_naive > now:
                        continue  # Not due yet

                    # Fire the reminder!
                    session_id = data.get("session_id", "default")
                    message = data.get("message", "Reminder!")
                    reminder_id = doc.id

                    logger.info("Firing reminder %s: %s", reminder_id, message[:60])

                    # Write the reminder as a message with notification metadata
                    msg_id = f"msg_{uuid.uuid4().hex[:12]}"
                    await (
                        self.db.collection("sessions")
                        .document(session_id)
                        .collection("messages")
                        .document(msg_id)
                        .set({
                            "message_id": msg_id,
                            "session_id": session_id,
                            "role": "agent",
                            "content": message,
                            "content_rating": "sfw",
                            "notification": {
                                "type": "reminder",
                                "sound": True,
                                "title": "Reminder",
                            },
                            "attachments": [],
                            "metadata": {"reminder_id": reminder_id},
                            "created_at": datetime.utcnow(),
                        })
                    )

                    # Mark as fired
                    await self.db.collection("reminders").document(reminder_id).update({
                        "fired": True,
                    })

            except Exception:
                logger.exception("Reminder watcher error")

            await asyncio.sleep(15)


async def run_daemon(env: GlitchEnv | None = None) -> None:
    """Entry point for running the daemon."""
    daemon = GlitchDaemon(env)
    await daemon.start()


def main() -> None:
    """Synchronous entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_daemon())


if __name__ == "__main__":
    main()
