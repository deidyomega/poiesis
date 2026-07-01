"""Integration test for the crown-jewel safety net: supervised deploy + rollback.

Runs against a throwaway git repo + a tiny standalone uvicorn app (never the real
repo), exercising Supervisor._handle_deploy for both the healthy and broken cases.
"""

from __future__ import annotations

import os
import socket
import subprocess

import pytest

from poiesis import gitops, store
from poiesis.config import PoiesisEnv
from poiesis.db import Database
from poiesis.migrations.runner import run_migrations
from poiesis.supervisor import Supervisor

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}

GOOD_APP = (
    "from fastapi import FastAPI\n"
    "app = FastAPI()\n"
    "@app.get('/healthz')\n"
    "async def h():\n"
    "    return {'status': 'ok'}\n"
)
BAD_APP = "this is not valid python @@@\n"


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, check=True, capture_output=True)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.asyncio
async def test_deploy_live_then_rollback(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tmpapp.py").write_text(GOOD_APP)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    green0 = gitops.current_sha(repo)

    db_path = tmp_path / "t.db"
    env = PoiesisEnv(
        db_path=db_path, repo_root=repo, host="127.0.0.1", port=_free_port(),
        session_secret="x" * 40,
    )
    seed = Database(db_path)
    await seed.connect()
    await run_migrations(seed)
    await store.set_setting(seed, "last_green_sha", green0)
    await seed.close()

    sup = Supervisor(env, app_target="tmpapp:app")
    await sup.db.connect()
    try:
        await sup._start_app()
        assert await sup._healthy(timeout=30), "app should boot healthy"

        # ── Happy deploy: a benign committed change goes live ──
        (repo / "marker.txt").write_text("v1")
        target = gitops.commit_all(repo, "add marker")
        did = await store.create_deploy(
            sup.db, channel_id="general", message_id=None, summary="add marker",
            target_sha=target, rollback_sha=green0,
        )
        await sup._handle_deploy(await store.get_deploy(sup.db, did))
        assert (await store.get_deploy(sup.db, did))["status"] == "live"
        assert await store.get_setting(sup.db, "last_green_sha") == target

        # ── Broken deploy: app won't boot → rollback to last-green ──
        (repo / "tmpapp.py").write_text(BAD_APP)
        bad = gitops.commit_all(repo, "break app")
        did2 = await store.create_deploy(
            sup.db, channel_id="general", message_id=None, summary="break",
            target_sha=bad, rollback_sha=target,
        )
        await sup._handle_deploy(await store.get_deploy(sup.db, did2))
        assert (await store.get_deploy(sup.db, did2))["status"] == "rolled_back"
        # Working tree restored to the good commit, and the app is healthy again.
        assert (repo / "tmpapp.py").read_text().startswith("from fastapi")
        assert gitops.current_sha(repo) == target
        assert await sup._healthy(timeout=30)
    finally:
        await sup._stop_app()
        await sup.db.close()
