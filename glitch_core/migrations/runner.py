from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from glitch_core.db import Database

logger = logging.getLogger(__name__)

VERSIONS_DIR = Path(__file__).parent / "versions"


async def run_migrations(db: Database, versions_dir: Path | None = None) -> list[str]:
    """Apply any pending .sql migrations in order. Idempotent.

    Returns the list of migration filenames applied this run.
    """
    versions_dir = versions_dir or VERSIONS_DIR

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )

    applied = {
        row["version"] for row in await db.fetch_all("SELECT version FROM schema_migrations")
    }

    files = sorted(p for p in versions_dir.glob("*.sql"))
    newly_applied: list[str] = []
    for path in files:
        version = path.stem
        if version in applied:
            continue
        logger.info("Applying migration %s", version)
        await db.executescript(path.read_text())
        await db.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat()),
        )
        newly_applied.append(version)

    if newly_applied:
        logger.info("Applied %d migration(s): %s", len(newly_applied), ", ".join(newly_applied))
    else:
        logger.info("Database schema up to date")
    return newly_applied
