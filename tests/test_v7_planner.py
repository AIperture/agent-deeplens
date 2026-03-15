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

from v7.planning import build_task, draft_plan
from v7.types import RuntimeState


class FakeSkills:
    def compile_prompt(self, *args, **kwargs) -> str:
        del args, kwargs
        return "You are a planner."


class FakeLogger:
    def warning(self, *args, **kwargs) -> None:
        del args, kwargs


class FakeLLM:
    async def chat(self, **kwargs):
        del kwargs
        raise RuntimeError("fallback")


class FakeContext:
    def llm(self, profile: str = "default") -> FakeLLM:
        assert profile == "default"
        return FakeLLM()

    def skills(self) -> FakeSkills:
        return FakeSkills()

    def logger(self) -> FakeLogger:
        return FakeLogger()


def _run(coro):
    return asyncio.run(coro)


def test_attachment_only_defaults_to_analysis_plan() -> None:
    state = RuntimeState()
    task = build_task("please analyze this", [{"name": "lens.json", "artifact_id": "a1"}], state)
    plan = _run(draft_plan(task=task, state=state, context_bundle=types.SimpleNamespace(working_state={}), context=FakeContext()))
    assert [step.tool_name for step in plan.steps] == ["dl.analysis"]


def test_design_request_marks_missing_required_fields() -> None:
    state = RuntimeState()
    task = build_task("design a new lens", [], state)
    plan = _run(draft_plan(task=task, state=state, context_bundle=types.SimpleNamespace(working_state={}), context=FakeContext()))
    assert plan.steps[0].tool_name == "dl.create_lens"
    assert plan.steps[0].required_fields == ["fov", "fnum", "foclen_or_imgh"]


def test_status_request_uses_status_tool() -> None:
    state = RuntimeState(active_run_id="run-123")
    task = build_task("check status", [], state)
    plan = _run(draft_plan(task=task, state=state, context_bundle=types.SimpleNamespace(working_state={}), context=FakeContext()))
    assert [step.tool_name for step in plan.steps] == ["ag.status"]
