from __future__ import annotations

import asyncio
import logging
import os

import click


@click.command("run")
@click.option("--dry-run/--no-dry-run", default=True, help="Preview without writing (default: dry-run).")
@click.option("--force", is_flag=True, help="Run for real (shortcut for --no-dry-run).")
def compaction_run_cmd(dry_run: bool, force: bool) -> None:
    """Run memory compaction on unarchived journal entries."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if force:
        dry_run = False

    # Export API keys
    from glitch_core.config import GlitchEnv
    env = GlitchEnv()
    if env.anthropic_api_key and "ANTHROPIC_API_KEY" not in os.environ:
        os.environ["ANTHROPIC_API_KEY"] = env.anthropic_api_key
    if env.gemini_api_key and "GEMINI_API_KEY" not in os.environ:
        os.environ["GEMINI_API_KEY"] = env.gemini_api_key
    if env.ollama_host and "OLLAMA_BASE_URL" not in os.environ:
        os.environ["OLLAMA_BASE_URL"] = env.ollama_host

    async def _run() -> None:
        from glitch_core.compaction.pipeline import run_compaction
        from glitch_core.config import get_firestore_client
        from glitch_core.schemas import CompactionConfig

        db = get_firestore_client(env)

        # Load config and override dry_run
        doc = await db.collection("meta").document("compaction_config").get()
        config = CompactionConfig.model_validate(doc.to_dict()) if doc.exists else CompactionConfig()
        config.dry_run = dry_run
        if force:
            config.min_journals_to_trigger = 1

        click.echo(f"Running compaction ({'DRY RUN' if dry_run else 'LIVE'})...")
        result = await run_compaction(db, config)

        click.echo(f"\nResult: {result.status}")
        click.echo(f"  Journals read: {result.journals_read}")
        click.echo(f"  Journals archived: {result.journals_archived}")
        click.echo(f"  Memories created: {result.memories_created}")
        click.echo(f"  Memories updated: {result.memories_updated}")
        click.echo(f"  Memories flagged for review: {result.memories_flagged}")
        if result.errors:
            click.echo(f"  Errors: {len(result.errors)}")
            for err in result.errors:
                click.echo(f"    [{err.stage}] {err.message}")

        db.close()

    asyncio.run(_run())


@click.command("status")
@click.option("--limit", "-n", default=10, help="Number of recent runs to show.")
def compaction_status_cmd(limit: int) -> None:
    """Show recent compaction run history."""
    logging.basicConfig(level=logging.WARNING)

    async def _status() -> None:
        from glitch_core.config import GlitchEnv, get_firestore_client

        env = GlitchEnv()
        db = get_firestore_client(env)

        runs = []
        query = (
            db.collection("compaction_runs")
            .order_by("started_at", direction="DESCENDING")
            .limit(limit)
        )
        async for doc in query.stream():
            if doc.id == "_placeholder":
                continue
            data = doc.to_dict()
            runs.append(data)

        if not runs:
            click.echo("No compaction runs yet.")
        else:
            for r in runs:
                status = r.get("status", "?")
                started = r.get("started_at", "?")
                created = r.get("memories_created", 0)
                updated = r.get("memories_updated", 0)
                flagged = r.get("memories_flagged", 0)
                journals = r.get("journals_read", 0)
                errors = len(r.get("errors", []))
                click.echo(
                    f"  {r.get('run_id', '?')}: {status} — "
                    f"{journals} journals, {created} created, {updated} updated, "
                    f"{flagged} flagged, {errors} errors "
                    f"(started {started})"
                )

        db.close()

    asyncio.run(_status())


@click.command("rollback")
@click.argument("run_id")
@click.confirmation_option(prompt="Are you sure you want to rollback this compaction run?")
def compaction_rollback_cmd(run_id: str) -> None:
    """Rollback a compaction run, restoring journals and reverting memories."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async def _rollback() -> None:
        from glitch_core.compaction.rollback import rollback_compaction_run
        from glitch_core.config import GlitchEnv, get_firestore_client

        env = GlitchEnv()
        db = get_firestore_client(env)

        success = await rollback_compaction_run(db, run_id)
        if success:
            click.echo(f"Compaction run {run_id} rolled back successfully.")
        else:
            click.echo(f"Failed to rollback run {run_id}. Check logs for details.")

        db.close()

    asyncio.run(_rollback())
