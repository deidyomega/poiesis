from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from google.cloud.firestore_v1.base_query import FieldFilter

logger = logging.getLogger(__name__)


async def reap_stale_tasks(db: Any) -> None:
    """Recover stale tasks from dead workers. Runs every 60 seconds.

    Three responsibilities:
    1. Release tasks claimed by dead workers (no heartbeat for 2+ min)
    2. Promote preferred→fallback tasks past their window
    3. Monitor (never reassign) exclusive tasks waiting too long
    """
    now = datetime.now(timezone.utc)

    # 1. Identify dead workers (single query, small collection)
    dead_worker_ids: set[str] = set()
    async for doc in db.collection("workers").limit(100).stream():
        if doc.id == "_placeholder":
            continue
        data = doc.to_dict()
        last_hb = data.get("last_heartbeat")
        if last_hb and isinstance(last_hb, datetime):
            hb_aware = last_hb if last_hb.tzinfo else last_hb.replace(tzinfo=timezone.utc)
            if (now - hb_aware) > timedelta(minutes=2):
                dead_worker_ids.add(doc.id)

    # Only scan tasks if we have dead workers or need to check preferred/exclusive
    # Get known sessions (cached, small collection)
    sessions: list[str] = []
    async for doc in db.collection("sessions").limit(100).stream():
        if doc.id != "_placeholder":
            sessions.append(doc.id)

    for session_id in sessions:
        tasks_coll = db.collection("sessions").document(session_id).collection("sub_tasks")

        # 1. Release tasks from dead workers — query only claimed/running tasks
        if dead_worker_ids:
            for status in ("claimed", "running"):
                query = tasks_coll.where(filter=FieldFilter("status", "==", status)).limit(50)
                async for task_doc in query.stream():
                    data = task_doc.to_dict()
                    claimed_by = data.get("claimed_by", "")
                    if claimed_by in dead_worker_ids:
                        logger.warning(
                            "Releasing task %s from dead worker %s",
                            task_doc.id, claimed_by,
                        )
                        await tasks_coll.document(task_doc.id).update({
                            "status": "pending",
                            "claimed_by": None,
                            "claimed_at": None,
                            "started_at": None,
                        })

        # 2 & 3. Check pending tasks for preferred fallback and exclusive monitoring
        pending_query = tasks_coll.where(filter=FieldFilter("status", "==", "pending")).limit(50)
        async for task_doc in pending_query.stream():
            data = task_doc.to_dict()
            routing = data.get("routing", {})
            affinity = routing.get("affinity", "any")
            created_at = data.get("created_at")

            if not created_at or not isinstance(created_at, datetime):
                continue

            ca_aware = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
            age = now - ca_aware

            # 2. Promote preferred→fallback
            if affinity == "preferred":
                fallback_agent = routing.get("fallback_agent")
                fallback_seconds = routing.get("fallback_window_seconds", 300)
                if fallback_agent and age > timedelta(seconds=fallback_seconds):
                    logger.info(
                        "Promoting task %s to fallback agent '%s' after %ds",
                        task_doc.id, fallback_agent, age.total_seconds(),
                    )
                    new_routing = {**routing}
                    new_routing["agent_id"] = fallback_agent
                    new_routing["affinity"] = "any"
                    new_routing["target_worker"] = None
                    await tasks_coll.document(task_doc.id).update({"routing": new_routing})

            # 3. Monitor exclusive (never reassign)
            elif affinity == "exclusive" and age > timedelta(hours=24):
                logger.warning(
                    "Exclusive task %s waiting %s — target '%s' may be offline",
                    task_doc.id, age, routing.get("target_worker", "unknown"),
                )
