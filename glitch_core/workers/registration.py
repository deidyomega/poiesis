from __future__ import annotations

import logging
import socket
from datetime import datetime
from typing import Any

from glitch_core import __version__
from glitch_core.agents import _can_run_model
from glitch_core.config import GlitchEnv
from glitch_core.schemas import AgentConfig, WorkerRegistration

logger = logging.getLogger(__name__)


async def register_worker(
    db: Any,
    env: GlitchEnv,
    agent_configs: list[AgentConfig],
) -> WorkerRegistration:
    """Register this node as a worker in Firestore.

    Determines which agents this node can run based on available API keys
    and capabilities, then writes the registration to /workers/{worker_id}.
    """
    worker_id = env.node_name

    # Determine which agents this node supports
    supported_agents: list[str] = []
    node_caps = set(env.node_capabilities)

    for cfg in agent_configs:
        if not cfg.enabled:
            continue
        if not _can_run_model(cfg.model, env):
            continue
        required = set(cfg.required_capabilities)
        if required and not required.issubset(node_caps):
            continue
        supported_agents.append(cfg.agent_id)

    registration = WorkerRegistration(
        worker_id=worker_id,
        hostname=socket.gethostname(),
        node_name=env.node_name,
        capabilities=env.node_capabilities,
        supported_agents=supported_agents,
        glitch_version=__version__,
        status="online",
    )

    await db.collection("workers").document(worker_id).set(registration.model_dump())

    logger.info(
        "Registered worker '%s' — supports agents: %s",
        worker_id, supported_agents or "(none)",
    )

    return registration
