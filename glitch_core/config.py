from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from google.cloud.firestore_v1 import AsyncClient
from google.oauth2 import service_account
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from glitch_core.schemas import GlitchConfig

logger = logging.getLogger(__name__)

GLITCH_HOME = Path.home() / ".glitch"
CONFIG_DIR = GLITCH_HOME


class GlitchEnv(BaseSettings):
    """Machine-local environment configuration read from ~/.glitch/.env."""

    model_config = SettingsConfigDict(
        env_prefix="GLITCH_",
        env_file=str(GLITCH_HOME / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    firebase_project: str
    firebase_credentials: Path = GLITCH_HOME / "credentials.json"
    gemini_api_key: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    mistral_api_key: str | None = None
    groq_api_key: str | None = None
    ollama_host: str | None = None
    node_name: str = "main"
    node_capabilities: list[str] = Field(default_factory=lambda: ["api"])


def load_yaml_config(path: Path | None = None) -> GlitchConfig:
    """Load and validate glitch_core.yaml into a GlitchConfig model."""
    if path is None:
        path = Path(__file__).parent.parent / "glitch_core.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Agent config not found: {path}")
    with open(path) as f:
        raw: dict[str, Any] = yaml.safe_load(f)
    return GlitchConfig.model_validate(raw)


# Fallback default agent ID — used when Firestore isn't reachable yet
DEFAULT_AGENT_ID = "router"


async def get_default_agent_id(db: AsyncClient) -> str:
    """Read the default agent ID from /meta/project. Falls back to 'router'."""
    try:
        doc = await db.collection("meta").document("project").get()
        if doc.exists:
            return doc.to_dict().get("default_agent", DEFAULT_AGENT_ID)
    except Exception:
        pass
    return DEFAULT_AGENT_ID


def find_firebase_bin() -> str | None:
    """Find the firebase CLI binary, including nvm/npm paths.

    Also adds the node bin directory to PATH so subprocess calls work
    (firebase needs node in PATH to execute).
    """
    import os
    import shutil

    fb = shutil.which("firebase")
    if fb:
        return fb

    # Check nvm node versions
    nvm_dir = Path.home() / ".nvm" / "versions" / "node"
    if nvm_dir.exists():
        for node_dir in sorted(nvm_dir.iterdir(), reverse=True):
            candidate = node_dir / "bin" / "firebase"
            if candidate.exists():
                # Add node bin to PATH so firebase subprocess can find node
                node_bin = str(node_dir / "bin")
                if node_bin not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = node_bin + os.pathsep + os.environ.get("PATH", "")
                return str(candidate)

    return None


def get_firestore_client(env: GlitchEnv | None = None) -> AsyncClient:
    """Create an async Firestore client from the service account credentials."""
    if env is None:
        env = GlitchEnv()
    creds = service_account.Credentials.from_service_account_file(
        str(env.firebase_credentials)
    )
    return AsyncClient(project=env.firebase_project, credentials=creds)
