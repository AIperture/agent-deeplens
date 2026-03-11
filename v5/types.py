from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Literal


DEEPLENS_SKILL_ID = "aethergraph-agent-deeplens-v5"
STATE_KEY = "deeplens_state_v5"

# Keep this list narrow and explicit. Tools may introduce additional
# fine-grained missing inputs later, but these are the main user-facing codes.
MISSING_FIELD_CODES = [
    "fov",
    "fnum",
    "foclen_or_imgh",
    "lens_source",
    "run_id",
    "analysis_mode",
    "export_format",
]


# ---------------------------------------------------------------------
# Core enums
# ---------------------------------------------------------------------


class DomainHint(str, Enum):
    CHAT = "chat"
    ANALYSIS = "analysis"
    DESIGN = "design"
    OPTIMIZATION = "optimization"
    EXPORT = "export"
    RUN_CONTROL = "run_control"
    DEBUG = "debug"
    UNKNOWN = "unknown"


class ContextMode(str, Enum):
    LITE = "lite"
    FULL = "full"


class TaskShape(str, Enum):
    DIRECT_ANSWER = "direct_answer"
    SINGLE_ACTION = "single_action"
    MULTI_STEP = "multi_step"
    RUN_CONTROL = "run_control"
    UNSUPPORTED = "unsupported"


class TaskStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    WAITING_FOR_USER = "waiting_for_user"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    SUPERSEDED = "superseded"


class FieldSource(str, Enum):
    USER_TEXT = "user_text"
    USER_FOLLOWUP = "user_followup"
    REGEX = "regex"
    HEURISTIC = "heuristic"
    ATTACHMENT = "attachment"
    STATE = "state"
    LLM = "llm"
    DEFAULT = "default"
    TOOL = "tool"


class ToolExecutionStyle(str, Enum):
    INLINE = "inline"
    SPAWN = "spawn"


class ToolCategory(str, Enum):
    AG = "ag"
    DEEPLENS = "deeplens"


class ApprovalLevel(str, Enum):
    NONE = "none"
    SOFT = "soft"
    HARD = "hard"


class PendingInteractionKind(str, Enum):
    MISSING_INFO = "missing_info"
    APPROVAL = "approval"


class ResponseShape(str, Enum):
    FREE_TEXT = "free_text"
    YES_NO = "yes_no"
    SCALAR = "scalar"
    FILE_OR_REF = "file_or_ref"
    STRUCTURED = "structured"


class LoopActionKind(str, Enum):
    ASK_USER = "ask_user"
    REQUEST_APPROVAL = "request_approval"
    TOOL_CALL = "tool_call"
    RESPOND = "respond"
    FINISH = "finish"
    FAIL = "fail"


class OutcomeType(str, Enum):
    SUCCESS = "success"
    NEEDS_INPUT = "needs_input"
    NEEDS_APPROVAL = "needs_approval"
    RUN_SUBMITTED = "run_submitted"
    RUN_UPDATED = "run_updated"
    FAILED = "failed"
    NOOP = "noop"


class LoopOutcomeKind(str, Enum):
    COMPLETE = "complete"
    WAITING = "waiting"
    ESCALATE = "escalate"
    FAILED = "failed"


class TurnRole(str, Enum):
    NEW_TASK = "new_task"
    UPDATE_OPEN_TASK = "update_open_task"
    ANSWER_PENDING_INTERACTION = "answer_pending_interaction"
    CONTROL_OR_META = "control_or_meta"
    DIRECT_REPLY = "direct_reply"


class RepairDecisionKind(str, Enum):
    RETRY = "retry"
    ASK_USER = "ask_user"
    ESCALATE = "escalate"
    FAIL = "fail"


# ---------------------------------------------------------------------
# Lightweight turn / extraction objects
# ---------------------------------------------------------------------


@dataclass
class TurnEnvelope:
    raw_message: str
    cleaned_message: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    explicit_command: str | None = None
    ui_hints: dict[str, Any] = field(default_factory=dict)
    user_meta: dict[str, Any] = field(default_factory=dict)
    active_refs: list[str] = field(default_factory=list)
    active_source_ref: dict[str, Any] = field(default_factory=dict)
    active_run_id: str | None = None


@dataclass
class FieldValue:
    value: Any = None
    source: FieldSource = FieldSource.DEFAULT
    confidence: float = 0.0
    inferred: bool = False
    confirmed: bool = False
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FieldValue":
        raw = data or {}
        return cls(
            value=raw.get("value"),
            source=FieldSource(raw.get("source", FieldSource.DEFAULT.value)),
            confidence=float(raw.get("confidence", 0.0) or 0.0),
            inferred=bool(raw.get("inferred", False)),
            confirmed=bool(raw.get("confirmed", False)),
            notes=list(raw.get("notes", [])),
        )


@dataclass
class TaskFrame:
    # Keep TaskFrame as the single main semantic object.
    # Do not put controller-specific or UI-specific state here.
    task_id: str
    user_goal: str
    domain_hint: DomainHint = DomainHint.UNKNOWN
    task_shape: TaskShape = TaskShape.UNSUPPORTED
    status: TaskStatus = TaskStatus.DRAFT
    preferred_tool: str | None = None

    # Parsed / merged fields
    field_map: dict[str, FieldValue] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    # Task payload buckets
    design_spec: dict[str, Any] = field(default_factory=dict)
    analysis_request: dict[str, Any] = field(default_factory=dict)
    run_request: dict[str, Any] = field(default_factory=dict)
    delivery_request: dict[str, Any] = field(default_factory=dict)

    # Refs / sources
    attachments: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    active_artifact_refs: list[str] = field(default_factory=list)
    lens_source: dict[str, Any] = field(default_factory=dict)

    # Bookkeeping
    created_from_turn: int | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TaskFrame":
        raw = data or {}
        task = cls(
            task_id=raw.get("task_id", ""),
            user_goal=raw.get("user_goal", ""),
            domain_hint=DomainHint(raw.get("domain_hint", DomainHint.UNKNOWN.value)),
            task_shape=TaskShape(raw.get("task_shape", TaskShape.UNSUPPORTED.value)),
            status=TaskStatus(raw.get("status", TaskStatus.DRAFT.value)),
            preferred_tool=raw.get("preferred_tool"),
            missing_fields=list(raw.get("missing_fields", [])),
            notes=list(raw.get("notes", [])),
            assumptions=list(raw.get("assumptions", [])),
            design_spec=dict(raw.get("design_spec", {})),
            analysis_request=dict(raw.get("analysis_request", {})),
            run_request=dict(raw.get("run_request", {})),
            delivery_request=dict(raw.get("delivery_request", {})),
            attachments=list(raw.get("attachments", [])),
            source_refs=list(raw.get("source_refs", [])),
            active_artifact_refs=list(raw.get("active_artifact_refs", [])),
            lens_source=dict(raw.get("lens_source", {})),
            created_from_turn=raw.get("created_from_turn"),
            updated_at=raw.get("updated_at"),
        )
        for key, value in dict(raw.get("field_map", {}) or {}).items():
            if isinstance(value, dict):
                task.field_map[key] = FieldValue.from_dict(value)
        return task


@dataclass
class PendingInteraction:
    # This replaces many scattered "waiting for user / pending approval"
    # fields from v3/v4.
    kind: PendingInteractionKind
    prompt: str
    related_task_id: str
    expected_fields: list[str] = field(default_factory=list)
    response_shape: ResponseShape = ResponseShape.FREE_TEXT
    strictness: str = "normal"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PendingInteraction | None":
        if not data:
            return None
        raw = data or {}
        return cls(
            kind=PendingInteractionKind(raw["kind"]),
            prompt=raw.get("prompt", ""),
            related_task_id=raw.get("related_task_id", ""),
            expected_fields=list(raw.get("expected_fields", [])),
            response_shape=ResponseShape(raw.get("response_shape", ResponseShape.FREE_TEXT.value)),
            strictness=raw.get("strictness", "normal"),
            metadata=dict(raw.get("metadata", {})),
        )


@dataclass
class LoopAction:
    # Keep action vocabulary very small.
    kind: LoopActionKind
    tool_name: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    priority: int = 0


@dataclass
class ToolResult:
    ok: bool
    tool_name: str
    summary: str
    outcome_type: OutcomeType
    status: str = "completed"

    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    retryable: bool = False
    run_id: str | None = None
    needs_input: bool = False
    missing_fields: list[str] = field(default_factory=list)
    state_updates: dict[str, Any] = field(default_factory=dict)
    recommended_next_actions: list[str] = field(default_factory=list)
    user_visible_summary: str | None = None
    should_end_turn: bool = False
    raw_status: str | None = None


@dataclass
class RepairDecision:
    # Narrow repair output. Do not turn this into a second planner.
    kind: RepairDecisionKind
    reason: str
    task: TaskFrame | None = None
    action: LoopAction | None = None
    pending_interaction: PendingInteraction | None = None


@dataclass
class LoopOutcome:
    kind: LoopOutcomeKind
    reason: str
    task_snapshot: TaskFrame
    pending_interaction: PendingInteraction | None = None
    last_tool_result: ToolResult | None = None


@dataclass
class InterpreterDecision:
    # This is intentionally much smaller than v4 InterpretationResult.
    turn_role: TurnRole
    task: TaskFrame | None = None
    direct_reply: str | None = None
    replace_active_task: bool = False
    clear_pending_interaction: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class ConversationState:
    # Keep persistent runtime state small.
    context_mode: str = "lite"
    active_task: dict[str, Any] | None = None
    pending_interaction: dict[str, Any] | None = None

    active_run_id: str | None = None
    pending_runs: list[dict[str, Any]] = field(default_factory=list)
    active_lens_ref: str | None = None
    active_source_ref: dict[str, Any] = field(default_factory=dict)
    last_metrics: dict[str, Any] = field(default_factory=dict)
    last_artifacts: list[dict[str, Any]] = field(default_factory=list)
    last_analysis_bundle: dict[str, Any] = field(default_factory=dict)
    design_draft: dict[str, Any] = field(default_factory=dict)
    last_result_summary: str | None = None
    next_action_hints: list[str] = field(default_factory=list)
    last_user_turn: str | None = None
    requested_next_step: str | None = None
    pending_action: str | None = None
    pending_approval: dict[str, Any] | None = None
    approved_action: str | None = None

    retry_counters: dict[str, int] = field(default_factory=dict)
    loop_trace: list[dict[str, Any]] = field(default_factory=list)
    loop_history: list[dict[str, Any]] = field(default_factory=list)
    active_plan: dict[str, Any] | None = None

    last_summary_tag: str = "session"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ConversationState":
        raw = data or {}
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in raw.items() if k in allowed}
        return cls(**filtered)

    def get_active_task(self) -> TaskFrame | None:
        if not self.active_task:
            return None
        return TaskFrame.from_dict(self.active_task)

    def set_active_task(self, task: TaskFrame | None) -> None:
        self.active_task = task.to_dict() if task else None

    def get_pending_interaction(self) -> PendingInteraction | None:
        return PendingInteraction.from_dict(self.pending_interaction)

    def set_pending_interaction(self, pending: PendingInteraction | None) -> None:
        self.pending_interaction = pending.to_dict() if pending else None


# ---------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------


async def load_state(context: Any, level: str = "user") -> ConversationState:
    raw = await context.memory().latest_state(
        STATE_KEY,
        level=level,
        user_persistence=True,
    )
    return ConversationState.from_dict(raw)


async def save_state(context: Any, state: ConversationState) -> None:
    await context.memory().record_state(
        key=STATE_KEY,
        value=state.to_dict(),
        tags=["ag.deeplens.v5", "runtime"],
        meta={"agent": "deeplens_agent_v5"},
        severity=1,
    )
