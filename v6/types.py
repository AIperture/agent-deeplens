from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Literal, TypedDict


DEEPLENS_SKILL_ID = "aethergraph-agent-deeplens-v6"
STATE_KEY = "deeplens_state_v6"
MISSING_FIELD_CODES = [
    "fov",
    "fnum",
    "foclen_or_imgh",
    "lens_source",
    "run_id",
    "analysis_mode",
    "export_formats",
]


class TaskShape(str, Enum):
    DIRECT_ANSWER = "direct_answer"
    SINGLE_ACTION = "single_action"
    MULTI_STEP = "multi_step"
    RUN_CONTROL = "run_control"
    UNSUPPORTED = "unsupported"


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


class FieldSource(str, Enum):
    USER_TEXT = "user_text"
    REGEX = "regex"
    HEURISTIC = "heuristic"
    ATTACHMENT = "attachment"
    STATE = "state"
    LLM = "llm"
    DEFAULT = "default"
    TOOL = "tool"


class AgendaActionStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELED = "canceled"


class AgendaStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class ResponseOutcomeKind(str, Enum):
    COMPLETE = "complete"
    WAITING = "waiting"
    ESCALATE = "escalate"
    FAILED = "failed"


class FailureKind(str, Enum):
    INVALID_INPUTS = "invalid_inputs"
    MISSING_INPUTS = "missing_inputs"
    MISSING_DEPENDENCY = "missing_dependency"
    TRANSIENT_TOOL_ERROR = "transient_tool_error"
    INTERNAL_TOOL_ERROR = "internal_tool_error"
    UNSAFE_ACTION = "unsafe_action"
    UNKNOWN = "unknown"


class RecoveryDecisionKind(str, Enum):
    RETRY_ACTION = "retry_action"
    REPLACE_REMAINING_AGENDA = "replace_remaining_agenda"
    ASK_USER = "ask_user"
    ESCALATE = "escalate"
    FAIL = "fail"


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
class IntentFrame:
    user_goal: str
    dominant_domain: DomainHint = DomainHint.UNKNOWN
    requested_capabilities: list[str] = field(default_factory=list)
    sequencing_hints: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    active_refs: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "IntentFrame | None":
        if not data:
            return None
        raw = data or {}
        return cls(
            user_goal=raw.get("user_goal", ""),
            dominant_domain=DomainHint(raw.get("dominant_domain", DomainHint.UNKNOWN.value)),
            requested_capabilities=list(raw.get("requested_capabilities", [])),
            sequencing_hints=list(raw.get("sequencing_hints", [])),
            missing_fields=list(raw.get("missing_fields", [])),
            active_refs=dict(raw.get("active_refs", {})),
            summary=raw.get("summary", ""),
        )


@dataclass
class AgendaAction:
    action_id: str
    kind: Literal["ask_user", "request_approval", "tool_call", "respond", "finish", "fail"]
    name: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    status: AgendaActionStatus = AgendaActionStatus.PENDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "name": self.name,
            "args": self.args,
            "rationale": self.rationale,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgendaAction":
        return cls(
            action_id=str(data.get("action_id") or ""),
            kind=str(data.get("kind") or "respond"),
            name=data.get("name"),
            args=dict(data.get("args") or {}),
            rationale=str(data.get("rationale") or ""),
            status=AgendaActionStatus(data.get("status", AgendaActionStatus.PENDING.value)),
        )


@dataclass
class ActionAgenda:
    goal: str
    actions: list[AgendaAction] = field(default_factory=list)
    status: AgendaStatus = AgendaStatus.DRAFT
    resumable: bool = True
    next_action_hints: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "actions": [action.to_dict() for action in self.actions],
            "status": self.status.value,
            "resumable": self.resumable,
            "next_action_hints": list(self.next_action_hints),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ActionAgenda | None":
        if not data:
            return None
        raw = data or {}
        return cls(
            goal=raw.get("goal", ""),
            actions=[AgendaAction.from_dict(item) for item in list(raw.get("actions") or []) if isinstance(item, dict)],
            status=AgendaStatus(raw.get("status", AgendaStatus.DRAFT.value)),
            resumable=bool(raw.get("resumable", True)),
            next_action_hints=list(raw.get("next_action_hints", [])),
            metadata=dict(raw.get("metadata", {})),
        )


@dataclass
class DeepLensTask:
    user_goal: str
    task_shape: TaskShape
    domain_hint: DomainHint = DomainHint.UNKNOWN
    attachments: list[dict[str, Any]] = field(default_factory=list)
    parsed_args: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    preferred_tool: str | None = None
    active_artifact_refs: list[str] = field(default_factory=list)
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    lens_source: dict[str, Any] = field(default_factory=dict)
    design_spec: dict[str, Any] = field(default_factory=dict)
    analysis_request: dict[str, Any] = field(default_factory=dict)
    run_request: dict[str, Any] = field(default_factory=dict)
    delivery_request: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    field_map: dict[str, FieldValue] = field(default_factory=dict)
    requested_capabilities: list[str] = field(default_factory=list)
    sequencing_hints: list[str] = field(default_factory=list)
    intent_summary: str = ""


@dataclass
class PendingRun:
    run_id: str
    graph_id: str
    status: str = "submitted"
    cancelable: bool = True
    submitted_at: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeepLensState:
    context_mode: str = "lite"
    active_task: dict[str, Any] | None = None
    active_intent: dict[str, Any] | None = None
    active_agenda: dict[str, Any] | None = None
    active_run_id: str | None = None
    pending_runs: list[dict[str, Any]] = field(default_factory=list)
    active_lens_ref: str | None = None
    active_source_ref: dict[str, Any] = field(default_factory=dict)
    last_metrics: dict[str, Any] = field(default_factory=dict)
    last_artifacts: list[dict[str, Any]] = field(default_factory=list)
    last_analysis_bundle: dict[str, Any] = field(default_factory=dict)
    design_draft: dict[str, Any] = field(default_factory=dict)
    next_action_hints: list[str] = field(default_factory=list)
    requested_next_step: str | None = None
    pending_action: str | None = None
    pending_approval: dict[str, Any] | None = None
    approved_action: str | None = None
    retry_counters: dict[str, int] = field(default_factory=dict)
    recovery_attempts: dict[str, int] = field(default_factory=dict)
    loop_trace: list[dict[str, Any]] = field(default_factory=list)
    loop_history: list[dict[str, Any]] = field(default_factory=list)
    last_tool_result: dict[str, Any] = field(default_factory=dict)
    failure_history: list[dict[str, Any]] = field(default_factory=list)
    last_replan_reason: str | None = None
    active_recovery: dict[str, Any] | None = None
    last_summary_tag: str = "session"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DeepLensState":
        raw = data or {}
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in raw.items() if k in allowed}
        return cls(**filtered)


class RouteDecision(TypedDict):
    task_shape: TaskShape
    domain_hint: DomainHint
    preferred_tool: str | None
    context_mode: ContextMode
    reason: str
    confidence: float
    task: DeepLensTask


class RouteResult(TypedDict):
    decision: RouteDecision
    state: DeepLensState
    immediate_reply: str | None


class LoopAction(TypedDict, total=False):
    kind: Literal["ask_user", "request_approval", "tool_call", "respond", "finish", "fail"]
    name: str | None
    args: dict[str, Any]
    rationale: str


@dataclass
class ToolResult:
    ok: bool
    summary: str
    status: str = "completed"
    tool_name: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    retryable: bool = False
    run_id: str | None = None
    failure_kind: FailureKind | None = None
    blocking: bool = False
    repairable: bool = False
    needs_replan: bool = False
    needs_user_input: bool = False
    missing_fields: list[str] = field(default_factory=list)
    invalid_fields: dict[str, str] = field(default_factory=dict)
    repair_hints: list[str] = field(default_factory=list)
    dependency_failures: list[str] = field(default_factory=list)
    human_escalation_reason: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    should_end_turn: bool = False


@dataclass
class ResponseFrame:
    outcome_kind: ResponseOutcomeKind
    reply: str
    agenda_status: str = ""
    completed_actions: list[str] = field(default_factory=list)
    pending_actions: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    approval_prompt: str | None = None
    last_tool_summary: str | None = None
    next_action_hints: list[str] = field(default_factory=list)
    active_run_id: str | None = None


@dataclass
class RecoveryDecision:
    kind: RecoveryDecisionKind
    reason: str
    action: AgendaAction | None = None
    replacement_actions: list[AgendaAction] = field(default_factory=list)
    ask_user_prompt: str | None = None
    task: DeepLensTask | None = None
    outcome_kind: ResponseOutcomeKind | None = None


ROUTER_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_shape": {"type": "string", "enum": [x.value for x in TaskShape]},
        "domain_hint": {"type": "string", "enum": [x.value for x in DomainHint]},
        "preferred_tool": {"type": ["string", "null"]},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
        "parsed_args": {"type": ["string", "null"]},
        "missing_fields": {
            "type": "array",
            "items": {"type": "string", "enum": MISSING_FIELD_CODES},
            "maxItems": 6,
        },
    },
    "required": [
        "task_shape",
        "domain_hint",
        "preferred_tool",
        "reason",
        "confidence",
        "parsed_args",
        "missing_fields",
    ],
    "additionalProperties": False,
}


async def load_state(context: Any, level: str = "user") -> DeepLensState:
    raw = await context.memory().latest_state(
        STATE_KEY,
        level=level,
        user_persistence=True,
    )
    return DeepLensState.from_dict(raw)


async def save_state(context: Any, state: DeepLensState) -> None:
    await context.memory().record_state(
        key=STATE_KEY,
        value=state.to_dict(),
        tags=["ag.deeplens.v6", "runtime"],
        meta={"agent": "deeplens_agent_v6"},
        severity=1,
    )
