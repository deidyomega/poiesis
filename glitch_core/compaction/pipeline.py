from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from pydantic_ai import Agent

from glitch_core.compaction.prompts import (
    COMPACTION_SYSTEM_PROMPT,
    MERGE_SYSTEM_PROMPT,
    build_compaction_prompt,
    build_merge_prompt,
)
from glitch_core.schemas import (
    CompactedMemory,
    CompactionConfig,
    CompactionError,
    CompactionResult,
    MergeResult,
    CompactionRun,
)

logger = logging.getLogger(__name__)


async def run_compaction(db: Any, config: CompactionConfig | None = None) -> CompactionRun:
    """Execute the full compaction pipeline.

    Four crash-safe phases:
      1. Read — grab unprocessed journals, load existing memories
      2. Group & Summarize — batch journals and send to summarization agent
      3. Validate & Write — write memories to Firestore, flag low-confidence for review
      4. Archive — mark consumed journals as archived

    Args:
        db: Async Firestore client.
        config: Compaction settings. Loaded from Firestore if not provided.

    Returns:
        CompactionRun audit log.
    """
    # Load config from Firestore if not provided
    if config is None:
        doc = await db.collection("meta").document("compaction_config").get()
        if doc.exists:
            config = CompactionConfig.model_validate(doc.to_dict())
        else:
            config = CompactionConfig()

    run_id = f"comp_{uuid.uuid4().hex[:12]}"
    run = CompactionRun(
        run_id=run_id,
        config_snapshot=config.model_dump(),
    )

    logger.info("Starting compaction run: %s (dry_run=%s)", run_id, config.dry_run)

    try:
        # ── Phase 1: Read ──────────────────────────────────────────────
        journals = await _phase_read(db, config, run)
        if journals is None:
            # Not enough journals to trigger
            await _write_run_log(db, run)
            return run

        existing_memories = await _load_existing_memories(db)

        # ── Phase 2: Group & Summarize ─────────────────────────────────
        all_results = await _phase_summarize(db, config, run, journals, existing_memories)

        if config.dry_run:
            run.status = "dry_run"
            run.completed_at = datetime.utcnow()
            logger.info(
                "Dry run complete: %d journals read, %d memories proposed",
                run.journals_read,
                sum(len(r.memories) for r in all_results),
            )
            await _write_run_log(db, run)
            return run

        # ── Phase 3: Validate & Write ──────────────────────────────────
        consumed_journal_ids = await _phase_write(
            db, config, run, all_results, existing_memories
        )

        # ── Phase 4: Archive ───────────────────────────────────────────
        if config.archive_journals:
            await _phase_archive(db, run, journals, consumed_journal_ids)

        # ── Phase 5: Merge ────────────────────────────────────────────
        # After writing new memories, check if any should be combined
        # with existing memories into richer entries
        await _phase_merge(db, config, run)

        run.status = "completed"
        run.completed_at = datetime.utcnow()

    except Exception as e:
        logger.exception("Compaction run failed: %s", run_id)
        run.status = "failed"
        run.completed_at = datetime.utcnow()
        run.errors.append(CompactionError(
            stage="pipeline",
            message=str(e),
            recoverable=False,
        ))

    await _write_run_log(db, run)

    logger.info(
        "Compaction %s: %s — %d journals, %d created, %d updated, %d flagged, %d errors",
        run_id, run.status, run.journals_read, run.memories_created,
        run.memories_updated, run.memories_flagged, len(run.errors),
    )

    return run


# ── Phase 1: Read ──────────────────────────────────────────────────────────

async def _phase_read(
    db: Any,
    config: CompactionConfig,
    run: CompactionRun,
) -> list[dict[str, Any]] | None:
    """Read unprocessed journals from Firestore."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    journals: list[dict[str, Any]] = []

    query = (
        db.collection("journals")
        .where(filter=FieldFilter("archived", "==", False))
        .order_by("created_at")
        .limit(config.max_journals_per_run)
    )

    async for doc in query.stream():
        if doc.id == "_placeholder":
            continue
        data = doc.to_dict()
        data["journal_id"] = doc.id
        journals.append(data)

    run.journals_read = len(journals)

    if len(journals) < config.min_journals_to_trigger:
        logger.info(
            "Only %d journals (need %d) — skipping compaction",
            len(journals), config.min_journals_to_trigger,
        )
        run.status = "skipped"
        run.completed_at = datetime.utcnow()
        return None

    logger.info("Phase 1 — Read: %d unarchived journals", len(journals))
    return journals


async def _load_existing_memories(db: Any) -> dict[str, dict[str, Any]]:
    """Load all active core memories for cross-referencing."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    memories: dict[str, dict[str, Any]] = {}

    query = db.collection("core_memories").where(
        filter=FieldFilter("deleted", "==", False)
    ).limit(1000)
    async for doc in query.stream():
        if doc.id == "_placeholder":
            continue
        memories[doc.id] = doc.to_dict()

    logger.info("Loaded %d existing core memories for cross-reference", len(memories))
    return memories


# ── Phase 2: Group & Summarize ─────────────────────────────────────────────

async def _phase_summarize(
    db: Any,
    config: CompactionConfig,
    run: CompactionRun,
    journals: list[dict[str, Any]],
    existing_memories: dict[str, dict[str, Any]],
) -> list[CompactionResult]:
    """Batch journals and run through the summarization agent."""
    agent = Agent(
        config.model,
        output_type=CompactionResult,
        system_prompt=COMPACTION_SYSTEM_PROMPT,
        defer_model_check=True,
    )

    # Batch journals
    batches: list[list[dict[str, Any]]] = []
    for i in range(0, len(journals), config.batch_size):
        batches.append(journals[i : i + config.batch_size])

    logger.info("Phase 2 — Summarize: %d batches of up to %d journals", len(batches), config.batch_size)

    all_results: list[CompactionResult] = []

    for batch_idx, batch in enumerate(batches):
        try:
            prompt = build_compaction_prompt(batch, existing_memories)
            result = await agent.run(prompt)
            compaction_result: CompactionResult = result.output

            logger.info(
                "Batch %d/%d: %d memories, %d discarded",
                batch_idx + 1, len(batches),
                len(compaction_result.memories),
                len(compaction_result.discarded),
            )
            all_results.append(compaction_result)

        except Exception as e:
            logger.exception("Batch %d/%d failed", batch_idx + 1, len(batches))
            batch_ids = [j.get("journal_id", "?") for j in batch]
            run.errors.append(CompactionError(
                stage="summarization",
                message=str(e),
                journal_ids=batch_ids,
                recoverable=True,
            ))

    return all_results


# ── Phase 3: Validate & Write ──────────────────────────────────────────────

async def _phase_write(
    db: Any,
    config: CompactionConfig,
    run: CompactionRun,
    all_results: list[CompactionResult],
    existing_memories: dict[str, dict[str, Any]],
) -> set[str]:
    """Write compacted memories to Firestore. Returns consumed journal IDs."""
    consumed_journal_ids: set[str] = set()

    for result in all_results:
        for mem in result.memories:
            try:
                # Track which journals were consumed
                for j_id in mem.source_journal_ids:
                    consumed_journal_ids.add(j_id)

                # Check if this updates an existing memory
                updating_id = None
                if mem.related_memory_ids:
                    for rel_id in mem.related_memory_ids:
                        if rel_id in existing_memories:
                            updating_id = rel_id
                            break

                if updating_id:
                    await _update_existing_memory(db, run.run_id, updating_id, mem, existing_memories)
                    run.memories_updated += 1
                else:
                    await _create_new_memory(db, run.run_id, mem)
                    run.memories_created += 1

            except Exception as e:
                logger.exception("Failed to write memory: %s", mem.content[:60])
                run.errors.append(CompactionError(
                    stage="write",
                    message=str(e),
                    journal_ids=mem.source_journal_ids,
                    recoverable=True,
                ))

    logger.info(
        "Phase 3 — Write: %d created, %d updated, %d flagged for review",
        run.memories_created, run.memories_updated, run.memories_flagged,
    )
    return consumed_journal_ids


async def _write_review_item(
    db: Any, run_id: str, mem: CompactedMemory
) -> None:
    """Write a low-confidence memory to the review queue."""
    review_id = f"rev_{uuid.uuid4().hex[:12]}"
    await db.collection("memory_review").document(review_id).set({
        "memory_id": review_id,
        "content": mem.content,
        "category": mem.category,
        "confidence": mem.confidence,
        "importance": mem.importance,
        "source_journals": mem.source_journal_ids,
        "related_memory_ids": mem.related_memory_ids,
        "compaction_run": run_id,
        "reviewed": False,
        "created_at": datetime.utcnow(),
    })


async def _create_new_memory(
    db: Any, run_id: str, mem: CompactedMemory
) -> None:
    """Create a brand-new core memory."""
    memory_id = f"mem_{uuid.uuid4().hex[:12]}"
    await db.collection("core_memories").document(memory_id).set({
        "memory_id": memory_id,
        "content": mem.content,
        "category": mem.category,
        "confidence": mem.confidence,
        "importance": mem.importance,
        "source_journals": mem.source_journal_ids,
        "previous_content": None,
        "version": 1,
        "reviewed": False,
        "deleted": False,
        "compaction_run": run_id,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    })


async def _update_existing_memory(
    db: Any,
    run_id: str,
    memory_id: str,
    mem: CompactedMemory,
    existing_memories: dict[str, dict[str, Any]],
) -> None:
    """Update an existing memory, preserving previous_content for rollback."""
    old = existing_memories[memory_id]
    doc_ref = db.collection("core_memories").document(memory_id)
    await doc_ref.update({
        "previous_content": old.get("content", ""),
        "content": mem.content,
        "category": mem.category,
        "confidence": mem.confidence,
        "importance": mem.importance,
        "source_journals": mem.source_journal_ids,
        "version": old.get("version", 1) + 1,
        "compaction_run": run_id,
        "updated_at": datetime.utcnow(),
    })


# ── Phase 4: Archive ──────────────────────────────────────────────────────

async def _phase_archive(
    db: Any,
    run: CompactionRun,
    journals: list[dict[str, Any]],
    consumed_journal_ids: set[str],
) -> None:
    """Archive journals that were successfully consumed by written memories."""
    archived_count = 0

    for journal in journals:
        j_id = journal.get("journal_id")
        if j_id not in consumed_journal_ids:
            continue

        try:
            # Copy to archive
            archive_data = {**journal}
            archive_data["archived_at"] = datetime.utcnow()
            archive_data["compaction_run"] = run.run_id
            await db.collection("journals_archive").document(j_id).set(archive_data)

            # Mark original as archived (never delete)
            await db.collection("journals").document(j_id).update({"archived": True})
            archived_count += 1

        except Exception as e:
            logger.exception("Failed to archive journal: %s", j_id)
            run.errors.append(CompactionError(
                stage="archive",
                message=str(e),
                journal_ids=[j_id],
                recoverable=True,
            ))

    run.journals_archived = archived_count
    logger.info("Phase 4 — Archive: %d journals archived", archived_count)


# ── Phase 5: Merge ─────────────────────────────────────────────────────────

async def _phase_merge(
    db: Any,
    config: CompactionConfig,
    run: CompactionRun,
) -> None:
    """Post-compaction pass: merge related memories into richer entries."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    # Load all active memories
    memories: dict[str, dict[str, Any]] = {}
    query = db.collection("core_memories").where(
        filter=FieldFilter("deleted", "==", False)
    ).limit(500)
    async for doc in query.stream():
        if doc.id == "_placeholder":
            continue
        memories[doc.id] = doc.to_dict()

    # Only merge if we have enough memories to be worth it
    if len(memories) < 3:
        logger.info("Phase 5 — Merge: skipped (only %d memories)", len(memories))
        return

    try:
        agent = Agent(
            config.model,
            output_type=MergeResult,
            system_prompt=MERGE_SYSTEM_PROMPT,
            defer_model_check=True,
        )

        prompt = build_merge_prompt(memories)
        result = await agent.run(prompt)
        merge_result: MergeResult = result.output

        if not merge_result.merge_groups:
            logger.info("Phase 5 — Merge: no merges needed")
            return

        merged_count = 0
        for group in merge_result.merge_groups:
            if len(group.memory_ids) < 2:
                continue

            # Verify all memory IDs exist
            valid_ids = [mid for mid in group.memory_ids if mid in memories]
            if len(valid_ids) < 2:
                continue

            # Keep the first memory ID as the survivor, delete the rest
            survivor_id = valid_ids[0]
            old_content = memories[survivor_id].get("content", "")

            # Update the survivor with merged content
            await db.collection("core_memories").document(survivor_id).update({
                "previous_content": old_content,
                "content": group.merged_content,
                "category": group.category,
                "importance": group.importance,
                "confidence": group.confidence,
                "version": memories[survivor_id].get("version", 1) + 1,
                "compaction_run": run.run_id,
                "updated_at": datetime.utcnow(),
            })

            # Soft-delete the other memories in the group
            for mid in valid_ids[1:]:
                await db.collection("core_memories").document(mid).update({
                    "deleted": True,
                    "merged_into": survivor_id,
                    "updated_at": datetime.utcnow(),
                })

            merged_count += 1
            logger.info(
                "Merged %d memories into %s: %s",
                len(valid_ids), survivor_id, group.merged_content[:80],
            )

        logger.info("Phase 5 — Merge: %d groups merged", merged_count)

    except Exception as e:
        logger.exception("Memory merge pass failed (non-fatal)")
        run.errors.append(CompactionError(
            stage="merge",
            message=str(e),
            recoverable=True,
        ))


# ── Audit Log ──────────────────────────────────────────────────────────────

async def _write_run_log(db: Any, run: CompactionRun) -> None:
    """Write the compaction run audit log to Firestore."""
    try:
        await db.collection("compaction_runs").document(run.run_id).set(
            run.model_dump()
        )
    except Exception:
        logger.exception("Failed to write compaction run log: %s", run.run_id)
