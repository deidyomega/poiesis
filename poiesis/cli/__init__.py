from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import click

from poiesis.config import PoiesisEnv

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _setup_logging() -> None:
    # Always mirror to a durable file so a post-mortem (esp. blank/errored turns)
    # doesn't depend on the launch method — under `--app-only` in screen there's no
    # journald, and screen's scrollback is short and volatile.
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_dir = Path.home() / ".poiesis"
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(log_dir / "poiesis.log", maxBytes=5_000_000, backupCount=3)
        )
    except OSError:
        pass  # stream-only is an acceptable fallback
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=handlers)


@click.group()
@click.version_option(package_name="poiesis")
def cli() -> None:
    """Poiesis — single-user self-hosted AI."""


@cli.command()
@click.option("--password", default=None, help="Set the admin password (bcrypt-hashed into ~/.poiesis/.env).")
def bootstrap(password: str | None) -> None:
    """Idempotent first-run setup: ~/.poiesis, .env scaffold, SQLite seed."""
    _setup_logging()
    from poiesis.bootstrap import bootstrap as run_bootstrap

    asyncio.run(run_bootstrap(admin_password=password))
    click.echo("Bootstrap complete.")
    if not password:
        click.echo("Set an admin password with:  poiesis bootstrap --password <pw>")


@cli.command()
@click.option("--app-only", is_flag=True, help="Run only the web app (no supervisor / no self-mod restart).")
def start(app_only: bool) -> None:
    """Start Poiesis (supervisor + web app, with self-mod deploy)."""
    _setup_logging()
    env = PoiesisEnv()
    if app_only:
        import uvicorn

        uvicorn.run("poiesis.asgi:app", host=env.host, port=env.port)
    else:
        from poiesis.supervisor import run_supervisor

        asyncio.run(run_supervisor(env))


@cli.command("hash-password")
@click.argument("password")
def hash_password_cmd(password: str) -> None:
    """Print a bcrypt hash for a password (for POIESIS_ADMIN_PASSWORD_HASH)."""
    from poiesis.web.auth import hash_password

    click.echo(hash_password(password))
