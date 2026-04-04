#!/usr/bin/env python3
"""Quick wipe of journals + memories. Uses batch deletes to minimize operations."""
import asyncio
from glitch_core.config import GlitchEnv, get_firestore_client


async def wipe():
    env = GlitchEnv()
    db = get_firestore_client(env)

    collections = [
        "journals",
        "journals_archive",
        "core_memories",
        "memories_deleted",
        "compaction_runs",
    ]

    for coll in collections:
        batch = db.batch()
        count = 0
        async for doc in db.collection(coll).stream():
            if doc.id == "_placeholder":
                continue
            batch.delete(db.collection(coll).document(doc.id))
            count += 1
            # Firestore batches max 500 ops
            if count % 500 == 0:
                await batch.commit()
                batch = db.batch()
        if count % 500 != 0:
            await batch.commit()
        if count:
            print(f"  {coll}: {count} deleted")

    print("Done.")
    db.close()


asyncio.run(wipe())
