from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
AG_SRC = ROOT.parent / "aethergraph" / "src"
if str(AG_SRC) not in sys.path:
    sys.path.insert(0, str(AG_SRC))

from template.agent import _finalize_task_lifecycle, _prepare_task_transition
from template.binder import bind_capability_action
from template.context_builder import PromptContextBundle
from template.loop import run_loop
from template.planner import build_plan_agenda
from template.router import route
from template.tools.fake_tools import EXECUTOR_MAP
from template.types import (
    AgendaAction,
    AgendaStatus,
    ContextMode,
    FieldResolution,
    FieldSource,
    PlanAgenda,
    ResponseOutcomeKind,
    TaskShape,
    TemplateState,
    TemplateTask,
    ToolResult,
)


def _run(coro):
    return asyncio.run(coro)


class FakeMemory:
    def __init__(self) -> None:
        self.state: dict = {}
        self.chat: list[dict] = []

    async def latest_state(self, key: str, level: str = "session", user_persistence: bool = True):
        del key, level, user_persistence
        return self.state

    async def record_state(self, key: str, value: dict, tags: list[str], meta: dict, severity: int) -> None:
        del key, tags, meta, severity
        self.state = value

    async def record_chat_user(self, text: str, tags: list[str], data: dict) -> None:
        self.chat.append({"role": "user", "text": text, "tags": tags, "data": data})

    async def recent_chat(self, limit: int, roles: list[str], include_tags: bool, include_ts: bool, level: str, use_persistence: bool):
        del roles, include_tags, include_ts, level, use_persistence
        return self.chat[-limit:]


class FakeChannel:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.phases: list[dict] = []

    async def send_text(self, text: str, **kwargs) -> None:
        del kwargs
        self.texts.append(text)

    async def send_phase(self, **kwargs) -> None:
        self.phases.append(kwargs)


class FakeLLM:
    def __init__(self, responses: list[dict] | None = None) -> None:
        self.responses = list(responses or [])

    async def chat(self, **kwargs):
        del kwargs
        if not self.responses:
            raise RuntimeError("No LLM response queued")
        return json.dumps(self.responses.pop(0)), {}


class FakeContext:
    def __init__(self, llm_responses: list[dict] | None = None) -> None:
        self._memory = FakeMemory()
        self._channel = FakeChannel()
        self._llm = FakeLLM(llm_responses)

    def memory(self) -> FakeMemory:
        return self._memory

    def channel(self, name: str) -> FakeChannel:
        assert name == "ui:session"
        return self._channel

    def llm(self, name: str = "fast") -> FakeLLM:
        assert name == "fast"
        return self._llm


def test_route_classifies_new_continue_and_answer_pending() -> None:
    fresh = TemplateState()
    new_result = _run(route(message="fetch a reference about routers", attachments=[], state=fresh, context=None, user_meta=None))
    assert new_result["turn_role"] == "new_task"
    assert new_result["decision"]["task"].requested_capabilities == ["fetch_reference"]

    active_task = TemplateTask(
        user_goal="fetch reference",
        task_shape=TaskShape.SINGLE_ACTION,
        task_status="waiting",
        requested_capabilities=["fetch_reference"],
    )
    waiting_state = TemplateState(active_task=active_task.to_dict(), pending_action="ask_user", waiting_prompt="Need topic")
    answer_result = _run(route(message="topic=photonics", attachments=[], state=waiting_state, context=None, user_meta=None))
    assert answer_result["turn_role"] == "answer_pending_interaction"
    assert answer_result["decision"]["task"].parsed_args["topic"] == "photonics"

    cont_task = TemplateTask(user_goal="draft a plan", task_shape=TaskShape.MULTI_STEP, task_status="active", requested_capabilities=["draft_plan"])
    cont_state = TemplateState(active_task=cont_task.to_dict())
    cont_result = _run(route(message="continue", attachments=[], state=cont_state, context=None, user_meta=None))
    assert cont_result["turn_role"] == "continue_task"


def test_planner_outputs_bounded_capability_agenda() -> None:
    task = TemplateTask(
        user_goal="fetch a reference and draft a plan",
        task_shape=TaskShape.MULTI_STEP,
        requested_capabilities=["fetch_reference", "draft_plan"],
        parsed_args={"topic": "routing", "objective": "debug the kernel"},
    )
    agenda = _run(build_plan_agenda(task=task, state=TemplateState(), context_bundle=PromptContextBundle(), context=None))
    assert len(agenda.actions) <= 3
    assert [action.kind for action in agenda.actions] == ["bind_and_execute", "bind_and_execute", "finish"]


def test_binder_resolution_precedence_and_defaults() -> None:
    task = TemplateTask(
        user_goal="fetch a reference",
        task_shape=TaskShape.SINGLE_ACTION,
        parsed_args={"topic": "task-topic"},
        field_map={"topic": FieldResolution(value="field-topic", source=FieldSource.TASK_FIELD_MAP, confidence=0.7)},
    )
    state = TemplateState(memory_bag={"topic": "memory-topic"})
    binding = _run(
        bind_capability_action(
            action_id="t1",
            capability_name="fetch_reference",
            planner_args={"topic": "planner-topic"},
            task=task,
            state=state,
            context_bundle=PromptContextBundle(),
            context=None,
        )
    )
    assert binding.executable_action.resolved_inputs["topic"] == "planner-topic"
    assert binding.executable_action.field_resolutions["topic"].source == FieldSource.PLANNER_ARGS
    assert binding.executable_action.resolved_inputs["detail_level"] == "standard"

    task2 = TemplateTask(user_goal="analyze options", task_shape=TaskShape.SINGLE_ACTION)
    state2 = TemplateState(
        memory_bag={"objective": "memory-objective"},
        prior_tool_outputs={"fetch_reference": {"topic": "prior-topic"}},
    )
    binding2 = _run(
        bind_capability_action(
            action_id="t1",
            capability_name="analyze_options",
            planner_args={},
            task=task2,
            state=state2,
            context_bundle=PromptContextBundle(),
            context=None,
        )
    )
    assert binding2.executable_action.resolved_inputs["objective"] == "memory-objective"


def test_binder_missing_fields_becomes_ask_user() -> None:
    binding = _run(
        bind_capability_action(
            action_id="t1",
            capability_name="fetch_reference",
            planner_args={},
            task=TemplateTask(user_goal="fetch", task_shape=TaskShape.SINGLE_ACTION),
            state=TemplateState(),
            context_bundle=PromptContextBundle(),
            context=None,
        )
    )
    assert binding.suggested_action == "ask_user"
    assert binding.executable_action.missing_fields == ["topic"]


def test_approval_enforcement_requires_explicit_receipt() -> None:
    context = FakeContext()
    task = TemplateTask(
        user_goal="apply change",
        task_shape=TaskShape.SINGLE_ACTION,
        requested_capabilities=["apply_change"],
        parsed_args={"change_summary": "rename the placeholder"},
    )
    out = _run(run_loop(task=task, state=TemplateState(), context_bundle=PromptContextBundle(), context_mode=ContextMode.LITE, context=context))
    assert out["outcome_kind"] == ResponseOutcomeKind.WAITING
    assert "Approve" in out["reply"]


def test_deterministic_recovery_normalizes_invalid_inputs_and_retries() -> None:
    context = FakeContext()
    task = TemplateTask(
        user_goal="analyze options",
        task_shape=TaskShape.SINGLE_ACTION,
        requested_capabilities=["analyze_options"],
        parsed_args={"objective": "debug the template", "constraints": ["explode", "keep logs"]},
    )
    out = _run(run_loop(task=task, state=TemplateState(), context_bundle=PromptContextBundle(), context_mode=ContextMode.LITE, context=context))
    assert out["outcome_kind"] == ResponseOutcomeKind.COMPLETE
    assert out["state"].prior_tool_outputs["analyze_options"]["constraints"] == ["keep logs"]


def test_transient_failure_retries_then_succeeds() -> None:
    context = FakeContext()
    state = TemplateState(approval_tokens={"t1": "approved"})
    task = TemplateTask(
        user_goal="apply change",
        task_shape=TaskShape.SINGLE_ACTION,
        requested_capabilities=["apply_change"],
        parsed_args={"change_summary": "transient patch"},
    )
    out = _run(run_loop(task=task, state=state, context_bundle=PromptContextBundle(), context_mode=ContextMode.LITE, context=context))
    assert out["outcome_kind"] == ResponseOutcomeKind.COMPLETE
    assert out["state"].memory_bag["last_applied_change"] == "transient patch"


def test_llm_recovery_fallback_path_when_deterministic_repair_is_insufficient() -> None:
    async def failing_executor(**kwargs):
        del kwargs
        return ToolResult(
            ok=False,
            tool_name="fetch_reference",
            summary="Reference backend returned an unknown execution error.",
            failure_kind=FailureKind.EXECUTION_ERROR,
        )

    from template.types import FailureKind

    with patch.dict(EXECUTOR_MAP, {"fetch_reference": failing_executor}):
        context = FakeContext(
            [
                {"value": None},
                {"decision": "ask_user", "reason": "Need a narrower topic", "prompt": "Which specific topic?"},
            ]
        )
        task = TemplateTask(
            user_goal="fetch a reference",
            task_shape=TaskShape.SINGLE_ACTION,
            requested_capabilities=["fetch_reference"],
            parsed_args={"topic": "systems"},
        )
        out = _run(run_loop(task=task, state=TemplateState(), context_bundle=PromptContextBundle(), context_mode=ContextMode.LITE, context=context))
        assert out["outcome_kind"] == ResponseOutcomeKind.WAITING
        assert out["reply"] == "Which specific topic?"


def test_loop_completion_multi_step_and_step_budget_exhaustion() -> None:
    context = FakeContext()
    task = TemplateTask(
        user_goal="fetch, analyze, plan, summarize",
        task_shape=TaskShape.MULTI_STEP,
        requested_capabilities=["fetch_reference", "analyze_options", "draft_plan", "summarize_result"],
        parsed_args={"topic": "routing", "objective": "stabilize the agent", "constraints": ["keep it small"]},
    )
    out = _run(run_loop(task=task, state=TemplateState(), context_bundle=PromptContextBundle(), context_mode=ContextMode.LITE, context=context))
    assert out["outcome_kind"] == ResponseOutcomeKind.COMPLETE
    assert "latest result came from draft_plan" in out["reply"]

    actions = [AgendaAction(action_id=f"a{i}", kind="bind_and_execute", capability=None if False else None) for i in range(11)]
    for idx, action in enumerate(actions, start=1):
        action.capability = task_capability = build_dummy_capability("inspect_context", "inspect")
        action.action_id = f"x{idx}"
    budget_agenda = PlanAgenda(goal="over budget", actions=actions, status=AgendaStatus.ACTIVE)
    budget_state = TemplateState(active_agenda=budget_agenda.to_dict())
    budget_task = TemplateTask(user_goal="inspect many times", task_shape=TaskShape.MULTI_STEP)
    out_budget = _run(run_loop(task=budget_task, state=budget_state, context_bundle=PromptContextBundle(), context_mode=ContextMode.LITE, context=context))
    assert out_budget["outcome_kind"] == ResponseOutcomeKind.ESCALATE


def build_dummy_capability(name: str, goal: str):
    from template.types import CapabilityIntent

    return CapabilityIntent(capability_name=name, goal=goal, candidate_tools=[name])


def test_state_persistence_and_resumability_after_approval_and_waiting() -> None:
    active_task = TemplateTask(user_goal="apply change", task_shape=TaskShape.SINGLE_ACTION, task_status="active")
    state = TemplateState(active_task=active_task.to_dict(), loop_trace=[{"step": 1}])
    _prepare_task_transition(state, "new_task")
    assert state.loop_history[-1]["task"]["task_status"] == "superseded"

    context = FakeContext()
    waiting_task = TemplateTask(
        user_goal="apply change",
        task_shape=TaskShape.SINGLE_ACTION,
        requested_capabilities=["apply_change"],
        parsed_args={"change_summary": "rename section"},
    )
    waiting_state = TemplateState()
    first = _run(run_loop(task=waiting_task, state=waiting_state, context_bundle=PromptContextBundle(), context_mode=ContextMode.LITE, context=context))
    assert first["outcome_kind"] == ResponseOutcomeKind.WAITING
    waiting_state = first["state"]
    waiting_state.approval_tokens["t1"] = "approved"
    waiting_state.pending_action = None
    waiting_state.pending_approval = None
    waiting_state.active_agenda = None
    second = _run(run_loop(task=waiting_task, state=waiting_state, context_bundle=PromptContextBundle(), context_mode=ContextMode.LITE, context=context))
    assert second["outcome_kind"] == ResponseOutcomeKind.COMPLETE
    _finalize_task_lifecycle(waiting_state, second)
    assert waiting_state.active_task is None
