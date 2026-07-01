"""Process supervisor: runs the web app and owns the self-mod deploy + rollback.

This is the stable outer ring — it is NOT self-modified at runtime. It starts the
app (uvicorn) as a child, watches the `deploys` table for self-mod requests, and on
each one restarts the app, health-checks it, and hard-resets the repo to the
last-green commit if the new code fails to boot.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import httpx

from poiesis import gitops, store
from poiesis.config import PoiesisEnv
from poiesis.db import Database

logger = logging.getLogger(__name__)


class Supervisor:
    def __init__(self, env: PoiesisEnv, app_target: str = "poiesis.asgi:app") -> None:
        self.env = env
        self.app_target = app_target
        self.db = Database(env.db_path)
        self.proc: asyncio.subprocess.Process | None = None
        self._url = f"http://{env.host}:{env.port}"

    async def _start_app(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "uvicorn", self.app_target,
            "--host", self.env.host, "--port", str(self.env.port),
            "--app-dir", str(self.env.repo_root), "--no-access-log",
        )
        logger.info("Started app (pid %s) on %s", self.proc.pid, self._url)

    async def _stop_app(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()

    async def _restart_app(self) -> None:
        await self._stop_app()
        await self._start_app()

    async def _healthy(self, timeout: float = 45.0) -> bool:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        async with httpx.AsyncClient() as client:
            while loop.time() < deadline:
                if self.proc and self.proc.returncode is not None:
                    return False  # child exited (import error / crash on boot)
                try:
                    r = await client.get(f"{self._url}/healthz", timeout=2.0)
                    if r.status_code == 200:
                        return True
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1.0)
        return False

    async def _handle_deploy(self, d: dict) -> None:
        did, rollback, target = d["id"], d.get("rollback_sha"), d.get("target_sha")
        summary = d.get("summary") or "self-mod"
        logger.info("Deploy %s requested: %s", did, summary)

        await store.update_deploy(self.db, did, status="restarting")
        await self._restart_app()
        await store.update_deploy(self.db, did, status="health_check")

        if await self._healthy():
            await store.update_deploy(self.db, did, status="live")
            if target:
                await store.set_setting(self.db, "last_green_sha", target)
            logger.info("Deploy %s live", did)
            return

        logger.error("Deploy %s failed health check — rolling back to %s", did, (rollback or "?")[:8])
        if rollback and gitops.reset_hard(self.env.repo_root, rollback):
            await self._restart_app()
            await self._healthy()
            await store.update_deploy(
                self.db, did, status="rolled_back",
                reason="New code failed to boot; reverted to last-green.",
            )
        else:
            await store.update_deploy(
                self.db, did, status="failed",
                reason="Health check failed and no rollback target was available.",
            )

    async def run(self) -> None:
        await self.db.connect()
        await self._start_app()
        if not await self._healthy(timeout=60):
            logger.error("App did not become healthy on initial boot")

        logger.info("Supervisor watching for deploys")
        while True:
            try:
                for d in await store.pending_deploys(self.db):
                    await self._handle_deploy(d)
                if self.proc and self.proc.returncode is not None:
                    logger.warning("App exited (code %s) — restarting", self.proc.returncode)
                    await self._start_app()
                    await self._healthy()
            except Exception:
                logger.exception("Supervisor loop error")
            await asyncio.sleep(1.0)


async def run_supervisor(env: PoiesisEnv | None = None) -> None:
    sup = Supervisor(env or PoiesisEnv())
    try:
        await sup.run()
    finally:
        await sup._stop_app()
        await sup.db.close()
