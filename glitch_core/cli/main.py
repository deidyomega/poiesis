from __future__ import annotations

import asyncio
import logging

import click


@click.command()
def start_cmd() -> None:
    """Start the Glitch Core daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    click.echo("Starting Glitch Core daemon...")

    # Export API keys before any PydanticAI imports touch the env
    import os
    from glitch_core.config import GlitchEnv
    env = GlitchEnv()
    if env.anthropic_api_key and "ANTHROPIC_API_KEY" not in os.environ:
        os.environ["ANTHROPIC_API_KEY"] = env.anthropic_api_key
    if env.gemini_api_key and "GEMINI_API_KEY" not in os.environ:
        os.environ["GEMINI_API_KEY"] = env.gemini_api_key
    if env.ollama_host and "OLLAMA_BASE_URL" not in os.environ:
        os.environ["OLLAMA_BASE_URL"] = env.ollama_host

    from glitch_core.daemon import run_daemon

    asyncio.run(run_daemon())


@click.command()
def bootstrap_cmd() -> None:
    """Run first-time Firestore initialization."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    click.echo("Running Glitch Core bootstrap...")

    from glitch_core.bootstrap import bootstrap

    asyncio.run(bootstrap())
    click.echo("Bootstrap complete.")


@click.command()
@click.confirmation_option(prompt="This will DELETE ALL DATA in Firestore and reset to a clean state. Are you sure?")
def nuke_cmd() -> None:
    """Wipe the Firestore database and reset to clean state."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    import subprocess
    from pathlib import Path

    from glitch_core.config import find_firebase_bin

    repo_root = Path(__file__).parent.parent.parent
    firebase_bin = find_firebase_bin()

    if not firebase_bin:
        click.echo("Firebase CLI not found. Install it: npm install -g firebase-tools")
        return

    # 1. Delete all Firestore data via Firebase CLI (server-side, no quota hit)
    click.echo("Deleting all Firestore data...")
    try:
        result = subprocess.run(
            [firebase_bin, "firestore:delete", "--all-collections", "--force"],
            capture_output=True, text=True, timeout=120,
            cwd=str(repo_root),
        )
        if result.returncode == 0:
            click.echo("  All collections deleted.")
        else:
            click.echo(f"  firebase firestore:delete failed: {result.stderr.strip()}")
            click.echo("  You may need to run: firebase firestore:delete --all-collections --force")
            return
    except Exception as e:
        click.echo(f"  Error: {e}")
        return

    # 2. Reset firestore.rules to deny-all
    rules_path = repo_root / "firestore.rules"
    rules_path.write_text(
        'rules_version = \'2\';\n'
        'service cloud.firestore {\n'
        '  match /databases/{database}/documents {\n'
        '    match /{document=**} {\n'
        '      allow read, write: if false;\n'
        '    }\n'
        '  }\n'
        '}\n'
    )
    click.echo("  Reset firestore.rules to deny-all")

    # 3. Deploy deny-all rules
    try:
        result = subprocess.run(
            [firebase_bin, "deploy", "--only", "firestore:rules"],
            capture_output=True, text=True, timeout=30,
            cwd=str(repo_root),
        )
        if result.returncode == 0:
            click.echo("  Deployed deny-all rules to Firebase")
        else:
            click.echo("  Could not deploy rules — run 'firebase deploy --only firestore:rules' manually")
    except Exception:
        click.echo("  Could not deploy rules — deploy manually")

    click.echo("\nDatabase nuked. Run 'glitch bootstrap' to reinitialize.")


@click.command()
def status_cmd() -> None:
    """Show system status."""
    import json
    from pathlib import Path

    config_path = Path.home() / ".glitch" / "config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        click.echo(f"Glitch Core v{config.get('version', 'unknown')}")
        click.echo(f"Firebase Project: {config.get('firebase_project', 'not set')}")
    else:
        click.echo("Glitch Core is not configured. Run 'glitch bootstrap' first.")
        return

    # Try to connect and show worker status
    try:
        from glitch_core.config import GlitchEnv, get_firestore_client

        env = GlitchEnv()
        db = get_firestore_client(env)

        async def _status() -> None:
            worker_count = 0
            async for doc in db.collection("workers").stream():
                if doc.id != "_placeholder":
                    data = doc.to_dict()
                    status = "online" if data.get("online") else "offline"
                    click.echo(f"  Worker: {data.get('node_name', doc.id)} [{status}]")
                    worker_count += 1

            if worker_count == 0:
                click.echo("  No workers registered.")

            db.close()

        asyncio.run(_status())
    except Exception as e:
        click.echo(f"Could not connect to Firestore: {e}")
