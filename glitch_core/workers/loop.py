from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from google.cloud.firestore_v1 import Client as SyncClient
from google.oauth2 import service_account

from glitch_core.config import GlitchEnv
from glitch_core.schemas import AgentConfig, TaskError, WorkerRegistration
from glitch_core.workers.protocol import try_claim_task
from glitch_core.workers.registration import register_worker

logger = logging.getLogger(__name__)


def _format_agent_result(agent_id: str, result_data: dict[str, Any], raw_output: Any) -> str:
    """Format a sub-agent's structured output into readable markdown for chat."""

    # ResearchResult
    if "summary" in result_data and "sources" in result_data:
        parts = [f"**Research: {result_data.get('query', 'Results')}**\n"]
        parts.append(result_data["summary"])
        sources = result_data.get("sources", [])
        if sources:
            parts.append("\n**Sources:**")
            for src in sources:
                title = src.get("title", "Link")
                url = src.get("url", "")
                snippet = src.get("snippet", "")
                if url:
                    parts.append(f"- [{title}]({url})")
                elif title:
                    parts.append(f"- {title}")
                if snippet:
                    parts.append(f"  {snippet}")
        confidence = result_data.get("confidence")
        if confidence is not None:
            parts.append(f"\n*Confidence: {int(confidence * 100)}%*")
        return "\n".join(parts)

    # CodeArtifact
    if "code" in result_data and "language" in result_data:
        parts = [f"**{result_data.get('filename', 'Code')}**\n"]
        if result_data.get("explanation"):
            parts.append(result_data["explanation"])
            parts.append("")
        lang = result_data.get("language", "")
        parts.append(f"```{lang}")
        parts.append(result_data["code"])
        parts.append("```")
        if result_data.get("tests"):
            parts.append("\n**Tests:**")
            parts.append(f"```{lang}")
            parts.append(result_data["tests"])
            parts.append("```")
        return "\n".join(parts)

    # CommandResult
    if "exit_code" in result_data and "command" in result_data:
        status = "success" if result_data["exit_code"] == 0 else "failed"
        parts = [f"**Command** (`{result_data.get('host', 'localhost')}`) — {status}\n"]
        parts.append(f"```\n$ {result_data['command']}\n```")
        if result_data.get("stdout"):
            parts.append(f"**stdout:**\n```\n{result_data['stdout']}\n```")
        if result_data.get("stderr"):
            parts.append(f"**stderr:**\n```\n{result_data['stderr']}\n```")
        duration = result_data.get("duration_ms")
        if duration:
            parts.append(f"*{duration}ms*")
        return "\n".join(parts)

    # Plain text or unknown structure
    content = result_data.get("content") or result_data.get("explanation") or str(raw_output)
    return f"**[{agent_id}]** {content}"


class WorkerDaemon:
    """Distributed task executor. Runs on every node that processes sub-agent tasks."""

    def __init__(
        self,
        db: Any,
        env: GlitchEnv,
        agent_configs: list[AgentConfig],
        agent_registry: dict[str, Any],
    ) -> None:
        self.db = db
        self.env = env
        self.agent_configs = agent_configs
        self.agent_registry = agent_registry
        self.registration: WorkerRegistration | None = None
        self._running = False

        # Sync client for on_snapshot
        creds = service_account.Credentials.from_service_account_file(
            str(env.firebase_credentials)
        )
        self.sync_db = SyncClient(project=env.firebase_project, credentials=creds)

    async def run(self) -> None:
        """Start the worker: register, heartbeat, and listen for tasks."""
        self._running = True

        self.registration = await register_worker(self.db, self.env, self.agent_configs)

        tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="worker_heartbeat"),
            asyncio.create_task(self._task_listener(), name="worker_task_listener"),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Worker daemon shutting down")
        finally:
            self._running = False

    async def _heartbeat_loop(self) -> None:
        """Publish liveness every 30 seconds."""
        worker_id = self.env.node_name
        while self._running:
            await asyncio.sleep(30)
            try:
                await self.db.collection("workers").document(worker_id).update({
                    "last_heartbeat": datetime.utcnow(),
                    "status": "online",
                })
            except Exception:
                logger.exception("Worker heartbeat failed")

    async def _task_listener(self) -> None:
        """Watch for pending sub_tasks via on_snapshot (zero-cost when idle)."""
        logger.info(
            "Worker task listener starting — agents: %s",
            list(self.agent_registry.keys()),
        )

        # Queue for snapshot events from background thread
        task_queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()
        watches: list = []
        subscribed_sessions: set[str] = set()

        def _subscribe_tasks(sid: str) -> None:
            """Subscribe to pending tasks in a session."""
            if sid in subscribed_sessions:
                return
            subscribed_sessions.add(sid)

            sync_tasks_ref = (
                self.sync_db.collection("sessions")
                .document(sid)
                .collection("sub_tasks")
                .where("status", "==", "pending")
            )

            def _on_snapshot(doc_snapshot, changes, read_time):
                for change in changes:
                    if change.type.name == "ADDED":
                        doc = change.document
                        loop.call_soon_threadsafe(
                            task_queue.put_nowait,
                            (sid, doc.id, doc.to_dict()),
                        )

            watch = sync_tasks_ref.on_snapshot(_on_snapshot)
            watches.append(watch)
            logger.info("Worker subscribed to tasks in session: %s", sid)

        # Subscribe to existing sessions
        async for doc in self.db.collection("sessions").limit(100).stream():
            if doc.id != "_placeholder":
                _subscribe_tasks(doc.id)

        # Watch for new sessions
        def _on_session_snapshot(doc_snapshot, changes, read_time):
            for change in changes:
                if change.type.name == "ADDED":
                    sid = change.document.id
                    if sid != "_placeholder":
                        _subscribe_tasks(sid)

        session_watch = self.sync_db.collection("sessions").on_snapshot(_on_session_snapshot)
        watches.append(session_watch)

        logger.info("Worker subscribed to %d sessions + watching for new ones", len(subscribed_sessions))

        try:
            while self._running:
                try:
                    session_id, task_id, data = await asyncio.wait_for(
                        task_queue.get(), timeout=30
                    )
                except asyncio.TimeoutError:
                    continue

                if data.get("status") != "pending":
                    continue

                if self._can_handle(data):
                    await self._try_and_execute(session_id, task_id, data)

        finally:
            for w in watches:
                w.unsubscribe()

    def _can_handle(self, task_data: dict[str, Any]) -> bool:
        """Local routing filter — check if we should even try to claim this task."""
        if self.registration is None:
            return False

        routing = task_data.get("routing", {})
        affinity = routing.get("affinity", "any")
        target_worker = routing.get("target_worker")
        agent_id = routing.get("agent_id", "")

        # 1. Exclusive affinity to another worker → skip
        if affinity == "exclusive" and target_worker != self.registration.worker_id:
            return False

        # 2. Preferred for another worker and not yet timed out → skip
        if affinity == "preferred" and target_worker != self.registration.worker_id:
            fallback_seconds = routing.get("fallback_window_seconds", 300)
            created_at = task_data.get("created_at")
            if created_at and isinstance(created_at, datetime):
                age = datetime.now(timezone.utc) - (
                    created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
                )
                if age < timedelta(seconds=fallback_seconds):
                    return False

        # 3. Capability match
        required_caps = set(routing.get("required_capabilities", []))
        if required_caps:
            node_caps = set(self.registration.capabilities)
            if not required_caps.issubset(node_caps):
                return False

        # 4. Agent support
        if agent_id and agent_id not in self.registration.supported_agents:
            return False

        # 5. We actually have the agent instance
        if agent_id and agent_id not in self.agent_registry:
            return False

        return True

    async def _try_and_execute(
        self,
        session_id: str,
        task_id: str,
        task_data: dict[str, Any],
    ) -> None:
        """Attempt to claim a task and execute it if we win the claim."""
        worker_id = self.env.node_name

        # Atomic claim
        claim = await try_claim_task(self.db, session_id, task_id, worker_id)
        if not claim.claimed:
            return

        agent_id = task_data.get("routing", {}).get("agent_id", "unknown")
        prompt = task_data.get("prompt", "")

        logger.info("Claimed task %s (agent=%s)", task_id, agent_id)

        task_ref = (
            self.db.collection("sessions")
            .document(session_id)
            .collection("sub_tasks")
            .document(task_id)
        )

        # Mark running
        await task_ref.update({
            "status": "running",
            "started_at": datetime.utcnow(),
        })

        # Update worker current_task
        await self.db.collection("workers").document(worker_id).update({
            "current_task": task_id,
        })

        try:
            agent = self.agent_registry[agent_id]
            result = await agent.run(prompt)

            # Extract output
            output = result.output
            if hasattr(output, "model_dump"):
                result_data = output.model_dump()
            else:
                result_data = {"content": str(output)}

            # Mark completed
            await task_ref.update({
                "status": "completed",
                "result": result_data,
                "completed_at": datetime.utcnow(),
            })

            # Write result as a message in the session
            msg_id = f"msg_{uuid.uuid4().hex[:12]}"
            content = _format_agent_result(agent_id, result_data, output)
            await (
                self.db.collection("sessions")
                .document(session_id)
                .collection("messages")
                .document(msg_id)
                .set({
                    "message_id": msg_id,
                    "session_id": session_id,
                    "role": "sub_agent",
                    "content": content,
                    "content_rating": task_data.get("content_rating", "sfw"),
                    "attachments": [],
                    "metadata": {
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "result": result_data,
                    },
                    "created_at": datetime.utcnow(),
                })
            )

            # Write run log for observability
            try:
                usage = result.usage()
                all_messages_json = result.all_messages_json()
                await (
                    self.db.collection("sessions")
                    .document(session_id)
                    .collection("run_logs")
                    .document(msg_id)
                    .set({
                        "response_msg_id": msg_id,
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "worker_id": worker_id,
                        "log_type": "sub_agent",
                        "all_messages": all_messages_json.decode("utf-8")
                            if isinstance(all_messages_json, bytes)
                            else all_messages_json,
                        "usage": {
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "requests": usage.requests,
                            "tool_calls": usage.tool_calls,
                        },
                        "created_at": datetime.utcnow(),
                    })
                )
            except Exception:
                logger.exception("Failed to write worker run log (non-fatal)")

            logger.info("Task %s completed by agent %s", task_id, agent_id)

        except Exception as e:
            logger.exception("Task %s failed", task_id)
            await task_ref.update({
                "status": "failed",
                "error": TaskError(
                    error_type="execution",
                    message=str(e),
                ).model_dump(),
                "completed_at": datetime.utcnow(),
            })

            err_msg_id = f"msg_{uuid.uuid4().hex[:12]}"
            await (
                self.db.collection("sessions")
                .document(session_id)
                .collection("messages")
                .document(err_msg_id)
                .set({
                    "message_id": err_msg_id,
                    "session_id": session_id,
                    "role": "system",
                    "content": f"Sub-agent '{agent_id}' failed: {e}",
                    "content_rating": "sfw",
                    "attachments": [],
                    "metadata": {"task_id": task_id, "error": str(e)},
                    "created_at": datetime.utcnow(),
                })
            )

        finally:
            await self.db.collection("workers").document(worker_id).update({
                "current_task": None,
            })
