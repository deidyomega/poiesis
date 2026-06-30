from __future__ import annotations

import asyncio
import logging

import click

from glitch_core.config import GlitchEnv

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


@click.group()
@click.version_option(package_name="glitch-core")
def cli() -> None:
    """Glitch — single-user self-hosted AI."""


@cli.command()
@click.option("--password", default=None, help="Set the admin password (bcrypt-hashed into ~/.glitch/.env).")
def bootstrap(password: str | None) -> None:
    """Idempotent first-run setup: ~/.glitch, .env scaffold, SQLite seed."""
    _setup_logging()
    from glitch_core.bootstrap import bootstrap as run_bootstrap

    asyncio.run(run_bootstrap(admin_password=password))
    click.echo("Bootstrap complete.")
    if not password:
        click.echo("Set an admin password with:  glitch bootstrap --password <pw>")


@cli.command()
@click.option("--app-only", is_flag=True, help="Run only the web app (no supervisor / no self-mod restart).")
def start(app_only: bool) -> None:
    """Start Glitch (supervisor + web app, with self-mod deploy)."""
    _setup_logging()
    env = GlitchEnv()
    if app_only:
        import uvicorn

        uvicorn.run("glitch_core.asgi:app", host=env.host, port=env.port)
    else:
        from glitch_core.supervisor import run_supervisor

        asyncio.run(run_supervisor(env))


@cli.command("hash-password")
@click.argument("password")
def hash_password_cmd(password: str) -> None:
    """Print a bcrypt hash for a password (for GLITCH_ADMIN_PASSWORD_HASH)."""
    from glitch_core.web.auth import hash_password

    click.echo(hash_password(password))
