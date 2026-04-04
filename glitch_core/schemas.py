from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────

class TaskStatus(str, enum.Enum):
    """Lifecycle state of a sub-agent task."""
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskCommand(str, enum.Enum):
    """High-level command categories the router can issue."""
    CODE = "code"
    RESEARCH = "research"
    SYSADMIN = "sysadmin"
    SPICY = "spicy"
    CUSTOM = "custom"


class ModelTier(str, enum.Enum):
    """Model cost/capability tiers."""
    FAST = "fast"
    BALANCED = "balanced"
    HEAVY = "heavy"
    LOCAL = "local"


class MessageRole(str, enum.Enum):
    """Who authored a chat message."""
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
    SUB_AGENT = "sub_agent"


class ContentRating(str, enum.Enum):
    """Content safety classification."""
    SFW = "sfw"
    NSFW = "nsfw"


class TaskAffinity(str, enum.Enum):
    """How strictly a task must be routed to a specific worker type."""
    ANY = "any"
    PREFERRED = "preferred"
    EXCLUSIVE = "exclusive"


class WorkerCapability(str, enum.Enum):
    """What a worker node can do."""
    API = "api"
    LOCAL = "local"
    GPU = "gpu"
    TAILNET = "tailnet"


class MemoryCategory(str, enum.Enum):
    """Categories for core memories."""
    IDENTITY = "identity"
    RELATIONSHIP = "relationship"
    PREFERENCE = "preference"
    FACT = "fact"
    SKILL = "skill"
    MEDICAL = "medical"
    WORK = "work"
    HOBBY = "hobby"
    OTHER = "other"


# ── Task Models ────────────────────────────────────────────────────────────

class TaskError(BaseModel):
    """Error details from a failed task."""
    error_type: str
    message: str
    traceback: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class TaskRouting(BaseModel):
    """Routing metadata for sub-agent tasks."""
    command: TaskCommand
    agent_id: str
    model_tier: ModelTier
    affinity: TaskAffinity = TaskAffinity.ANY
    target_worker: str | None = None
    required_capabilities: list[WorkerCapability] = Field(default_factory=list)
    fallback_agent: str | None = None
    fallback_window_seconds: int = 300


class SubAgentTask(BaseModel):
    """A task dispatched by the router to a worker."""
    # Immutable — set by router
    task_id: str
    session_id: str
    prompt: str
    routing: TaskRouting
    output_schema: dict[str, Any] | None = None
    timeout_seconds: int = 120
    blocking: bool = True
    content_rating: ContentRating = ContentRating.SFW
    priority: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Mutable — owned by worker
    status: TaskStatus = TaskStatus.PENDING
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: TaskError | None = None


# ── Worker Models ──────────────────────────────────────────────────────────

class ClaimResult(BaseModel):
    """Result of attempting to claim a task."""
    claimed: bool
    task_id: str
    reason: str | None = None


class WorkerRegistration(BaseModel):
    """A registered worker node in Firestore at /workers/{worker_id}."""
    worker_id: str
    hostname: str
    node_name: str
    capabilities: list[str] = Field(default_factory=list)
    supported_agents: list[str] = Field(default_factory=list)
    poiesis_version: str = "0.1.0"
    status: str = "online"
    current_task: str | None = None
    last_heartbeat: datetime = Field(default_factory=datetime.utcnow)
    started_at: datetime = Field(default_factory=datetime.utcnow)


# ── Agent Output Schemas ───────────────────────────────────────────────────

class CodeArtifact(BaseModel):
    """Output from the coder agent."""
    filename: str
    language: str
    code: str
    explanation: str
    tests: str | None = None
    sandbox_passed: bool = False
    git_sha: str | None = None


class Source(BaseModel):
    """A research source reference."""
    url: str
    title: str
    snippet: str | None = None


class ResearchResult(BaseModel):
    """Output from the research agent."""
    query: str
    summary: str
    sources: list[Source] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class CommandResult(BaseModel):
    """Output from the sysadmin agent."""
    command: str
    exit_code: int
    stdout: str
    stderr: str
    host: str
    duration_ms: int


# ── Chat Models ────────────────────────────────────────────────────────────

class Attachment(BaseModel):
    """File or media attached to a chat message."""
    filename: str
    content_type: str
    url: str | None = None
    size_bytes: int | None = None


class MessageNotification(BaseModel):
    """Notification metadata on a message — tells clients to alert the user."""
    type: str = "reminder"  # "reminder", "alert", "task_complete", etc.
    sound: bool = True
    title: str = ""


class ChatMessage(BaseModel):
    """A single message in a chat session."""
    message_id: str
    session_id: str
    role: MessageRole
    content: str
    content_rating: ContentRating = ContentRating.SFW
    notification: MessageNotification | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Reminder(BaseModel):
    """A scheduled reminder stored in Firestore at /reminders/{id}."""
    reminder_id: str
    session_id: str
    agent_id: str = ""
    message: str  # pre-composed message text
    fire_at: datetime
    fired: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ── Memory Models ──────────────────────────────────────────────────────────

class JournalEntry(BaseModel):
    """A mid-term observation logged during conversation, with surrounding context."""
    journal_id: str
    session_id: str
    content: str
    context_messages: list[str] = Field(default_factory=list)  # last ~5 messages for WHY this was noted
    topic: str | None = None
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    archived: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CoreMemory(BaseModel):
    """A long-term distilled fact in the memory system."""
    memory_id: str
    content: str
    category: MemoryCategory = MemoryCategory.OTHER
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    source_journals: list[str] = Field(default_factory=list)
    previous_content: str | None = None
    version: int = 1
    reviewed: bool = False
    deleted: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ── Event Protocol ─────────────────────────────────────────────────────────

class TaskQueued(BaseModel):
    """Event: a sub-agent task was queued."""
    task_id: str
    session_id: str
    command: TaskCommand
    agent_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TaskCompleted(BaseModel):
    """Event: a sub-agent task finished."""
    task_id: str
    session_id: str
    status: TaskStatus
    result: dict[str, Any] | None = None
    error: TaskError | None = None
    completed_at: datetime = Field(default_factory=datetime.utcnow)


# ── Config Models ──────────────────────────────────────────────────────────

class AgentConfig(BaseModel):
    """Configuration for an agent. Stored in Firestore at /agents/{agent_id}."""
    agent_id: str
    name: str
    description: str
    model: str
    system_prompt: str = ""
    model_tier: ModelTier = ModelTier.FAST
    output_type: str = "text"  # "text", "code_artifact", "research_result", "command_result", "spicy_result"
    triggers: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)  # tool IDs — Phase 3
    timeout_seconds: int = 120
    affinity: TaskAffinity = TaskAffinity.ANY
    required_capabilities: list[str] = Field(default_factory=list)
    fallback_agent: str | None = None
    fallback_window_seconds: int = 300
    content_rating: ContentRating = ContentRating.SFW
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class GlitchConfig(BaseModel):
    """Parsed glitch_core.yaml — the full agent configuration."""
    version: str = "1"
    router: AgentConfig
    agents: list[AgentConfig] = Field(default_factory=list)

    def worker_agents(self) -> list[AgentConfig]:
        """Return all non-router agents that are enabled."""
        return [a for a in self.agents if a.enabled]

    def get_agent(self, agent_id: str) -> AgentConfig | None:
        """Look up an agent by ID."""
        for a in self.agents:
            if a.agent_id == agent_id:
                return a
        return None


class FeatureFlags(BaseModel):
    """Runtime feature gates stored in ProjectMeta."""
    ouroboros_enabled: bool = Field(
        default=False,
        description="Allow the AI to generate and hot-reload tools, pages, and themes at runtime.",
    )


class ProjectMeta(BaseModel):
    """Project-level metadata stored at /meta/project."""
    version: str = "0.1.0"
    schema_version: int = 1
    firebase_project: str = ""
    default_agent: str = "router"
    feature_flags: FeatureFlags = Field(default_factory=FeatureFlags)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CompactionConfig(BaseModel):
    """Compaction pipeline settings stored at /meta/compaction_config."""
    schedule_cron: str = "0 3 * * *"
    model: str = "anthropic:claude-sonnet-4-20250514"
    min_journals_to_trigger: int = 5
    max_journals_per_run: int = 100
    batch_size: int = 10
    max_memories_per_run: int = 20
    require_confidence: float = 0.7
    never_compact_categories: list[str] = Field(
        default_factory=lambda: ["relationship", "identity", "medical"]
    )
    archive_journals: bool = True
    dry_run: bool = False
    enabled: bool = True


# ── Compaction Output Models ───────────────────────────────────────────────

class CompactedMemory(BaseModel):
    """A single memory distilled by the compaction summarization agent."""
    category: str
    content: str
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    source_journal_ids: list[str]
    related_memory_ids: list[str] = Field(default_factory=list)


class DiscardedJournal(BaseModel):
    """A journal the compaction agent decided not to keep."""
    journal_id: str
    reason: str  # "duplicate", "trivial", "superseded"


class CompactionResult(BaseModel):
    """What the summarization model returns per batch."""
    memories: list[CompactedMemory] = Field(default_factory=list)
    discarded: list[DiscardedJournal] = Field(default_factory=list)


class MergeGroup(BaseModel):
    """A group of memories to be merged into one."""
    memory_ids: list[str]
    merged_content: str
    category: str = "other"
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class MergeResult(BaseModel):
    """Result of the memory merging pass."""
    merge_groups: list[MergeGroup] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)


class CompactionError(BaseModel):
    """An error during a compaction phase."""
    stage: str  # "read", "summarization", "validation", "write", "archive"
    message: str
    journal_ids: list[str] = Field(default_factory=list)
    recoverable: bool = True


class CompactionRun(BaseModel):
    """Audit log for a compaction execution, written to /compaction_runs/{run_id}."""
    run_id: str
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
    status: str = "running"  # running, completed, failed, skipped, dry_run, rolled_back
    journals_read: int = 0
    journals_archived: int = 0
    memories_created: int = 0
    memories_updated: int = 0
    memories_flagged: int = 0
    errors: list[CompactionError] = Field(default_factory=list)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)


# ── Ouroboros Models ───────────────────────────────────────────────────────

class ValidationStage(str, enum.Enum):
    """Stages of Ouroboros validation."""
    SYNTAX = "syntax"
    IMPORT = "import"
    AST_SCAN = "ast_scan"
    SCHEMA = "schema"
    RENDER = "render"
    RUNTIME = "runtime"


class ValidationFailure(BaseModel):
    """A validation failure during Ouroboros promotion."""
    stage: ValidationStage
    error: str
    fixable: bool = True


class PromotionResult(BaseModel):
    """Result of an Ouroboros promotion (tool, page, or config write)."""
    success: bool
    artifact_path: str | None = None
    error: str | None = None
    rollback_id: str | None = None  # git SHA for rollback
    validation_failures: list[ValidationFailure] = Field(default_factory=list)


class ToolRegistration(BaseModel):
    """A tool registered in Firestore at /tools/{tool_id}."""
    tool_id: str
    name: str
    description: str
    filename: str  # relative to tools/ directory
    created_by: str = ""  # agent_id that created it
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    enabled: bool = True


# ── Workspace Models ───────────────────────────────────────────────────────

class WorkspaceFile(BaseModel):
    """Result of writing a file to the workspace."""
    path: str
    workspace_relative: str
    size_bytes: int
    created: bool


class WorkspaceEntry(BaseModel):
    """A single entry in a workspace directory listing."""
    name: str
    path: str
    is_dir: bool
    size_bytes: int
    modified_at: datetime | None = None


class WorkspaceTree(BaseModel):
    """Directory listing of the workspace."""
    files: list[WorkspaceEntry] = Field(default_factory=list)
    total_size_bytes: int = 0


class ScriptResult(BaseModel):
    """Result of running a script in the workspace."""
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out
