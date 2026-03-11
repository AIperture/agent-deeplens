from __future__ import annotations

from typing import Any

from .state import set_active_task, set_next_action_hints
from .types import (
    ConversationState,
    DomainHint,
    OutcomeType,
    TaskFrame,
    TaskStatus,
    ToolResult,
)


# ---------------------------------------------------------------------
# What to implement here
# ---------------------------------------------------------------------
# - Normalize raw tool outputs into ToolResult
# - Apply successful tool outputs back into task/state
# - Merge run/artifact/source refs in one place
#
# What should NOT be included here
# - Task interpretation
# - Broad repair logic
# - Multi-step control logic
# ---------------------------------------------------------------------


def _coerce_outcome_type(raw: Any, ok: bool, needs_input: bool) -> OutcomeType:
    if isinstance(raw, OutcomeType):
        return raw

    if isinstance(raw, str):
        lowered = raw.lower().strip()
        for candidate in OutcomeType:
            if lowered == candidate.value:
                return candidate

    if needs_input:
        return OutcomeType.NEEDS_INPUT

    return OutcomeType.SUCCESS if ok else OutcomeType.FAILED


def _dict_get(raw: Any, key: str, default: Any = None) -> Any:
    if isinstance(raw, dict):
        return raw.get(key, default)
    return getattr(raw, key, default)


def normalize_tool_result(raw_result: Any, *, tool_name: str) -> ToolResult:
    """
    Accept a broad variety of raw tool-return shapes.

    Supported patterns:
    - already-normalized ToolResult
    - dict-like payload
    - object with matching attributes
    """
    if isinstance(raw_result, ToolResult):
        return raw_result

    ok = bool(_dict_get(raw_result, "ok", False))
    summary = _dict_get(raw_result, "summary", "") or _dict_get(raw_result, "message", "") or ""
    needs_input = bool(_dict_get(raw_result, "needs_input", False))
    missing_fields = list(_dict_get(raw_result, "missing_fields", []) or [])
    outcome_type = _coerce_outcome_type(
        _dict_get(raw_result, "outcome_type", None),
        ok=ok,
        needs_input=needs_input,
    )

    return ToolResult(
        ok=ok,
        tool_name=_dict_get(raw_result, "tool_name", tool_name) or tool_name,
        summary=summary or ("Completed successfully." if ok else "The tool call failed."),
        outcome_type=outcome_type,
        status=_dict_get(raw_result, "status", "completed"),
        data=dict(_dict_get(raw_result, "data", {}) or {}),
        artifacts=list(_dict_get(raw_result, "artifacts", []) or []),
        warnings=list(_dict_get(raw_result, "warnings", []) or []),
        error_code=_dict_get(raw_result, "error_code", None),
        retryable=bool(_dict_get(raw_result, "retryable", False)),
        run_id=_dict_get(raw_result, "run_id", None),
        needs_input=needs_input,
        missing_fields=missing_fields,
        state_updates=dict(_dict_get(raw_result, "state_updates", {}) or {}),
        recommended_next_actions=list(_dict_get(raw_result, "recommended_next_actions", []) or []),
        user_visible_summary=_dict_get(raw_result, "user_visible_summary", None),
        should_end_turn=bool(_dict_get(raw_result, "should_end_turn", False)),
        raw_status=_dict_get(raw_result, "raw_status", None),
    )


def _apply_run_updates(result: ToolResult, state: ConversationState, task: TaskFrame) -> None:
    if result.run_id:
        state.active_run_id = result.run_id
        task.run_request["run_id"] = result.run_id

    run_update = result.state_updates.get("run") if result.state_updates else None
    if isinstance(run_update, dict) and run_update.get("run_id"):
        state.active_run_id = run_update["run_id"]
        task.run_request["run_id"] = run_update["run_id"]


def _apply_artifact_updates(result: ToolResult, state: ConversationState, task: TaskFrame) -> None:
    if result.artifacts:
        state.last_artifacts.extend(result.artifacts)
        state.last_artifacts = state.last_artifacts[-20:]

        task.active_artifact_refs.extend(
            [
                a.get("artifact_id") or a.get("uri") or a.get("name")
                for a in result.artifacts
                if a.get("artifact_id") or a.get("uri") or a.get("name")
            ]
        )
        task.active_artifact_refs = task.active_artifact_refs[-20:]

    artifact_update = result.state_updates.get("artifacts") if result.state_updates else None
    if isinstance(artifact_update, list):
        state.last_artifacts.extend(artifact_update)
        state.last_artifacts = state.last_artifacts[-20:]


def _apply_source_updates(result: ToolResult, state: ConversationState, task: TaskFrame) -> None:
    source_ref = None

    if result.state_updates:
        source_ref = result.state_updates.get("active_source_ref")

    if not source_ref and result.data:
        source_ref = result.data.get("lens_source") or result.data.get("source_ref")

    if isinstance(source_ref, dict) and source_ref:
        state.active_source_ref = dict(source_ref)
        task.lens_source = dict(source_ref)


def _apply_explicit_state_updates(result: ToolResult, state: ConversationState) -> None:
    updates = dict(result.state_updates or {})
    if isinstance(result.data, dict):
        nested_updates = result.data.get("state_updates")
        if isinstance(nested_updates, dict):
            updates.update(nested_updates)
    for key, value in updates.items():
        if value is not None and hasattr(state, key):
            setattr(state, key, value)


def _apply_design_updates(result: ToolResult, state: ConversationState, task: TaskFrame) -> None:
    design_spec = None

    if result.state_updates:
        design_spec = result.state_updates.get("design_spec")

    if not design_spec and result.data:
        design_spec = result.data.get("design_spec")

    if isinstance(design_spec, dict) and design_spec:
        task.design_spec.update(design_spec)
        state.design_draft.update(design_spec)

    # If create_lens succeeded and returned a lens artifact/source, keep task alive
    # for follow-up analysis/export/optimization.
    if result.ok and task.domain_hint == DomainHint.DESIGN:
        task.status = TaskStatus.ACTIVE


def _apply_analysis_updates(result: ToolResult, state: ConversationState, task: TaskFrame) -> None:
    analysis_data = None

    if result.state_updates:
        analysis_data = result.state_updates.get("analysis")

    if not analysis_data and result.data:
        analysis_data = result.data.get("analysis")

    if isinstance(analysis_data, dict) and analysis_data:
        # Store compact summary only in persistent state.
        summary = analysis_data.get("summary") or result.summary
        if summary:
            state.last_result_summary = summary

    if result.ok and task.domain_hint == DomainHint.ANALYSIS:
        task.status = TaskStatus.COMPLETED if result.should_end_turn else TaskStatus.ACTIVE


def _apply_export_updates(result: ToolResult, state: ConversationState, task: TaskFrame) -> None:
    del state
    if result.ok and task.domain_hint == DomainHint.EXPORT:
        task.status = TaskStatus.COMPLETED if result.should_end_turn else TaskStatus.ACTIVE


def _apply_run_control_updates(result: ToolResult, state: ConversationState, task: TaskFrame) -> None:
    del task
    if result.state_updates:
        run_status = result.state_updates.get("run_status")
        if isinstance(run_status, str) and run_status in {"cancelled", "finished", "completed", "failed"}:
            if run_status in {"cancelled", "finished", "completed", "failed"}:
                state.active_run_id = None


def apply_tool_result_to_state(
    *,
    result: ToolResult,
    state: ConversationState,
    task: TaskFrame,
) -> None:
    """
    Apply successful tool observations back into task/state.

    This function should stay relatively dumb:
    - merge refs
    - merge returned structured data
    - update status

    It should not decide the next action.
    """
    if not result.ok:
        return

    _apply_run_updates(result, state, task)
    _apply_artifact_updates(result, state, task)
    _apply_source_updates(result, state, task)
    _apply_explicit_state_updates(result, state)
    _apply_design_updates(result, state, task)
    _apply_analysis_updates(result, state, task)
    _apply_export_updates(result, state, task)
    _apply_run_control_updates(result, state, task)

    if result.summary:
        state.last_result_summary = result.summary
    set_next_action_hints(state, result.recommended_next_actions)

    # Generic status handling
    if result.outcome_type == OutcomeType.RUN_SUBMITTED:
        task.status = TaskStatus.RUNNING
    elif result.outcome_type == OutcomeType.SUCCESS and task.status not in {TaskStatus.COMPLETED, TaskStatus.RUNNING}:
        task.status = TaskStatus.ACTIVE

    set_active_task(state, task)
