from __future__ import annotations

import asyncio
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

from v7.loop_engine import run_loop
from v7.types import DeepLensTask, Plan, PlanStatus, PlanStep, RuntimeState


def _run(coro):
    return asyncio.run(coro)


class FakeChannel:
    def __init__(self) -> None:
        self.phases: list[dict] = []
        self.approvals: list[str] = []
        self.ask_text_calls: list[str] = []

    async def send_phase(self, **kwargs) -> None:
        self.phases.append(kwargs)

    async def ask_approval(self, prompt: str, options: list[str], **kwargs) -> dict:
        del kwargs, options
        self.approvals.append(prompt)
        return {"approved": False}

    async def ask_text(self, prompt: str, **kwargs) -> str:
        del kwargs
        self.ask_text_calls.append(prompt)
        return ""


class FakeMemory:
    async def record_tool_result(self, **kwargs) -> None:
        del kwargs


class FakeContext:
    def __init__(self) -> None:
        self._channel = FakeChannel()
        self._memory = FakeMemory()

    def channel(self, name: str) -> FakeChannel:
        assert name == "ui:session"
        return self._channel

    def memory(self) -> FakeMemory:
        return self._memory


def test_approval_required_tool_cancels_when_rejected(monkeypatch) -> None:
    async def _fake_execute_tool(*, action, task, state, context):
        del action, task, state, context
        raise AssertionError("tool should not execute when approval is rejected")

    monkeypatch.setattr("v7.loop_engine.execute_tool", _fake_execute_tool)
    context = FakeContext()
    task = DeepLensTask(user_goal="optimize this lens", requested_capabilities=["optimize"], lens_source={"artifact_id": "a1"})
    plan = Plan(goal=task.user_goal, steps=[PlanStep(step_id="step_1", title="Optimize", goal="Optimize", tool_name="ag.spawn_graph")])
    out = _run(run_loop(task=task, plan=plan, state=RuntimeState(), context=context))
    assert out["plan"].status == PlanStatus.CANCELLED
    assert context.channel("ui:session").approvals


def test_missing_design_input_prompts_user(monkeypatch) -> None:
    async def _fake_apply_user_inputs(*, task, text, attachments, context):
        del text, attachments, context
        task.design_spec.update({"fov": 20.0, "fnum": 2.8, "foclen": 50.0})
        return task

    async def _fake_execute_tool(*, action, task, state, context):
        del action, task, state, context
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

    monkeypatch.setattr("v7.loop_engine.apply_user_inputs", _fake_apply_user_inputs)
    monkeypatch.setattr("v7.loop_engine.execute_tool", _fake_execute_tool)
    context = FakeContext()
    task = DeepLensTask(user_goal="design lens", requested_capabilities=["design"])
    plan = Plan(
        goal=task.user_goal,
        steps=[
            PlanStep(
                step_id="step_1",
                title="Create",
                goal="Create",
                tool_name="dl.create_lens",
                required_fields=["fov", "fnum", "foclen_or_imgh"],
            )
        ],
    )
    out = _run(run_loop(task=task, plan=plan, state=RuntimeState(), context=context))
    assert out["plan"].status == PlanStatus.COMPLETED
    assert context.channel("ui:session").ask_text_calls
