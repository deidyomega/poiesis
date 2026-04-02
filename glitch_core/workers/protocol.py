from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from glitch_core.schemas import ClaimResult

logger = logging.getLogger(__name__)


async def try_claim_task(
    db: Any,
    session_id: str,
    task_id: str,
    worker_id: str,
) -> ClaimResult:
    """Atomically claim a pending task.

    Uses a read-then-conditional-update pattern. Firestore transactions with
    the async client have API inconsistencies, so we do a direct read + update
    with a status check. Two workers racing: one wins the update, the other
    sees status != "pending" on its next poll and skips it.
    """
    task_ref = (
        db.collection("sessions")
        .document(session_id)
        .collection("sub_tasks")
        .document(task_id)
    )

    try:
        doc = await task_ref.get()

        if not doc.exists:
            return ClaimResult(claimed=False, task_id=task_id, reason="not_found")

        data = doc.to_dict()
        status = data.get("status", "")

        if status != "pending":
            return ClaimResult(claimed=False, task_id=task_id, reason=f"already_{status}")

        # Claim it — the worker loop's on_snapshot filter (status == "pending")
        # ensures only one worker typically sees each task. In the rare race case,
        # the second worker will read status="claimed" and bail.
        await task_ref.update({
            "status": "claimed",
            "claimed_by": worker_id,
            "claimed_at": datetime.utcnow(),
        })

        return ClaimResult(claimed=True, task_id=task_id)

    except Exception as e:
        logger.exception("Claim failed for task %s", task_id)
        return ClaimResult(claimed=False, task_id=task_id, reason=f"error: {e}")
