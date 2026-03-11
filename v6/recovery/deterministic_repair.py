from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..extraction import build_missing_prompt
from ..planning.agenda_planner import build_action_agenda
from ..tools.tool_arg_refine import normalize_tool_inputs
from ..types import AgendaAction, DeepLensTask, FailureKind, RecoveryDecision, RecoveryDecisionKind, ToolResult


def _merge_runtime_findings(task: DeepLensTask, result: ToolResult) -> None:
    for field_name in result.missing_fields:
        if field_name not in task.missing_fields:
            task.missing_fields.append(field_name)
    if result.invalid_fields:
        task.notes.append(f"runtime_invalid_fields:{result.invalid_fields}")


def _clone_action(action: AgendaAction) -> AgendaAction:
    return AgendaAction(
        action_id=action.action_id,
        kind=action.kind,
        name=action.name,
        args=deepcopy(action.args),
        rationale=action.rationale,
        status=action.status,
    )


def _patch_action_from_task(task: DeepLensTask, action: AgendaAction) -> AgendaAction | None:
    if action.kind != "tool_call":
        return None
    patched = _clone_action(action)
    changed = False
    if action.name == "dl.create_lens":
        patched.args["design_spec"] = deepcopy(task.design_spec)
        changed = True
    if action.name in {"dl.analysis", "dl.export_lens"} and task.lens_source:
        patched.args["lens_source"] = deepcopy(task.lens_source)
        changed = True
    if action.name == "dl.export_lens" and task.delivery_request.get("formats"):
        patched.args["formats"] = list(task.delivery_request["formats"])
        changed = True
    return patched if changed else None


def _normalize_retry_action(action: AgendaAction) -> AgendaAction | None:
    if action.kind != "tool_call" or action.name != "dl.create_lens":
        return None
    patched = _clone_action(action)
    normalized, invalid_fields = normalize_tool_inputs(spec=type("Spec", (), {"name": action.name})(), refined_inputs=patched.args)
    if invalid_fields:
        return None
    if normalized != patched.args:
        patched.args = normalized
        return patched
    return None


async def deterministic_recovery(
    *,
    task: DeepLensTask,
    state: Any,
    agenda: Any,
    failed_action: AgendaAction,
    result: ToolResult,
    context_bundle: Any,
    context: Any,
) -> RecoveryDecision | None:
    _merge_runtime_findings(task, result)

    if result.needs_user_input or result.missing_fields:
        return RecoveryDecision(
            kind=RecoveryDecisionKind.ASK_USER,
            reason="Execution revealed missing required inputs.",
            ask_user_prompt=build_missing_prompt(task),
            task=task,
        )

    normalized_retry = _normalize_retry_action(failed_action)
    if normalized_retry is not None:
        return RecoveryDecision(
            kind=RecoveryDecisionKind.RETRY_ACTION,
            reason="Normalized tool inputs for retry.",
            action=normalized_retry,
            task=task,
        )

    patched = _patch_action_from_task(task, failed_action)
    if patched is not None and patched.args != failed_action.args:
        return RecoveryDecision(
            kind=RecoveryDecisionKind.RETRY_ACTION,
            reason="Patched action arguments from task state.",
            action=patched,
            task=task,
        )

    if (
        result.failure_kind == FailureKind.MISSING_DEPENDENCY
        and failed_action.name in {"dl.analysis", "dl.export_lens"}
        and state.active_source_ref
    ):
        task.lens_source = dict(state.active_source_ref)
        patched = _patch_action_from_task(task, failed_action)
        if patched is not None:
            return RecoveryDecision(
                kind=RecoveryDecisionKind.RETRY_ACTION,
                reason="Retried using the active lens source already in state.",
                action=patched,
                task=task,
            )

    if (
        failed_action.name == "dl.create_lens"
        and result.blocking
        and state.active_source_ref
        and any(cap in {"analysis", "export"} for cap in task.requested_capabilities)
    ):
        task.lens_source = dict(state.active_source_ref)
        fallback_task = deepcopy(task)
        fallback_task.requested_capabilities = [cap for cap in task.requested_capabilities if cap != "design"]
        fallback_task.notes.append("deterministic_replan:using_active_source_after_design_failure")
        replacement_agenda = await build_action_agenda(task=fallback_task, state=state, context_bundle=context_bundle, context=context)
        return RecoveryDecision(
            kind=RecoveryDecisionKind.REPLACE_REMAINING_AGENDA,
            reason="Switched to the active lens source after design creation failed.",
            replacement_actions=replacement_agenda.actions,
            task=fallback_task,
        )

    return None
