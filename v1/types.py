from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal, TypedDict


DEEPLENS_SKILL_ID = "aethergraph-agent-deeplens"

class DeepLensIntent(str, Enum):
    CHAT = "chat"
    LENS_DESIGN = "lens_design"
    SIMULATION = "simulation"
    OPTIMIZATION = "optimization"
    VISUALIZATION = "visualization"
    DEBUG = "debug"


class ExecutionMode(str, Enum):
    AUTO = "auto"
    WORKFLOW = "workflow"
    LOOP = "loop"


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


@dataclass
class DeepLensTask:
    user_goal: str
    intent: DeepLensIntent
    execution_preference: ExecutionMode = ExecutionMode.AUTO

    attachments: list[dict[str, Any]] = field(default_factory=list)

    parsed_args: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    preferred_outputs: list[str] = field(default_factory=list)

    active_artifact_refs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class PendingRun:
    run_id: str
    graph_id: str
    intent: str
    status: str = "submitted"
    cancelable: bool = True
    submitted_at: str | None = None
    artifact_refs: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeepLensState:
    agent_mode_enabled: bool = False
    debug_heavy_runs_enabled: bool = False
    context_mode: str = "lite"

    last_intent: DeepLensIntent | None = None
    last_execution_mode: ExecutionMode | None = None

    active_task_id: str | None = None
    active_task: dict[str, Any] | None = None
    pending_action: str | None = None
    pending_approval: dict[str, Any] | None = None

    active_lens_ref: str | None = None
    pending_specs: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    last_metrics: dict[str, Any] = field(default_factory=dict)
    last_artifacts: list[dict[str, Any]] = field(default_factory=list)

    retry_counters: dict[str, int] = field(default_factory=dict)
    loop_trace: list[dict[str, Any]] = field(default_factory=list)

    pending_runs: list[dict[str, Any]] = field(default_factory=list)
    active_run_id: str | None = None

    last_summary_tag: str = "session"

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if self.last_intent is not None:
            out["last_intent"] = self.last_intent.value
        if self.last_execution_mode is not None:
            out["last_execution_mode"] = self.last_execution_mode.value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DeepLensState":
        if not data:
            return cls()
        obj = dict(data)
        if obj.get("last_intent"):
            obj["last_intent"] = DeepLensIntent(obj["last_intent"])
        if obj.get("last_execution_mode"):
            obj["last_execution_mode"] = ExecutionMode(obj["last_execution_mode"])
        return cls(**obj)


class RouterDecision(TypedDict):
    intent: DeepLensIntent
    execution_mode: ExecutionMode
    context_mode: ContextMode
    reason: str
    confidence: float
    task: DeepLensTask


class LoopAction(TypedDict, total=False):
    kind: Literal["ask_user", "request_approval", "tool_call", "respond", "finish", "fail"]
    name: str | None
    args: dict[str, Any]
    rationale: str


@dataclass
class ToolResult:
    ok: bool
    summary: str
    status: str = "completed"  # completed | submitted | waiting | failed | canceled
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
        "intent": {
            "type": "string",
            "enum": [i.value for i in DeepLensIntent],
        },
        "execution_mode": {
            "type": "string",
            "enum": [m.value for m in ExecutionMode],
        },
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
        "parsed_args": {"type": "object"},
        "missing_fields": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["intent", "execution_mode", "reason", "confidence"],
    "additionalProperties": False,
}


async def load_state(context: Any, level: str = "user") -> DeepLensState:
    mem = context.memory()
    raw = await mem.latest_state(
        "deeplens_state",
        level=level,
        user_persistence=True,
    )
    return DeepLensState.from_dict(raw)


async def save_state(context: Any, state: DeepLensState) -> None:
    mem = context.memory()
    await mem.record_state(
        key="deeplens_state",
        value=state.to_dict(),
        tags=["ag.deeplens", "runtime"],
        meta={"agent": "deeplens_agent"},
        severity=1,
    )