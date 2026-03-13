from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any


DEEPLENS_SKILL_ID = "aethergraph-agent-deeplens-v7"
STATE_KEY = "deeplens_state_v7"
SESSION_MEMORY_LEVEL = "session"


class ContextMode(str, Enum):
    LITE = "lite"
    FULL = "full"


class PlanStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class ErrorType(str, Enum):
    MISSING_INPUT = "missing_input"
    INVALID_INPUT = "invalid_input"
    TRANSIENT = "transient"
    UNEXPECTED = "unexpected"
    USER_REJECTED = "user_rejected"


@dataclass
class DeepLensTask:
    user_goal: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    requested_capabilities: list[str] = field(default_factory=list)
    design_spec: dict[str, Any] = field(default_factory=dict)
    analysis_request: dict[str, Any] = field(default_factory=dict)
    run_request: dict[str, Any] = field(default_factory=dict)
    delivery_request: dict[str, Any] = field(default_factory=dict)
    lens_source: dict[str, Any] = field(default_factory=dict)
    response_request: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DeepLensTask | None":
        if not data:
            return None
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in allowed}
        return cls(**filtered)


@dataclass
class PlanStep:
    step_id: str
    title: str
    goal: str
    tool_name: str
    required_fields: list[str] = field(default_factory=list)
    optional_fields: list[str] = field(default_factory=list)
    arg_overrides: dict[str, Any] = field(default_factory=dict)
    tool_policy_id: str = "default_tool"
    interaction_policy_id: str = "missing_input"
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    approval_granted: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanStep":
        raw = dict(data)
        raw["status"] = StepStatus(raw.get("status", StepStatus.PENDING.value))
        return cls(**raw)


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep]
    status: PlanStatus = PlanStatus.DRAFT
    rationale: str = ""
    notes: list[str] = field(default_factory=list)
    current_step: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "steps": [step.to_dict() for step in self.steps],
            "status": self.status.value,
            "rationale": self.rationale,
            "notes": list(self.notes),
            "current_step": self.current_step,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Plan | None":
        if not data:
            return None
        return cls(
            goal=str(data.get("goal") or ""),
            steps=[PlanStep.from_dict(item) for item in data.get("steps", [])],
            status=PlanStatus(data.get("status", PlanStatus.DRAFT.value)),
            rationale=str(data.get("rationale") or ""),
            notes=list(data.get("notes", [])),
            current_step=int(data.get("current_step", 0)),
        )


@dataclass
class RuntimeState:
    context_mode: str = ContextMode.LITE.value
    active_task: dict[str, Any] | None = None
    active_plan: dict[str, Any] | None = None
    active_run_id: str | None = None
    pending_runs: list[dict[str, Any]] = field(default_factory=list)
    active_lens_ref: str | None = None
    active_source_ref: dict[str, Any] = field(default_factory=dict)
    last_metrics: dict[str, Any] = field(default_factory=dict)
    last_analysis_bundle: dict[str, Any] = field(default_factory=dict)
    last_artifacts: list[dict[str, Any]] = field(default_factory=list)
    design_draft: dict[str, Any] = field(default_factory=dict)
    loop_history: list[dict[str, Any]] = field(default_factory=list)
    final_reply: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RuntimeState":
        raw = data or {}
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in raw.items() if k in allowed}
        return cls(**filtered)


@dataclass
class ToolSpec:
    name: str
    executor_key: str
    required_args: list[str]
    optional_args: list[str] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    tool_policy_id: str = "default_tool"
    interaction_policy_id: str | None = None
    description: str = ""


@dataclass
class BoundAction:
    step_id: str
    tool_name: str
    args: dict[str, Any]
    missing_fields: list[str] = field(default_factory=list)


@dataclass
class BindingResult:
    ok: bool
    action: BoundAction | None = None
    missing_fields: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class ToolResult:
    ok: bool
    tool_name: str
    summary: str
    structured_output: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    state_patch: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None
    should_end_turn: bool = False


async def load_state(context: Any, level: str = SESSION_MEMORY_LEVEL) -> RuntimeState:
    raw = await context.memory().latest_state(
        STATE_KEY,
        level=level,
        user_persistence=True,
    )
    return RuntimeState.from_dict(raw)


async def save_state(context: Any, state: RuntimeState) -> None:
    await context.memory().record_state(
        key=STATE_KEY,
        value=state.to_dict(),
        tags=["ag.deeplens.v7", "runtime"],
        meta={"agent": "deeplens_agent_v7"},
        severity=1,
    )
