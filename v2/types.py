from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Literal, TypedDict


DEEPLENS_SKILL_ID = "aethergraph-agent-deeplens"
STATE_KEY = "deeplens_state_v2"
DEBUG = True

class TaskShape(str, Enum):
    DIRECT_ANSWER = "direct_answer"
    SINGLE_ACTION = "single_action"
    MULTI_STEP = "multi_step"
    RUN_CONTROL = "run_control"
    UNSUPPORTED = "unsupported"

@dataclass
class TaskPlan:
    goal: str
    steps: list[str]
    current_step: int = 0
    status: str = "draft"   # draft | approved | active | paused | completed
    notes: list[str] = field(default_factory=list)

class DomainHint(str, Enum):
    CHAT = "chat"
    SIMULATION = "simulation"
    OPTIMIZATION = "optimization"
    DEBUG = "debug"
    UNKNOWN = "unknown"


class ContextMode(str, Enum):
    LITE = "lite"
    FULL = "full"


class ToolExecutionStyle(str, Enum):
    INLINE = "inline"
    SPAWN = "spawn"
    SPAWN_AND_WAIT_SHORT = "spawn_and_wait_short"


class ToolCategory(str, Enum):
    AG = "ag"
    DEEPLENS = "deeplens"


class ApprovalLevel(str, Enum):
    NONE = "none"
    SOFT = "soft"
    HARD = "hard"


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
    notes: list[str] = field(default_factory=list)


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
    active_run_id: str | None = None
    pending_runs: list[dict[str, Any]] = field(default_factory=list)
    active_lens_ref: str | None = None
    last_metrics: dict[str, Any] = field(default_factory=dict)
    last_artifacts: list[dict[str, Any]] = field(default_factory=list)
    pending_action: str | None = None
    pending_approval: dict[str, Any] | None = None
    retry_counters: dict[str, int] = field(default_factory=dict)
    loop_trace: list[dict[str, Any]] = field(default_factory=list)
    loop_history: list[dict[str, Any]] = field(default_factory=list)
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
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    retryable: bool = False
    run_id: str | None = None
    should_end_turn: bool = False


ROUTER_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_shape": {"type": "string", "enum": [x.value for x in TaskShape]},
        "domain_hint": {"type": "string", "enum": [x.value for x in DomainHint]},
        "preferred_tool": {"type": ["string", "null"]},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
        "parsed_args": {"type": "object"},
        "missing_fields": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["task_shape", "domain_hint", "reason", "confidence"],
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
        tags=["ag.deeplens.v2", "runtime"],
        meta={"agent": "deeplens_agent"},
        severity=1,
    )
