from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from google.api_core import exceptions as gcp_exceptions
from google.cloud.firestore_admin_v1 import FirestoreAdminClient
from google.cloud.firestore_admin_v1.types import Database
from google.oauth2 import service_account

from glitch_core.config import GLITCH_HOME, GlitchEnv, get_firestore_client, load_yaml_config
from glitch_core.schemas import CompactionConfig, FeatureFlags, ProjectMeta
from glitch_core.web.theming import PRESET_THEMES

logger = logging.getLogger(__name__)

FIRESTORE_RULES = """\
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    // Browser gets read-only access for real-time listeners (onSnapshot).
    // All writes go through the daemon's Admin SDK which bypasses rules.

    match /sessions/{sessionId} {
      allow read: if true;
      allow write: if false;

      match /messages/{messageId} {
        allow read: if true;
        allow write: if false;
      }
      match /sub_tasks/{taskId} {
        allow read: if true;
        allow write: if false;
      }
      match /run_logs/{logId} {
        allow read: if true;
        allow write: if false;
      }
    }

    match /agents/{agentId} {
      allow read: if true;
      allow write: if false;
    }

    match /meta/{docId} {
      allow read: if true;
      allow write: if false;
    }

    // Everything else: deny from browser
    match /{document=**} {
      allow read, write: if false;
    }
  }
}
"""

DEFAULT_SOUL = """# Soul — Glitch Core

You are Glitch, a personal AI assistant. You are direct, technical but not condescending, and you remember context from previous conversations.

## Personality
- Concise and helpful. Don't over-explain unless asked.
- Honest about uncertainty. Never fabricate memories or facts.
- Proactive about offering relevant context from your memory.
- Able to delegate complex tasks to specialized sub-agents when needed.

## Directives
- Always check your core memories for relevant context before responding.
- Log interesting observations to your journal during conversations.
- When a task requires code generation, research, or system administration, delegate to the appropriate sub-agent.
- Never pretend to remember something you don't. If a memory doesn't exist, say so.
- Respect the user's time. Be brief unless depth is requested.
"""


def _ensure_firestore_database(env: GlitchEnv) -> None:
    """Create the Firestore (default) database if it doesn't exist."""
    creds = service_account.Credentials.from_service_account_file(
        str(env.firebase_credentials)
    )
    client = FirestoreAdminClient(credentials=creds)
    parent = f"projects/{env.firebase_project}"
    db_name = f"{parent}/databases/(default)"

    # Check if database already exists
    try:
        client.get_database(name=db_name)
        logger.info("Firestore database already exists.")
        return
    except gcp_exceptions.NotFound:
        pass
    except gcp_exceptions.PermissionDenied:
        # Service account can't even check — fall through to creation attempt
        pass

    logger.info("Firestore database not found — creating it now...")
    try:
        operation = client.create_database(
            parent=parent,
            database=Database(
                location_id="nam5",  # US multi-region, free tier eligible
                type_=Database.DatabaseType.FIRESTORE_NATIVE,
            ),
            database_id="(default)",
        )

        # Poll until the long-running operation completes
        logger.info("Waiting for database creation (this may take a minute)...")
        result = operation.result(timeout=120)
        logger.info("Firestore database created: %s", result.name)

    except gcp_exceptions.PermissionDenied:
        logger.error(
            "\n"
            "╔══════════════════════════════════════════════════════════════╗\n"
            "║  Could not create the Firestore database automatically.    ║\n"
            "║  Your service account doesn't have permission.             ║\n"
            "║                                                            ║\n"
            "║  Create it manually (takes 30 seconds):                    ║\n"
            "║  https://console.firebase.google.com/project/%s/firestore  ║\n"
            "║                                                            ║\n"
            "║  1. Click 'Create database'                                ║\n"
            "║  2. Choose 'Native mode'                                   ║\n"
            "║  3. Pick a location (nam5 for US)                          ║\n"
            "║  4. Click 'Create'                                         ║\n"
            "║  5. Re-run: glitch bootstrap                               ║\n"
            "╚══════════════════════════════════════════════════════════════╝",
            env.firebase_project,
        )
        raise SystemExit(1)

    except gcp_exceptions.AlreadyExists:
        logger.info("Firestore database already exists (race-safe).")


async def bootstrap(env: GlitchEnv | None = None) -> None:
    """Initialize Firestore with default documents for a fresh installation."""
    if env is None:
        env = GlitchEnv()

    # Step 0: ensure the Firestore database exists
    _ensure_firestore_database(env)

    db = get_firestore_client(env)

    logger.info("Bootstrapping Firestore for project: %s", env.firebase_project)

    # 1. /meta/project
    project_meta = ProjectMeta(
        version="0.1.0",
        schema_version=1,
        firebase_project=env.firebase_project,
        feature_flags=FeatureFlags(),
    )
    await db.collection("meta").document("project").set(project_meta.model_dump())
    logger.info("Created /meta/project")

    # 2. /agents/{id} — seed from glitch_core.yaml + default system prompts
    from glitch_core.agents import DEFAULT_PROMPTS
    config = load_yaml_config()

    # Seed the router as an agent like any other — its soul is its system_prompt
    router_data = config.router.model_dump()
    router_data["system_prompt"] = DEFAULT_SOUL
    router_data["output_type"] = "text"
    router_data.pop("output_schema", None)
    router_data["created_at"] = datetime.utcnow()
    router_data["updated_at"] = datetime.utcnow()
    await db.collection("agents").document("router").set(router_data)
    logger.info("Created /agents/router")

    # Seed each worker agent
    for agent_cfg in config.agents:
        agent_data = agent_cfg.model_dump()
        # Inject default system prompt if not already set
        if not agent_data.get("system_prompt"):
            agent_data["system_prompt"] = DEFAULT_PROMPTS.get(agent_cfg.agent_id, "")
        # Add output_type mapping from the old output_schema field
        if not agent_data.get("output_type") or agent_data["output_type"] == "text":
            type_map = {
                "CodeArtifact": "code_artifact",
                "ResearchResult": "research_result",
                "CommandResult": "command_result",
            }
            old_schema = agent_data.pop("output_schema", None)
            if old_schema and old_schema in type_map:
                agent_data["output_type"] = type_map[old_schema]
        agent_data.pop("output_schema", None)
        agent_data["created_at"] = datetime.utcnow()
        agent_data["updated_at"] = datetime.utcnow()
        await db.collection("agents").document(agent_cfg.agent_id).set(agent_data)
        logger.info("Created /agents/%s", agent_cfg.agent_id)

    # 3. /meta/compaction_config
    compaction = CompactionConfig()
    await db.collection("meta").document("compaction_config").set(compaction.model_dump())
    logger.info("Created /meta/compaction_config")

    # 5. /meta/theme
    default_theme = PRESET_THEMES["default"]
    await db.collection("meta").document("theme").set(default_theme.model_dump())
    logger.info("Created /meta/theme")

    # 6. Seed empty collections with placeholder docs
    placeholder = {"_placeholder": True}
    for collection_name in [
        "sessions", "journals", "journals_archive", "core_memories",
        "memories_deleted", "compaction_runs",
        "workers", "theme_history",
    ]:
        await db.collection(collection_name).document("_placeholder").set(placeholder)
        logger.info("Seeded %s with placeholder", collection_name)

    # 7. Write ~/.glitch/config.json
    config_json = GLITCH_HOME / "config.json"
    config_json.write_text(json.dumps({
        "firebase_project": env.firebase_project,
        "version": "0.1.0",
    }, indent=2))
    logger.info("Wrote %s", config_json)

    # 8. Write proper security rules + deploy via Firebase CLI
    import subprocess
    from glitch_core.config import find_firebase_bin
    repo_root = Path(__file__).parent.parent

    # Ensure firestore.rules has the production rules (nuke resets to deny-all)
    rules_path = repo_root / "firestore.rules"
    rules_path.write_text(FIRESTORE_RULES)
    logger.info("Wrote firestore.rules")

    firebase_bin = find_firebase_bin()
    if firebase_bin:
        try:
            result = subprocess.run(
                [firebase_bin, "deploy", "--only", "firestore"],
                capture_output=True, text=True, timeout=120,
                cwd=str(repo_root),
            )
            if result.returncode == 0:
                logger.info("Deployed Firestore rules + indexes")
            else:
                logger.warning(
                    "Firebase deploy failed (run manually: firebase deploy --only firestore)\n%s",
                    result.stderr.strip(),
                )
        except Exception:
            logger.warning("Could not deploy Firestore config — deploy manually")
    else:
        logger.warning("Firebase CLI not found — deploy rules + indexes manually: firebase deploy --only firestore")

    db.close()
    logger.info("Bootstrap complete.")


def main() -> None:
    """Entry point for `python -m glitch_core.bootstrap`."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    asyncio.run(bootstrap())


if __name__ == "__main__":
    main()
