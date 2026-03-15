# context_policy.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .types import ContextPolicy, DeepLensState, TaskFrame


@dataclass
class ContextBundle:
    policy: ContextPolicy
    recent_chat: list[dict[str, Any]] = field(default_factory=list)
    long_term_memory: list[dict[str, Any]] = field(default_factory=list)
    active_source_ref: dict[str, Any] = field(default_factory=dict)
    active_artifacts: list[dict[str, Any]] = field(default_factory=list)
    task_state: dict[str, Any] = field(default_factory=dict)
    run_state: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _rank(policy: ContextPolicy) -> int:
    return {
        ContextPolicy.MINIMAL: 0,
        ContextPolicy.TASK_LOCAL: 1,
        ContextPolicy.ARTIFACT_CENTRIC: 2,
        ContextPolicy.MEMORY_AUGMENTED: 3,
        ContextPolicy.FULL: 4,
    }[policy]


def choose_context_policy(task: TaskFrame, state: DeepLensState | None = None) -> ContextPolicy:
    task_policy = task.context_policy
    if task_policy == ContextPolicy.MINIMAL:
        wf = task.workflow_family.value
        if wf == "run_control":
            task_policy = ContextPolicy.MINIMAL
        elif wf == "analysis":
            task_policy = ContextPolicy.ARTIFACT_CENTRIC
        elif wf in {"design", "optimization"}:
            task_policy = ContextPolicy.TASK_LOCAL
        elif wf == "export":
            task_policy = ContextPolicy.ARTIFACT_CENTRIC
        else:
            task_policy = ContextPolicy.MINIMAL

    if state is None:
        return task_policy

    try:
        persisted = ContextPolicy(state.context_policy)
    except Exception:
        persisted = ContextPolicy.TASK_LOCAL

    if persisted == ContextPolicy.FULL:
        return ContextPolicy.FULL
    if persisted == ContextPolicy.TASK_LOCAL and _rank(task_policy) > _rank(ContextPolicy.ARTIFACT_CENTRIC):
        return ContextPolicy.TASK_LOCAL
    return task_policy


async def _load_recent_chat(context: Any, *, limit: int) -> list[dict[str, Any]]:
    try:
        mem = context.memory()
        return await mem.recent_chat(limit=limit)
    except Exception:
        return []


async def _load_memory_events(context: Any, *, limit: int) -> list[dict[str, Any]]:
    try:
        mem = context.memory()
        return await mem.search_memory(limit=limit)
    except Exception:
        return []


def _artifact_tail(state: DeepLensState, n: int = 5) -> list[dict[str, Any]]:
    return list(state.last_artifacts[-n:])


def _task_state(task: TaskFrame) -> dict[str, Any]:
    return {
        "user_goal": task.user_goal,
        "workflow_family": task.workflow_family.value,
        "preferred_tool": task.preferred_tool,
        "design_spec": dict(task.design_spec or {}),
        "analysis_request": dict(task.analysis_request or {}),
        "run_request": dict(task.run_request or {}),
        "delivery_request": dict(task.delivery_request or {}),
        "missing_fields": list(task.missing_fields or []),
    }


def _run_state(state: DeepLensState) -> dict[str, Any]:
    return {
        "active_run_id": state.active_run_id,
        "pending_runs": list(state.pending_runs[-5:]),
    }


async def build_context_bundle(
    *,
    task: TaskFrame,
    state: DeepLensState,
    context: Any,
) -> ContextBundle:
    policy = choose_context_policy(task, state)

    bundle = ContextBundle(
        policy=policy,
        active_source_ref=dict(state.active_source_ref or {}),
        active_artifacts=_artifact_tail(state, 5),
        task_state=_task_state(task),
        run_state=_run_state(state),
    )

    if policy == ContextPolicy.MINIMAL:
        bundle.notes.append("Loaded minimal context.")
        return bundle

    if policy == ContextPolicy.TASK_LOCAL:
        bundle.recent_chat = await _load_recent_chat(context, limit=12)
        bundle.notes.append("Loaded task-local recent chat.")
        return bundle

    if policy == ContextPolicy.ARTIFACT_CENTRIC:
        bundle.recent_chat = await _load_recent_chat(context, limit=8)
        bundle.notes.append("Loaded artifact-centric context.")
        return bundle

    if policy == ContextPolicy.MEMORY_AUGMENTED:
        bundle.recent_chat = await _load_recent_chat(context, limit=10)
        bundle.long_term_memory = await _load_memory_events(context, limit=8)
        bundle.notes.append("Loaded memory-augmented context.")
        return bundle

    if policy == ContextPolicy.FULL:
        bundle.recent_chat = await _load_recent_chat(context, limit=20)
        bundle.long_term_memory = await _load_memory_events(context, limit=15)
        bundle.notes.append("Loaded full context.")
        return bundle

    bundle.notes.append("Loaded fallback minimal context.")
    return bundle
