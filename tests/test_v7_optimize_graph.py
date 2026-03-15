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

from v7.tool_executor import _exec_dl_optimize
from v7.types import RuntimeState
from v7.workflows import (
    OPTIMIZE_LAUNCHER_GRAPH_ID,
    build_generated_optimize_graph,
    _normalize_request,
    _plan_optimization_intervals,
)


def _run(coro):
    return asyncio.run(coro)


def test_plan_optimization_intervals_uses_checkpoint_boundaries() -> None:
    assert _plan_optimization_intervals(iterations=500, checkpoint_every=100) == [
        (0, 100),
        (100, 200),
        (200, 300),
        (300, 400),
        (400, 500),
    ]
    assert _plan_optimization_intervals(iterations=250, checkpoint_every=100) == [
        (0, 100),
        (100, 200),
        (200, 250),
    ]
    assert _plan_optimization_intervals(iterations=0, checkpoint_every=100) == [(0, 0)]


def test_build_generated_optimize_graph_has_deterministic_interval_nodes() -> None:
    request = _normalize_request(
        lens_source={"artifact_id": "lens-1"},
        goal="optimize lens",
        constraints=[],
        excluded_objectives=[],
        iterations=250,
        checkpoint_every=100,
        export_formats=["json"],
        use_stub=True,
    )
    graph = build_generated_optimize_graph(
        graph_id="deeplens_v7_optimize_run_test",
        request=request,
    )

    node_ids = graph.list_nodes()
    assert "prepare_optimize_request" in node_ids
    assert "load_lens_for_optimization" in node_ids
    assert "optimize_interval_000" in node_ids
    assert "optimize_interval_001" in node_ids
    assert "optimize_interval_002" in node_ids
    assert "export_optimized_lens" in node_ids
    assert "finalize_optimization_result" in node_ids
    assert graph.spec.meta["interval_count"] == 3


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def spawn_run(self, graph_id: str, *, inputs: dict, tags: list[str]) -> str:
        self.calls.append((graph_id, inputs))
        return "run-generated-1"


class _FakeContext:
    def __init__(self) -> None:
        self.run_id = "run-parent-xyz"
        self._runner = _FakeRunner()
        self._registry = _FakeRegistry()

    def runner(self) -> _FakeRunner:
        return self._runner

    def registry(self) -> "_FakeRegistry":
        return self._registry


class _FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def register(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _Task:
    def __init__(self) -> None:
        self.lens_source = {"artifact_id": "lens-1"}
        self.user_goal = "optimize lens"


def test_exec_dl_optimize_spawns_generated_graph_with_empty_inputs(monkeypatch) -> None:
    async def _fake_resolve_lens_source(**kwargs):
        del kwargs
        return {"artifact_id": "lens-1", "path": "lens.json", "name": "lens.json"}

    def _fake_generated_graph_id(*, request, parent_run_id):
        assert request["iterations"] == 500
        assert parent_run_id == "run-parent-xyz"
        return "generated-graph-id"

    def _fake_build_generated_optimize_graph(*, graph_id, request):
        assert graph_id == "generated-graph-id"
        assert request["iterations"] == 500
        return types.SimpleNamespace(spec={"graph_id": graph_id})

    monkeypatch.setattr("v7.tool_executor.resolve_lens_source", _fake_resolve_lens_source)
    monkeypatch.setattr("v7.tool_executor._generated_graph_id", _fake_generated_graph_id)
    monkeypatch.setattr(
        "v7.tool_executor.build_generated_optimize_graph",
        _fake_build_generated_optimize_graph,
    )

    context = _FakeContext()
    state = RuntimeState(active_source_ref={"artifact_id": "lens-1"})
    task = _Task()
    result = _run(
        _exec_dl_optimize(
            resolved_inputs={"graph_id": OPTIMIZE_LAUNCHER_GRAPH_ID},
            task=task,
            state=state,
            context=context,
        )
    )

    assert result.ok is True
    assert context.runner().calls == [("generated-graph-id", {})]
    assert context.registry().calls
