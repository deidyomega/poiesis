from __future__ import annotations

import click

from glitch_core.cli.main import bootstrap_cmd, nuke_cmd, start_cmd, status_cmd
from glitch_core.cli.compaction import compaction_run_cmd, compaction_status_cmd, compaction_rollback_cmd
from glitch_core.cli.workers import worker_start_cmd, worker_status_cmd


@click.group()
@click.version_option(package_name="glitch-core")
def cli() -> None:
    """Glitch Core — distributed self-hosted AI system."""


cli.add_command(start_cmd, "start")
cli.add_command(bootstrap_cmd, "bootstrap")
cli.add_command(status_cmd, "status")
cli.add_command(nuke_cmd, "nuke")


@cli.group()
def compaction() -> None:
    """Manage memory compaction pipeline."""


compaction.add_command(compaction_run_cmd, "run")
compaction.add_command(compaction_status_cmd, "status")
compaction.add_command(compaction_rollback_cmd, "rollback")


# Deferred subcommand groups
@cli.group()
def update() -> None:
    """Update Glitch Core. (Coming soon)"""


@cli.group()
def workers() -> None:
    """Manage worker nodes."""


workers.add_command(worker_start_cmd, "start")
workers.add_command(worker_status_cmd, "status")


@cli.group()
def pages() -> None:
    """Manage custom pages. (Coming soon)"""
