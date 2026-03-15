from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Literal


DEEPLENS_SKILL_ID = "aethergraph-agent-deeplens-v4"
STATE_KEY = "deeplens_state_v4"

MISSING_FIELD_CODES = [
    "fov",
    "fnum",
    "foclen_or_imgh",
    "lens_source",
    "run_id",
    "analysis_mode",
    "export_format",
]


class DomainHint(str, Enum):
    CHAT = "chat"
    ANALYSIS = "analysis"
    DESIGN = "design"
    OPTIMIZATION = "optimization"
    DEBUG = "debug"
    RUN_CONTROL = "run_control"
    EXPORT = "export"
    UNKNOWN = "unknown"


class IntentType(str, Enum):
    ASK = "ask"
    EXPLAIN = "explain"
    ANALYZE = "analyze"
    DESIGN = "design"
    OPTIMIZE = "optimize"
    DEBUG = "debug"
    EXPORT = "export"
    CONTROL_RUN = "control_run"
    CONTINUE = "continue"
    UNKNOWN = "unknown"


class TaskShape(str, Enum):
    DIRECT_ANSWER = "direct_answer"
    SINGLE_ACTION = "single_action"
    MULTI_STEP = "multi_step"
    RUN_CONTROL = "run_control"
    UNSUPPORTED = "unsupported"


class WorkflowFamily(str, Enum):
    DIRECT = "direct"
    ANALYSIS = "analysis"
    DESIGN = "design"
    OPTIMIZATION = "optimization"
    RUN_CONTROL = "run_control"
    EXPORT = "export"
    DEBUG = "debug"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class ControllerKind(str, Enum):
    DIRECT = "direct"
    WORKFLOW = "workflow"
    RUN_CONTROL = "run_control"


class ContextPolicy(str, Enum):
    MINIMAL = "minimal"
    TASK_LOCAL = "task_local"
    ARTIFACT_CENTRIC = "artifact_centric"
    MEMORY_AUGMENTED = "memory_augmented"
    FULL = "full"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalLevel(str, Enum):
    NONE = "none"
    SOFT = "soft"
    HARD = "hard"


class ToolExecutionStyle(str, Enum):
    INLINE = "inline"
    SPAWN = "spawn"


class ToolCategory(str, Enum):
    AG = "ag"
    DEEPLENS = "deeplens"


class FieldSource(str, Enum):
    USER_TEXT = "user_text"
    REGEX = "regex"
    HEURISTIC = "heuristic"
    ATTACHMENT = "attachment"
    STATE = "state"
    LLM = "llm"
    USER_FOLLOWUP = "user_followup"
    DEFAULT = "default"


class OutputType(str, Enum):
    TEXT = "text"
    SUMMARY = "summary"
    METRICS = "metrics"
    ARTIFACT = "artifact"
    TABLE = "table"
    PLOT = "plot"
    JSON = "json"
    UNKNOWN = "unknown"


class OutcomeType(str, Enum):
    SUCCESS = "success"
    NEEDS_INPUT = "needs_input"
    NEEDS_APPROVAL = "needs_approval"
    RUN_SUBMITTED = "run_submitted"
    RUN_UPDATED = "run_updated"
    FAILED = "failed"
    NOOP = "noop"


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


@dataclass
class TaskFrame:
    user_goal: str
    domain_hint: DomainHint = DomainHint.UNKNOWN
    intent: IntentType = IntentType.UNKNOWN
    task_shape: TaskShape = TaskShape.UNSUPPORTED
    workflow_family: WorkflowFamily = WorkflowFamily.UNKNOWN
    entry_stage: str | None = None
    preferred_tool: str | None = None
    context_policy: ContextPolicy = ContextPolicy.MINIMAL
    expected_output: OutputType = OutputType.UNKNOWN
    confidence: float = 0.0
    risk_level: RiskLevel = RiskLevel.LOW
    approval_required: ApprovalLevel = ApprovalLevel.NONE
    missing_fields: list[str] = field(default_factory=list)
    repairable_fields: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    field_map: dict[str, FieldValue] = field(default_factory=dict)
    task_payload: dict[str, Any] = field(default_factory=dict)
    design_spec: dict[str, Any] = field(default_factory=dict)
    analysis_request: dict[str, Any] = field(default_factory=dict)
    run_request: dict[str, Any] = field(default_factory=dict)
    delivery_request: dict[str, Any] = field(default_factory=dict)
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    active_artifact_refs: list[str] = field(default_factory=list)
    lens_source: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TaskFrame":
        raw = data or {}
        task = cls(
            user_goal=raw.get("user_goal", ""),
            domain_hint=DomainHint(raw.get("domain_hint", DomainHint.UNKNOWN.value)),
            intent=IntentType(raw.get("intent", IntentType.UNKNOWN.value)),
            task_shape=TaskShape(raw.get("task_shape", TaskShape.UNSUPPORTED.value)),
            workflow_family=WorkflowFamily(raw.get("workflow_family", WorkflowFamily.UNKNOWN.value)),
            entry_stage=raw.get("entry_stage"),
            preferred_tool=raw.get("preferred_tool"),
            context_policy=ContextPolicy(raw.get("context_policy", ContextPolicy.MINIMAL.value)),
            expected_output=OutputType(raw.get("expected_output", OutputType.UNKNOWN.value)),
            confidence=float(raw.get("confidence", 0.0) or 0.0),
            risk_level=RiskLevel(raw.get("risk_level", RiskLevel.LOW.value)),
            approval_required=ApprovalLevel(raw.get("approval_required", ApprovalLevel.NONE.value)),
            missing_fields=list(raw.get("missing_fields", [])),
            repairable_fields=list(raw.get("repairable_fields", [])),
            notes=list(raw.get("notes", [])),
            task_payload=dict(raw.get("task_payload", {})),
            design_spec=dict(raw.get("design_spec", {})),
            analysis_request=dict(raw.get("analysis_request", {})),
            run_request=dict(raw.get("run_request", {})),
            delivery_request=dict(raw.get("delivery_request", {})),
            source_refs=list(raw.get("source_refs", [])),
            active_artifact_refs=list(raw.get("active_artifact_refs", [])),
            lens_source=dict(raw.get("lens_source", {})),
        )
        for key, value in dict(raw.get("field_map", {}) or {}).items():
            if not isinstance(value, dict):
                continue
            task.field_map[key] = FieldValue(
                value=value.get("value"),
                source=FieldSource(value.get("source", FieldSource.DEFAULT.value)),
                confidence=float(value.get("confidence", 0.0) or 0.0),
                inferred=bool(value.get("inferred", False)),
                confirmed=bool(value.get("confirmed", False)),
                notes=list(value.get("notes", [])),
            )
        return task


@dataclass
class ExecutionRequest:
    controller_kind: ControllerKind
    selected_tool: str | None = None
    normalized_args: dict[str, Any] = field(default_factory=dict)
    execution_style: ToolExecutionStyle = ToolExecutionStyle.INLINE
    execution_constraints: dict[str, Any] = field(default_factory=dict)
    user_visible_summary: str = ""
    should_execute: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ExecutionRequest":
        raw = data or {}
        return cls(
            controller_kind=ControllerKind(raw.get("controller_kind", ControllerKind.DIRECT.value)),
            selected_tool=raw.get("selected_tool"),
            normalized_args=dict(raw.get("normalized_args", {})),
            execution_style=ToolExecutionStyle(raw.get("execution_style", ToolExecutionStyle.INLINE.value)),
            execution_constraints=dict(raw.get("execution_constraints", {})),
            user_visible_summary=raw.get("user_visible_summary", ""),
            should_execute=bool(raw.get("should_execute", True)),
        )


@dataclass
class InterpretationResult:
    envelope: TurnEnvelope
    task_frame: TaskFrame
    execution: ExecutionRequest | None = None
    immediate_reply: str | None = None


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
    context_policy: str = ContextPolicy.TASK_LOCAL.value
    active_task_frame: dict[str, Any] | None = None
    active_execution: dict[str, Any] | None = None
    active_run_id: str | None = None
    pending_runs: list[dict[str, Any]] = field(default_factory=list)
    active_lens_ref: str | None = None
    active_source_ref: dict[str, Any] = field(default_factory=dict)
    last_artifacts: list[dict[str, Any]] = field(default_factory=list)
    last_analysis_bundle: dict[str, Any] = field(default_factory=dict)
    last_metrics: dict[str, Any] = field(default_factory=dict)
    design_draft: dict[str, Any] = field(default_factory=dict)
    requested_next_step: str | None = None
    pending_action: str | None = None
    pending_approval: dict[str, Any] | None = None
    approved_action: str | None = None
    retry_counters: dict[str, int] = field(default_factory=dict)
    loop_trace: list[dict[str, Any]] = field(default_factory=list)
    loop_history: list[dict[str, Any]] = field(default_factory=list)
    active_plan: dict[str, Any] | None = None
    repair_history: list[dict[str, Any]] = field(default_factory=list)
    last_summary_tag: str = "session"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DeepLensState":
        raw = data or {}
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in raw.items() if k in allowed}
        return cls(**filtered)


@dataclass
class LoopAction:
    kind: Literal["ask_user", "request_approval", "tool_call", "respond", "finish", "fail"]
    name: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass
class ToolResult:
    ok: bool
    summary: str
    status: str = "completed"
    outcome_type: OutcomeType = OutcomeType.SUCCESS
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    retryable: bool = False
    run_id: str | None = None
    should_end_turn: bool = False
    needs_input: bool = False
    missing_fields: list[str] = field(default_factory=list)
    state_updates: dict[str, Any] = field(default_factory=dict)
    recommended_next_actions: list[str] = field(default_factory=list)
    user_visible_summary: str | None = None
    cost_class: str | None = None
    tool_name: str | None = None
    raw_status: str | None = None


INTERPRETATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domain_hint": {"type": "string", "enum": [x.value for x in DomainHint]},
        "intent": {"type": "string", "enum": [x.value for x in IntentType]},
        "task_shape": {"type": "string", "enum": [x.value for x in TaskShape]},
        "workflow_family": {"type": "string", "enum": [x.value for x in WorkflowFamily]},
        "preferred_tool": {"type": ["string", "null"]},
        "context_policy": {"type": "string", "enum": [x.value for x in ContextPolicy]},
        "expected_output": {"type": "string", "enum": [x.value for x in OutputType]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "design_spec": {
            "type": "object",
            "properties": {
                "fov": {"type": ["number", "null"]},
                "fnum": {"type": ["number", "null"]},
                "foclen": {"type": ["number", "null"]},
                "imgh": {"type": ["number", "null"]},
                "bfl": {"type": ["number", "null"]},
                "thickness": {"type": ["number", "null"]},
                "aperture": {"type": ["number", "null"]},
                "save_name": {"type": ["string", "null"]},
                "baseline_analysis": {"type": ["boolean", "null"]},
                "surf_list": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "wavelength": {
                    "type": ["object", "null"],
                    "properties": {
                        "value": {"type": ["number", "null"]},
                        "unit": {"type": ["string", "null"]},
                    },
                    "required": ["value", "unit"],
                    "additionalProperties": False,
                },
            },
            "required": [
                "fov",
                "fnum",
                "foclen",
                "imgh",
                "bfl",
                "thickness",
                "aperture",
                "save_name",
                "baseline_analysis",
                "surf_list",
                "wavelength",
            ],
            "additionalProperties": False,
        },
        "analysis_request": {
            "type": "object",
            "properties": {
                "mode": {"type": ["string", "null"], "enum": ["full", "mtf", "spot", "rms", None]},
                "analysis_mode": {"type": ["string", "null"], "enum": ["full", "mtf", "spot", "rms", None]},
                "source_ref": {
                    "type": ["object", "null"],
                    "properties": {
                        "name": {"type": ["string", "null"]},
                        "kind": {"type": ["string", "null"]},
                        "path": {"type": ["string", "null"]},
                        "artifact_id": {"type": ["string", "null"]},
                    },
                    "required": ["name", "kind", "path", "artifact_id"],
                    "additionalProperties": False,
                },
                "attachment": {
                    "type": ["object", "null"],
                    "properties": {
                        "name": {"type": ["string", "null"]},
                        "kind": {"type": ["string", "null"]},
                        "ext": {"type": ["string", "null"]},
                        "mime_type": {"type": ["string", "null"]},
                    },
                    "required": ["name", "kind", "ext", "mime_type"],
                    "additionalProperties": False,
                },
                "attachments": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": ["string", "null"]},
                            "kind": {"type": ["string", "null"]},
                            "ext": {"type": ["string", "null"]},
                            "mime_type": {"type": ["string", "null"]},
                        },
                        "required": ["name", "kind", "ext", "mime_type"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["mode", "analysis_mode", "source_ref", "attachment", "attachments"],
            "additionalProperties": False,
        },
        "run_request": {
            "type": "object",
            "properties": {
                "run_id": {"type": ["string", "null"]},
                "timeout_s": {"type": ["number", "null"]},
                "use_stub": {"type": ["boolean", "null"]},
                "lens_source": {
                    "type": ["object", "null"],
                    "properties": {
                        "name": {"type": ["string", "null"]},
                        "kind": {"type": ["string", "null"]},
                        "path": {"type": ["string", "null"]},
                        "artifact_id": {"type": ["string", "null"]},
                    },
                    "required": ["name", "kind", "path", "artifact_id"],
                    "additionalProperties": False,
                },
            },
            "required": ["run_id", "timeout_s", "use_stub", "lens_source"],
            "additionalProperties": False,
        },
        "delivery_request": {
            "type": "object",
            "properties": {
                "export_format": {"type": ["string", "null"], "enum": ["json", "zmx", None]},
                "formats": {
                    "type": ["array", "null"],
                    "items": {"type": "string", "enum": ["json", "zmx"]},
                },
                "source_ref": {
                    "type": ["object", "null"],
                    "properties": {
                        "name": {"type": ["string", "null"]},
                        "kind": {"type": ["string", "null"]},
                        "path": {"type": ["string", "null"]},
                        "artifact_id": {"type": ["string", "null"]},
                    },
                    "required": ["name", "kind", "path", "artifact_id"],
                    "additionalProperties": False,
                },
                "source_artifact": {
                    "type": ["object", "null"],
                    "properties": {
                        "name": {"type": ["string", "null"]},
                        "kind": {"type": ["string", "null"]},
                        "path": {"type": ["string", "null"]},
                        "artifact_id": {"type": ["string", "null"]},
                    },
                    "required": ["name", "kind", "path", "artifact_id"],
                    "additionalProperties": False,
                },
            },
            "required": ["export_format", "formats", "source_ref", "source_artifact"],
            "additionalProperties": False,
        },
        "missing_fields": {
            "type": "array",
            "items": {"type": "string", "enum": MISSING_FIELD_CODES},
            "maxItems": 8,
        },
        "notes": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
        },
    },
    "required": [
        "domain_hint",
        "intent",
        "task_shape",
        "workflow_family",
        "preferred_tool",
        "context_policy",
        "expected_output",
        "confidence",
        "reason",
        "design_spec",
        "analysis_request",
        "run_request",
        "delivery_request",
        "missing_fields",
        "notes",
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
        tags=["ag.deeplens.v4", "runtime"],
        meta={"agent": "deeplens_agent_v4"},
        severity=1,
    )


class RepairAction(str, Enum):
    NONE = "none"
    RETRY_SAME_TOOL = "retry_same_tool"
    RETRY_WITH_UPDATED_ARGS = "retry_with_updated_args"
    ASK_USER = "ask_user"
    REFRAME_TASK = "reframe_task"
    FAIL = "fail"


@dataclass
class RepairResult:
    action: RepairAction = RepairAction.NONE
    repaired_task_frame: dict[str, Any] | None = None
    repaired_execution: dict[str, Any] | None = None
    ask_user_message: str | None = None
    retry_reason: str | None = None
    failure_reason: str | None = None
    notes: list[str] = field(default_factory=list)
