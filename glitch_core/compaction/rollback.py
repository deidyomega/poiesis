from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


async def rollback_compaction_run(db: Any, run_id: str) -> bool:
    """Revert all changes from a compaction run.

    1. Memories created by this run → delete
    2. Memories updated by this run → revert to previous_content
    3. Journals archived by this run → restore to active

    Args:
        db: Async Firestore client.
        run_id: The compaction run ID to roll back.

    Returns:
        True if rollback succeeded, False if the run wasn't found.
    """
    # Verify the run exists
    run_doc = await db.collection("compaction_runs").document(run_id).get()
    if not run_doc.exists:
        logger.error("Compaction run not found: %s", run_id)
        return False

    run_data = run_doc.to_dict()
    if run_data.get("status") == "rolled_back":
        logger.warning("Compaction run already rolled back: %s", run_id)
        return True

    logger.info("Rolling back compaction run: %s", run_id)

    # 1. Revert/delete core memories from this run
    memories_reverted = 0
    memories_deleted = 0

    async for doc in db.collection("core_memories").stream():
        if doc.id == "_placeholder":
            continue
        data = doc.to_dict()
        if data.get("compaction_run") != run_id:
            continue

        doc_ref = db.collection("core_memories").document(doc.id)

        if data.get("previous_content") is not None:
            # This was an update — revert to previous content
            await doc_ref.update({
                "content": data["previous_content"],
                "previous_content": None,
                "version": max(data.get("version", 2) - 1, 1),
                "compaction_run": None,
                "updated_at": datetime.utcnow(),
            })
            memories_reverted += 1
        else:
            # This was a new memory created by this run — delete it
            await doc_ref.delete()
            memories_deleted += 1

    logger.info(
        "Memories: %d reverted, %d deleted", memories_reverted, memories_deleted
    )

    # 2. Remove review items from this run
    reviews_removed = 0
    async for doc in db.collection("memory_review").stream():
        if doc.id == "_placeholder":
            continue
        data = doc.to_dict()
        if data.get("compaction_run") == run_id:
            await db.collection("memory_review").document(doc.id).delete()
            reviews_removed += 1

    logger.info("Review items removed: %d", reviews_removed)

    # 3. Restore archived journals from this run
    journals_restored = 0
    async for doc in db.collection("journals_archive").stream():
        if doc.id == "_placeholder":
            continue
        data = doc.to_dict()
        if data.get("compaction_run") != run_id:
            continue

        # Restore the journal to active
        journal_id = doc.id
        await db.collection("journals").document(journal_id).update({
            "archived": False,
        })

        # Remove the archive copy
        await db.collection("journals_archive").document(journal_id).delete()
        journals_restored += 1

    logger.info("Journals restored: %d", journals_restored)

    # 4. Update the run status
    await db.collection("compaction_runs").document(run_id).update({
        "status": "rolled_back",
        "rolled_back_at": datetime.utcnow(),
    })

    logger.info("Compaction run %s rolled back successfully", run_id)
    return True
