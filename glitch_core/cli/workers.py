from __future__ import annotations

import asyncio
import logging
import os

import click


@click.command("start")
def worker_start_cmd() -> None:
    """Start a standalone worker daemon (no web UI, no chat listener)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Export API keys
    from glitch_core.config import GlitchEnv
    env = GlitchEnv()
    if env.anthropic_api_key and "ANTHROPIC_API_KEY" not in os.environ:
        os.environ["ANTHROPIC_API_KEY"] = env.anthropic_api_key
    if env.gemini_api_key and "GEMINI_API_KEY" not in os.environ:
        os.environ["GEMINI_API_KEY"] = env.gemini_api_key
    if env.openai_api_key and "OPENAI_API_KEY" not in os.environ:
        os.environ["OPENAI_API_KEY"] = env.openai_api_key
    if env.mistral_api_key and "MISTRAL_API_KEY" not in os.environ:
        os.environ["MISTRAL_API_KEY"] = env.mistral_api_key
    if env.groq_api_key and "GROQ_API_KEY" not in os.environ:
        os.environ["GROQ_API_KEY"] = env.groq_api_key
    if env.ollama_host and "OLLAMA_BASE_URL" not in os.environ:
        os.environ["OLLAMA_BASE_URL"] = f"{env.ollama_host.rstrip('/')}/v1"

    click.echo(f"Starting worker on node: {env.node_name}")

    async def _run() -> None:
        from glitch_core.agents import build_agent_registry, load_agents_from_firestore
        from glitch_core.config import get_firestore_client
        from glitch_core.workers.loop import WorkerDaemon

        db = get_firestore_client(env)
        agent_configs = await load_agents_from_firestore(db)

        if not agent_configs:
            click.echo("No agents found in Firestore. Run 'glitch bootstrap' first.")
            db.close()
            return

        registry = build_agent_registry(agent_configs, env)

        if not registry:
            click.echo("No agents available for this node's capabilities/keys. Nothing to do.")
            db.close()
            return

        click.echo(f"Agents: {list(registry.keys())}")

        worker = WorkerDaemon(db=db, env=env, agent_configs=agent_configs, agent_registry=registry)
        try:
            await worker.run()
        finally:
            db.close()

    asyncio.run(_run())


@click.command("status")
def worker_status_cmd() -> None:
    """Show all registered workers and their status."""
    logging.basicConfig(level=logging.WARNING)

    async def _status() -> None:
        from datetime import datetime, timedelta, timezone

        from glitch_core.config import GlitchEnv, get_firestore_client

        env = GlitchEnv()
        db = get_firestore_client(env)
        now = datetime.now(timezone.utc)

        count = 0
        async for doc in db.collection("workers").stream():
            if doc.id == "_placeholder":
                continue
            data = doc.to_dict()
            count += 1

            name = data.get("node_name", doc.id)
            caps = data.get("capabilities", [])
            agents = data.get("supported_agents", [])
            version = data.get("glitch_version", "?")
            task = data.get("current_task")

            last_hb = data.get("last_heartbeat")
            if last_hb and isinstance(last_hb, datetime):
                hb_aware = last_hb if last_hb.tzinfo else last_hb.replace(tzinfo=timezone.utc)
                age = now - hb_aware
                online = age < timedelta(minutes=2)
                hb_str = f"{int(age.total_seconds())}s ago"
            else:
                online = False
                hb_str = "never"

            status_icon = "🟢" if online else "🔴"
            task_str = f" (working on {task})" if task else ""

            click.echo(f"  {status_icon} {name} v{version} — {', '.join(caps) or 'no caps'}")
            click.echo(f"     Agents: {', '.join(agents) or 'none'}")
            click.echo(f"     Heartbeat: {hb_str}{task_str}")

        if count == 0:
            click.echo("  No workers registered.")

        db.close()

    asyncio.run(_status())
