from __future__ import annotations

from typing import Any

from .action_select import build_pending_interaction, select_next_action
from .repair import repair_failed_action
from .state import append_trace, set_pending_interaction, set_active_task, update_last_result_summary
from .tool_dispatch import dispatch_tool_action
from .tool_results import normalize_tool_result, apply_tool_result_to_state
from .types import (
    ConversationState,
    LoopAction,
    LoopActionKind,
    LoopOutcome,
    LoopOutcomeKind,
    PendingInteraction,
    PendingInteractionKind,
    ResponseShape,
    TaskFrame,
)


# ---------------------------------------------------------------------
# What to implement here
# ---------------------------------------------------------------------
# - The bounded local task loop
# - One next atomic action at a time
# - AG ask_* integration
# - Tool dispatch / observe / repair / outcome transitions
#
# What should NOT be included here
# - Master conversation-level reinterpretation
# - Big controller split from v4
# - Multi-task planner
# - Rich topic-switch logic
#   (that belongs in the master interpreter / escalation path)
# ---------------------------------------------------------------------


def max_steps_for(task: TaskFrame) -> int:
    if task.task_shape.value == "multi_step":
        return 4
    return 3


def _validate_action_inputs(action: LoopAction, task: TaskFrame) -> tuple[bool, list[str], str]:
    if action.kind != LoopActionKind.TOOL_CALL:
        return True, [], ""

    missing: list[str] = []

    if action.tool_name == "dl.create_lens":
        if "fov" not in task.design_spec:
            missing.append("fov")
        if "fnum" not in task.design_spec:
            missing.append("fnum")
        if "foclen" not in task.design_spec and "imgh" not in task.design_spec:
            missing.append("foclen_or_imgh")

    elif action.tool_name in {"dl.analysis", "dl.export_lens", "ag.spawn_graph"}:
        if not task.lens_source:
            missing.append("lens_source")

    elif action.tool_name in {"ag.status", "ag.cancel"}:
        if not task.run_request.get("run_id"):
            missing.append("run_id")

    if missing:
        prompt = build_pending_interaction(task).prompt
        return False, missing, prompt

    return True, [], ""


def _merge_user_answer_into_task(task: TaskFrame, answer_text: str, files: list[dict[str, Any]]) -> TaskFrame:
    # TODO:
    # - Reuse extraction helpers here
    # - Merge scalar answers into field_map / design_spec / run_request / analysis_request
    # - Merge file answers into lens_source / source_refs
    #
    # Keep this merge logic local and narrow.
    #
    # Do NOT:
    # - fully reinterpret the conversation here
    # - switch tasks here
    # - start tool dispatch here
    task.notes.append(f"user_answer:{answer_text}")
    if files:
        task.source_refs.extend(files)
        if not task.lens_source:
            first = files[0]
            task.lens_source = {
                "name": first.get("name") or first.get("filename") or first.get("uri"),
                "artifact_id": first.get("artifact_id"),
                "uri": first.get("uri"),
            }
    return task


async def _ask_user_via_channel(
    *,
    prompt: str,
    context: Any,
    accept_files: bool = True,
) -> tuple[str, list[dict[str, Any]]]:
    # TODO:
    # Replace with your exact AG channel ask_* primitive.
    #
    # Example shape:
    #   reply = await context.channel().ask_text_or_files(...)
    #
    # Keep this function thin and AG-specific.
    reply = await context.channel().ask_text_or_files(
        prompt=prompt,
        accept_files=accept_files,
    )
    text = getattr(reply, "text", None) or ""
    files = getattr(reply, "files", None) or []
    return text, files


async def _ask_approval_via_channel(*, prompt: str, context: Any) -> str:
    # TODO:
    # Replace with your exact AG approval primitive.
    approval = await context.channel().ask_approval(prompt=prompt)
    # Return a simple normalized string for now.
    if getattr(approval, "approved", False):
        return "approved"
    if getattr(approval, "rejected", False):
        return "rejected"
    return "ambiguous"


async def run_task_loop(
    *,
    task: TaskFrame,
    state: ConversationState,
    context: Any,
) -> LoopOutcome:
    set_active_task(state, task)
    append_trace(state, "loop.start", task_id=task.task_id, preferred_tool=task.preferred_tool)

    for step_idx in range(max_steps_for(task)):
        action = select_next_action(task=task, state=state, context=context)
        append_trace(
            state,
            "loop.action_selected",
            step_idx=step_idx,
            kind=action.kind.value,
            tool_name=action.tool_name,
            reason=action.reason,
        )

        # -------------------------------------------------------------
        # ASK USER
        # -------------------------------------------------------------
        if action.kind == LoopActionKind.ASK_USER:
            pending = PendingInteraction(
                kind=PendingInteractionKind.MISSING_INFO,
                prompt=action.args["prompt"],
                related_task_id=task.task_id,
                expected_fields=list(action.args.get("expected_fields", [])),
                response_shape=ResponseShape(action.args.get("response_shape", ResponseShape.FREE_TEXT.value)),
            )
            set_pending_interaction(state, pending)

            answer_text, files = await _ask_user_via_channel(
                prompt=pending.prompt,
                context=context,
                accept_files=True,
            )

            # If the loop gets here, AG continuation returned to the same flow.
            set_pending_interaction(state, None)
            task = _merge_user_answer_into_task(task, answer_text, files)
            set_active_task(state, task)
            append_trace(state, "loop.ask_user_returned", step_idx=step_idx)
            continue

        # -------------------------------------------------------------
        # REQUEST APPROVAL
        # -------------------------------------------------------------
        if action.kind == LoopActionKind.REQUEST_APPROVAL:
            pending = PendingInteraction(
                kind=PendingInteractionKind.APPROVAL,
                prompt=action.args["prompt"],
                related_task_id=task.task_id,
                response_shape=ResponseShape.YES_NO,
            )
            set_pending_interaction(state, pending)

            approval_status = await _ask_approval_via_channel(
                prompt=pending.prompt,
                context=context,
            )
            set_pending_interaction(state, None)

            if approval_status == "approved":
                append_trace(state, "loop.approval.approved", step_idx=step_idx)
                continue

            if approval_status == "rejected":
                append_trace(state, "loop.approval.rejected", step_idx=step_idx)
                return LoopOutcome(
                    kind=LoopOutcomeKind.FAILED,
                    reason="User rejected the approval request.",
                    task_snapshot=task,
                )

            append_trace(state, "loop.approval.ambiguous", step_idx=step_idx)
            return LoopOutcome(
                kind=LoopOutcomeKind.ESCALATE,
                reason="Approval response was ambiguous; hand back to master interpreter.",
                task_snapshot=task,
                pending_interaction=pending,
            )

        # -------------------------------------------------------------
        # TOOL CALL
        # -------------------------------------------------------------
        if action.kind == LoopActionKind.TOOL_CALL:
            ok, missing, ask_prompt = _validate_action_inputs(action, task)
            if not ok:
                pending = PendingInteraction(
                    kind=PendingInteractionKind.MISSING_INFO,
                    prompt=ask_prompt,
                    related_task_id=task.task_id,
                    expected_fields=missing,
                    response_shape=ResponseShape.FREE_TEXT,
                )
                set_pending_interaction(state, pending)
                append_trace(state, "loop.validation_missing", step_idx=step_idx, missing=missing)
                return LoopOutcome(
                    kind=LoopOutcomeKind.WAITING,
                    reason="Need more information before tool execution.",
                    task_snapshot=task,
                    pending_interaction=pending,
                )

            raw_result = await dispatch_tool_action(
                tool_name=action.tool_name or "",
                task=task,
                state=state,
                context=context,
                override_args=action.args,
            )
            result = normalize_tool_result(raw_result, tool_name=action.tool_name or "")
            update_last_result_summary(state, result.summary)

            append_trace(
                state,
                "loop.tool_result",
                step_idx=step_idx,
                tool_name=result.tool_name,
                ok=result.ok,
                outcome_type=result.outcome_type.value,
                error_code=result.error_code,
            )

            if result.ok:
                apply_tool_result_to_state(result=result, state=state, task=task)
                set_active_task(state, task)

                if result.should_end_turn:
                    return LoopOutcome(
                        kind=LoopOutcomeKind.COMPLETE,
                        reason=result.summary,
                        task_snapshot=task,
                        last_tool_result=result,
                    )
                continue

            repair = await repair_failed_action(
                task=task,
                action=action,
                result=result,
                state=state,
                context=context,
            )

            append_trace(
                state,
                "loop.repair_decision",
                step_idx=step_idx,
                decision=repair.kind.value,
                reason=repair.reason,
            )

            if repair.task is not None:
                task = repair.task
                set_active_task(state, task)

            if repair.kind.value == "retry":
                # NOTE:
                # This skeleton does not immediately re-dispatch the patched action.
                # You can either:
                #   1) loop back and let select_next_action() reconsider, or
                #   2) directly reuse repair.action here.
                continue

            if repair.kind.value == "ask_user":
                set_pending_interaction(state, repair.pending_interaction)
                return LoopOutcome(
                    kind=LoopOutcomeKind.WAITING,
                    reason=repair.reason,
                    task_snapshot=task,
                    pending_interaction=repair.pending_interaction,
                    last_tool_result=result,
                )

            if repair.kind.value == "escalate":
                return LoopOutcome(
                    kind=LoopOutcomeKind.ESCALATE,
                    reason=repair.reason,
                    task_snapshot=task,
                    last_tool_result=result,
                )

            return LoopOutcome(
                kind=LoopOutcomeKind.FAILED,
                reason=repair.reason,
                task_snapshot=task,
                last_tool_result=result,
            )

        # -------------------------------------------------------------
        # RESPOND / FINISH / FAIL
        # -------------------------------------------------------------
        if action.kind == LoopActionKind.RESPOND:
            return LoopOutcome(
                kind=LoopOutcomeKind.COMPLETE,
                reason=action.args.get("text", action.reason),
                task_snapshot=task,
            )

        if action.kind == LoopActionKind.FINISH:
            return LoopOutcome(
                kind=LoopOutcomeKind.COMPLETE,
                reason=action.reason or "Task finished.",
                task_snapshot=task,
            )

        if action.kind == LoopActionKind.FAIL:
            return LoopOutcome(
                kind=LoopOutcomeKind.FAILED,
                reason=action.reason or "Loop selected fail action.",
                task_snapshot=task,
            )

    return LoopOutcome(
        kind=LoopOutcomeKind.ESCALATE,
        reason="Loop budget exceeded; hand back to master interpreter.",
        task_snapshot=task,
    )