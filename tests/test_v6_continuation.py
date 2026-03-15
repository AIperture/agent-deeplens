from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
AG_SRC = ROOT.parent / "aethergraph" / "src"
if str(AG_SRC) not in sys.path:
    sys.path.insert(0, str(AG_SRC))

from v6.agent import _finalize_task_lifecycle, _prepare_task_transition
from v6.router import route
from v6.types import (
    ActionAgenda,
    AgendaStatus,
    DeepLensState,
    DeepLensTask,
    DomainHint,
    ResponseOutcomeKind,
    TaskShape,
)


def _run_route(*, message: str, state: DeepLensState, attachments: list[dict] | None = None):
    return asyncio.run(route(message=message, attachments=attachments or [], state=state, context=None, user_meta=None))


def test_fresh_design_request_does_not_merge_design_draft() -> None:
    state = DeepLensState(design_draft={"fov": 20.0, "fnum": 2.8, "foclen": 50.0})

    result = _run_route(message="design a new lens for me", state=state)

    task = result["decision"]["task"]
    assert result["turn_role"] == "new_task"
    assert task.design_spec == {}
    assert task.missing_fields == ["fov", "fnum", "foclen_or_imgh"]


def test_waiting_task_parameter_answer_resumes_continuation() -> None:
    active_task = DeepLensTask(
        user_goal="Design a new lens",
        task_shape=TaskShape.MULTI_STEP,
        domain_hint=DomainHint.DESIGN,
        task_status="waiting",
        missing_fields=["fov", "fnum", "foclen_or_imgh"],
        requested_capabilities=["design"],
    )
    state = DeepLensState(
        active_task=asdict(active_task),
        runtime_missing_fields=["fov", "fnum", "foclen_or_imgh"],
        pending_action="ask_user",
    )

    result = _run_route(message="fov 20 f/2.8 focal length 50 mm", state=state)

    task = result["decision"]["task"]
    assert result["turn_role"] == "answer_pending_interaction"
    assert task.domain_hint == DomainHint.DESIGN
    assert task.design_spec["fov"] == 20.0
    assert task.design_spec["fnum"] == 2.8
    assert task.design_spec["foclen"] == 50.0
    assert task.missing_fields == []


def test_waiting_task_explicit_new_request_starts_new_task() -> None:
    active_task = DeepLensTask(
        user_goal="Design a new lens",
        task_shape=TaskShape.MULTI_STEP,
        domain_hint=DomainHint.DESIGN,
        task_status="waiting",
        requested_capabilities=["design"],
    )
    state = DeepLensState(active_task=asdict(active_task), pending_action="ask_user")

    result = _run_route(message="analyze this uploaded lens instead", state=state)

    assert result["turn_role"] == "new_task"
    assert result["decision"]["domain_hint"] == DomainHint.ANALYSIS


def test_prepare_task_transition_supersedes_prior_active_task() -> None:
    active_task = DeepLensTask(
        user_goal="Design a new lens",
        task_shape=TaskShape.MULTI_STEP,
        domain_hint=DomainHint.DESIGN,
        task_status="active",
    )
    state = DeepLensState(active_task=asdict(active_task), loop_trace=[{"step": 1}])

    _prepare_task_transition(state, "new_task")

    assert state.active_task["task_status"] == "superseded"
    assert state.loop_history[-1]["task"]["task_status"] == "superseded"


def test_completed_task_is_archived_and_cleared() -> None:
    active_task = DeepLensTask(
        user_goal="Design a new lens",
        task_shape=TaskShape.MULTI_STEP,
        domain_hint=DomainHint.DESIGN,
        task_status="active",
    )
    state = DeepLensState(
        active_task=asdict(active_task),
        active_agenda=ActionAgenda(goal="Design a new lens", status=AgendaStatus.COMPLETED).to_dict(),
        loop_trace=[{"step": 1}],
        pending_action="ask_user",
        runtime_missing_fields=["fov"],
    )

    _finalize_task_lifecycle(state, {"outcome_kind": ResponseOutcomeKind.COMPLETE, "reply": "Done."})

    assert state.active_task is None
    assert state.loop_history[-1]["task"]["task_status"] == "completed"
    assert state.pending_action is None
    assert state.runtime_missing_fields == []
