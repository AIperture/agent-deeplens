from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Literal, TypedDict


TaskStatus = Literal["active", "waiting", "completed", "failed", "canceled", "superseded"]
ACTIVE_TASK_STATUSES: tuple[TaskStatus, ...] = ("active", "waiting")
TERMINAL_TASK_STATUSES: tuple[TaskStatus, ...] = ("completed", "failed", "canceled", "superseded")


class TaskShape(str, Enum):
    DIRECT_ANSWER = "direct_answer"
    SINGLE_ACTION = "single_action"
    MULTI_STEP = "multi_step"
    UNSUPPORTED = "unsupported"


class ContextMode(str, Enum):
    LITE = "lite"
    FULL = "full"


class ApprovalLevel(str, Enum):
    NONE = "none"
    SOFT = "soft"
    HARD = "hard"


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
    BINDING_FAILURE = "binding_failure"
    INVALID_INPUTS = "invalid_inputs"
    MISSING_INPUTS = "missing_inputs"
    POLICY_FAILURE = "policy_failure"
    TRANSIENT_ERROR = "transient_error"
    EXECUTION_ERROR = "execution_error"
    INTERNAL_ERROR = "internal_error"
    UNKNOWN = "unknown"


class RecoveryDecisionKind(str, Enum):
    RETRY_ACTION = "retry_action"
    REPLACE_REMAINING_AGENDA = "replace_remaining_agenda"
    ASK_USER = "ask_user"
    REQUEST_APPROVAL = "request_approval"
    ESCALATE = "escalate"
    FAIL = "fail"


class FieldSource(str, Enum):
    PLANNER_ARGS = "planner_args"
    TASK_ARGS = "task_args"
    TASK_FIELD_MAP = "task_field_map"
    WORKING_STATE = "working_state"
    MEMORY_BAG = "memory_bag"
    PRIOR_TOOL_OUTPUT = "prior_tool_output"
    DETERMINISTIC_INFERENCE = "deterministic_inference"
    LLM_INFERENCE = "llm_inference"
    DEFAULT = "default"


@dataclass
class CapabilityIntent:
    capability_name: str
    goal: str
    rationale: str = ""
    required_fields: list[str] = field(default_factory=list)
    optional_fields: list[str] = field(default_factory=list)
    candidate_tools: list[str] = field(default_factory=list)
    expected_output: str = ""
    planner_args: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CapabilityIntent":
        return cls(
            capability_name=str(data.get("capability_name") or ""),
            goal=str(data.get("goal") or ""),
            rationale=str(data.get("rationale") or ""),
            required_fields=list(data.get("required_fields") or []),
            optional_fields=list(data.get("optional_fields") or []),
            candidate_tools=list(data.get("candidate_tools") or []),
            expected_output=str(data.get("expected_output") or ""),
            planner_args=dict(data.get("planner_args") or {}),
        )


@dataclass
class FieldResolution:
    value: Any = None
    source: FieldSource = FieldSource.DEFAULT
    confidence: float = 0.0
    confirmed: bool = False
    defaulted: bool = False
    valid: bool = True
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source"] = self.source.value
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FieldResolution":
        raw = data or {}
        return cls(
            value=raw.get("value"),
            source=FieldSource(raw.get("source", FieldSource.DEFAULT.value)),
            confidence=float(raw.get("confidence", 0.0) or 0.0),
            confirmed=bool(raw.get("confirmed", False)),
            defaulted=bool(raw.get("defaulted", False)),
            valid=bool(raw.get("valid", True)),
            message=str(raw.get("message") or ""),
        )


@dataclass
class ExecutableAction:
    action_id: str
    capability_name: str
    tool_name: str | None
    resolved_inputs: dict[str, Any] = field(default_factory=dict)
    field_resolutions: dict[str, FieldResolution] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    invalid_fields: dict[str, str] = field(default_factory=dict)
    approval_required: ApprovalLevel = ApprovalLevel.NONE
    binding_status: str = "ready"
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "capability_name": self.capability_name,
            "tool_name": self.tool_name,
            "resolved_inputs": dict(self.resolved_inputs),
            "field_resolutions": {k: v.to_dict() for k, v in self.field_resolutions.items()},
            "missing_fields": list(self.missing_fields),
            "invalid_fields": dict(self.invalid_fields),
            "approval_required": self.approval_required.value,
            "binding_status": self.binding_status,
            "provenance": dict(self.provenance),
        }


@dataclass
class AgendaAction:
    action_id: str
    kind: Literal["bind_and_execute", "ask_user", "request_approval", "respond", "finish", "fail"]
    capability: CapabilityIntent | None = None
    name: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    status: AgendaActionStatus = AgendaActionStatus.PENDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "capability": self.capability.to_dict() if self.capability else None,
            "name": self.name,
            "args": dict(self.args),
            "rationale": self.rationale,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgendaAction":
        capability_raw = data.get("capability")
        capability = CapabilityIntent.from_dict(capability_raw) if isinstance(capability_raw, dict) else None
        return cls(
            action_id=str(data.get("action_id") or ""),
            kind=str(data.get("kind") or "respond"),
            capability=capability,
            name=data.get("name"),
            args=dict(data.get("args") or {}),
            rationale=str(data.get("rationale") or ""),
            status=AgendaActionStatus(data.get("status", AgendaActionStatus.PENDING.value)),
        )


@dataclass
class PlanAgenda:
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
    def from_dict(cls, data: dict[str, Any] | None) -> "PlanAgenda | None":
        if not data:
            return None
        raw = data or {}
        return cls(
            goal=str(raw.get("goal") or ""),
            actions=[AgendaAction.from_dict(item) for item in list(raw.get("actions") or []) if isinstance(item, dict)],
            status=AgendaStatus(raw.get("status", AgendaStatus.DRAFT.value)),
            resumable=bool(raw.get("resumable", True)),
            next_action_hints=list(raw.get("next_action_hints") or []),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass
class TemplateTask:
    user_goal: str
    task_shape: TaskShape
    domain_hint: str = "general"
    task_status: TaskStatus = "active"
    attachments: list[dict[str, Any]] = field(default_factory=list)
    parsed_args: dict[str, Any] = field(default_factory=dict)
    field_map: dict[str, FieldResolution] = field(default_factory=dict)
    working_state: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    preferred_tool: str | None = None
    notes: list[str] = field(default_factory=list)
    requested_capabilities: list[str] = field(default_factory=list)
    sequencing_hints: list[str] = field(default_factory=list)
    intent_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["task_shape"] = self.task_shape.value
        payload["field_map"] = {k: v.to_dict() for k, v in self.field_map.items()}
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TemplateTask | None":
        if not data:
            return None
        raw = data or {}
        field_map_raw = dict(raw.get("field_map") or {})
        return cls(
            user_goal=str(raw.get("user_goal") or ""),
            task_shape=TaskShape(raw.get("task_shape", TaskShape.UNSUPPORTED.value)),
            domain_hint=str(raw.get("domain_hint") or "general"),
            task_status=str(raw.get("task_status") or "active"),
            attachments=[dict(item) for item in list(raw.get("attachments") or []) if isinstance(item, dict)],
            parsed_args=dict(raw.get("parsed_args") or {}),
            field_map={k: FieldResolution.from_dict(v if isinstance(v, dict) else None) for k, v in field_map_raw.items()},
            working_state=dict(raw.get("working_state") or {}),
            missing_fields=list(raw.get("missing_fields") or []),
            preferred_tool=raw.get("preferred_tool"),
            notes=list(raw.get("notes") or []),
            requested_capabilities=list(raw.get("requested_capabilities") or []),
            sequencing_hints=list(raw.get("sequencing_hints") or []),
            intent_summary=str(raw.get("intent_summary") or ""),
        )


@dataclass
class TemplateState:
    context_mode: str = "lite"
    active_task: dict[str, Any] | None = None
    active_agenda: dict[str, Any] | None = None
    active_route: dict[str, Any] | None = None
    next_action_hints: list[str] = field(default_factory=list)
    pending_action: str | None = None
    pending_approval: dict[str, Any] | None = None
    approval_tokens: dict[str, str] = field(default_factory=dict)
    retry_counters: dict[str, int] = field(default_factory=dict)
    recovery_attempts: dict[str, int] = field(default_factory=dict)
    loop_trace: list[dict[str, Any]] = field(default_factory=list)
    loop_history: list[dict[str, Any]] = field(default_factory=list)
    last_binding: dict[str, Any] | None = None
    last_tool_result: dict[str, Any] = field(default_factory=dict)
    prior_tool_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    memory_bag: dict[str, Any] = field(default_factory=dict)
    failure_history: list[dict[str, Any]] = field(default_factory=list)
    recent_failures: list[dict[str, Any]] = field(default_factory=list)
    runtime_missing_fields: list[str] = field(default_factory=list)
    runtime_invalid_fields: dict[str, str] = field(default_factory=dict)
    active_recovery: dict[str, Any] | None = None
    waiting_prompt: str | None = None
    debug_flags: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TemplateState":
        raw = data or {}
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in raw.items() if k in allowed}
        return cls(**filtered)


@dataclass
class ToolResult:
    ok: bool
    summary: str
    status: str = "completed"
    tool_name: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    failure_kind: FailureKind | None = None
    retryable: bool = False
    needs_replan: bool = False
    needs_user_input: bool = False
    missing_fields: list[str] = field(default_factory=list)
    invalid_fields: dict[str, str] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    should_end_turn: bool = False


@dataclass
class RecoveryDecision:
    kind: RecoveryDecisionKind
    reason: str
    action: AgendaAction | None = None
    replacement_actions: list[AgendaAction] = field(default_factory=list)
    ask_user_prompt: str | None = None
    approval_prompt: str | None = None
    task: TemplateTask | None = None
    outcome_kind: ResponseOutcomeKind | None = None


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


class RouteDecision(TypedDict):
    route_kind: Literal["new_task", "continue_task", "answer_pending_interaction"]
    task_shape: TaskShape
    context_mode: ContextMode
    reason: str
    confidence: float
    task: TemplateTask


class RouteResult(TypedDict):
    decision: RouteDecision
    state: TemplateState
    immediate_reply: str | None
    turn_role: str


async def load_state(context: Any, *, state_key: str, level: str = "session") -> TemplateState:
    raw = await context.memory().latest_state(
        state_key,
        level=level,
        user_persistence=True,
    )
    return TemplateState.from_dict(raw)


async def save_state(context: Any, *, state: TemplateState, state_key: str, agent_id: str) -> None:
    await context.memory().record_state(
        key=state_key,
        value=state.to_dict(),
        tags=[f"ag.{agent_id}", "runtime"],
        meta={"agent": agent_id},
        severity=1,
    )
