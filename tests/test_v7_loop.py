from __future__ import annotations

import asyncio
import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
AG_SRC = ROOT.parent / "aethergraph" / "src"
if str(AG_SRC) not in sys.path:
    sys.path.insert(0, str(AG_SRC))

if "aethergraph" not in sys.modules:
    fake_mod = types.ModuleType("aethergraph")

    class _FakeNodeContext:
        pass

    def _graph_fn(**kwargs):
        def _decorator(fn):
            fn._graph_fn_meta = kwargs
            return fn
        return _decorator

    fake_mod.NodeContext = _FakeNodeContext
    fake_mod.graph_fn = _graph_fn
    sys.modules["aethergraph"] = fake_mod

from v7.binding import bind_step
from v7.loop_engine import run_loop
from v7.planning import draft_plan
from v7.types import DeepLensTask, Plan, PlanStatus, PlanStep, RuntimeState


def _run(coro):
    return asyncio.run(coro)


class FakeChannel:
    def __init__(self, *, approval_responses: list[dict] | None = None) -> None:
        self.phases: list[dict] = []
        self.approvals: list[str] = []
        self.sent_texts: list[str] = []
        self.ask_text_calls: list[str] = []
        self.approval_responses = list(approval_responses or [])

    async def send_phase(self, **kwargs) -> None:
        self.phases.append(kwargs)

    async def send_text(self, text: str, **kwargs) -> None:
        del kwargs
        self.sent_texts.append(text)

    async def ask_approval(self, prompt: str, options: list[str], **kwargs) -> dict:
        del kwargs, options
        self.approvals.append(prompt)
        if not self.approval_responses:
            return {"approved": False}
        return self.approval_responses.pop(0)

    async def ask_text(self, prompt: str, **kwargs) -> str:
        del kwargs
        self.ask_text_calls.append(prompt)
        return ""


class FakeMemory:
    async def record_tool_result(self, **kwargs) -> None:
        del kwargs


class FakeLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: str, *args, **kwargs) -> None:
        del args, kwargs
        self.messages.append(message)

    def error(self, message: str, *args, **kwargs) -> None:
        del args, kwargs
        self.messages.append(message)


class FakeLLM:
    def __init__(self, *, failure: bool = False, response: object | None = None) -> None:
        self.failure = failure
        self.response = response

    async def chat(self, **kwargs):
        del kwargs
        if self.failure:
            raise RuntimeError("llm failed")
        return self.response or {"rationale": "ok", "steps": []}, {}


class FakeSkills:
    def compile_prompt(self, *args, **kwargs) -> str:
        del args, kwargs
        return "prompt"


class FakeContext:
    def __init__(self, *, channel: FakeChannel | None = None, llm_failure: bool = False, llm_response: object | None = None) -> None:
        self._channel = channel or FakeChannel()
        self._memory = FakeMemory()
        self._logger = FakeLogger()
        self._llm = FakeLLM(failure=llm_failure, response=llm_response)
        self._skills = FakeSkills()

    def channel(self, name: str) -> FakeChannel:
        assert name == "ui:session"
        return self._channel

    def memory(self) -> FakeMemory:
        return self._memory

    def llm(self, name: str) -> FakeLLM:
        del name
        return self._llm

    def skills(self) -> FakeSkills:
        return self._skills

    def logger(self) -> FakeLogger:
        return self._logger


class FakeContextBundle:
    def __init__(self) -> None:
        self.working_state = {}


def test_plan_confirmation_cancels_before_execution(monkeypatch) -> None:
    async def _fake_execute_tool(*, action, task, state, context):
        del action, task, state, context
        raise AssertionError("tool should not execute when plan confirmation is rejected")

    monkeypatch.setattr("v7.loop_engine.execute_tool", _fake_execute_tool)
    context = FakeContext(channel=FakeChannel(approval_responses=[{"approved": False}]))
    task = DeepLensTask(user_goal="optimize this lens", requested_capabilities=["optimize"], lens_source={"artifact_id": "a1"})
    plan = Plan(goal=task.user_goal, steps=[PlanStep(step_id="step_1", title="Optimize", goal="Optimize", tool_name="dl.optimize")])
    out = _run(run_loop(task=task, plan=plan, state=RuntimeState(), context=context))
    assert out["plan"].status == PlanStatus.CANCELLED
    assert any("Execute it?" in prompt for prompt in context.channel("ui:session").approvals)


def test_plan_confirmation_shows_resolved_args_and_executes(monkeypatch) -> None:
    async def _fake_execute_tool(*, action, task, state, context):
        del task, state, context
        assert action.args["design_spec"]["bfl"] == 3.0
        return types.SimpleNamespace(
            ok=True,
            tool_name="dl.create_lens",
            summary="created",
            artifacts=[],
            state_patch={},
            error_type=None,
            error_message=None,
            should_end_turn=False,
        )

    monkeypatch.setattr("v7.loop_engine.execute_tool", _fake_execute_tool)
    context = FakeContext(channel=FakeChannel(approval_responses=[{"approved": True}]))
    task = DeepLensTask(user_goal="design lens", requested_capabilities=["design"], design_spec={"fov": 20.0, "fnum": 2.8})
    plan = Plan(goal=task.user_goal, steps=[PlanStep(step_id="step_1", title="Create", goal="Create", tool_name="dl.create_lens")])
    out = _run(run_loop(task=task, plan=plan, state=RuntimeState(), context=context))
    assert out["plan"].status == PlanStatus.COMPLETED
    summary = context.channel("ui:session").sent_texts[0]
    assert '"bfl": 3.0' in summary
    assert '"fov": 20.0' in summary
    assert '"fnum": 2.8' in summary


def test_missing_design_input_prompts_only_for_fov_and_fnum() -> None:
    task = DeepLensTask(user_goal="design lens", requested_capabilities=["design"], design_spec={"foclen": 50.0})
    step = PlanStep(step_id="step_1", title="Create", goal="Create", tool_name="dl.create_lens")
    binding = bind_step(step, task, RuntimeState())
    assert not binding.ok
    assert binding.missing_fields == ["fov", "fnum"]


def test_registry_defaults_are_applied() -> None:
    state = RuntimeState(active_source_ref={"artifact_id": "lens-1", "uri": "file:///tmp/lens.json", "name": "lens.json"})
    task = DeepLensTask(user_goal="optimize", requested_capabilities=["optimize"], lens_source={"artifact_id": "lens-1"})
    optimize = bind_step(PlanStep(step_id="s1", title="Optimize", goal="Optimize", tool_name="dl.optimize"), task, state)
    analysis = bind_step(PlanStep(step_id="s2", title="Analyze", goal="Analyze", tool_name="dl.analysis"), task, state)
    create = bind_step(
        PlanStep(step_id="s3", title="Create", goal="Create", tool_name="dl.create_lens", arg_overrides={"design_spec": {"fov": 10.0, "fnum": 2.0}}),
        task,
        state,
    )
    assert optimize.resolved_args["run_request"]["iterations"] == 500
    assert analysis.resolved_args["analysis_request"]["mode"] == "full"
    assert create.resolved_args["design_spec"]["bfl"] == 3.0


def test_fallback_plan_uses_dl_optimize_and_explicit_send_step() -> None:
    context = FakeContext(llm_failure=True)
    task = DeepLensTask(
        user_goal="create and export a lens with fov 20 f/2.8",
        requested_capabilities=["design", "export"],
        design_spec={"fov": 20.0, "fnum": 2.8},
        delivery_request={"formats": ["json"]},
    )
    plan = _run(draft_plan(task=task, state=RuntimeState(), context_bundle=FakeContextBundle(), context=context))
    tool_names = [step.tool_name for step in plan.steps]
    assert "dl.optimize" not in tool_names
    assert "ag.send_file" in tool_names
    assert any("planner llm failed" in message for message in context.logger().messages)


def test_optimize_fallback_uses_new_tool_name() -> None:
    context = FakeContext(llm_failure=True)
    task = DeepLensTask(
        user_goal="optimize this lens",
        requested_capabilities=["optimize"],
        lens_source={"artifact_id": "a1", "uri": "file:///tmp/lens.json", "name": "lens.json"},
    )
    plan = _run(draft_plan(task=task, state=RuntimeState(), context_bundle=FakeContextBundle(), context=context))
    assert [step.tool_name for step in plan.steps] == ["dl.optimize"]


def test_artifact_send_steps_only_appear_when_requested() -> None:
    context = FakeContext(llm_failure=True)
    no_send_task = DeepLensTask(
        user_goal="analyze this lens",
        requested_capabilities=["analysis"],
        lens_source={"artifact_id": "a1", "uri": "file:///tmp/lens.json", "name": "lens.json"},
    )
    send_task = DeepLensTask(
        user_goal="analyze this lens and send the artifacts",
        requested_capabilities=["analysis"],
        lens_source={"artifact_id": "a1", "uri": "file:///tmp/lens.json", "name": "lens.json"},
    )
    no_send_plan = _run(draft_plan(task=no_send_task, state=RuntimeState(), context_bundle=FakeContextBundle(), context=context))
    send_plan = _run(draft_plan(task=send_task, state=RuntimeState(), context_bundle=FakeContextBundle(), context=context))
    assert "ag.send_file" not in [step.tool_name for step in no_send_plan.steps]
    assert "ag.send_file" in [step.tool_name for step in send_plan.steps]
