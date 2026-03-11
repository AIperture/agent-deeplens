from __future__ import annotations

from typing import Any

from .action_select import build_pending_interaction, propose_next_action_with_llm, select_next_action
from .extraction import extract_turn_fields
from .input_normalization import normalize_turn
from .memory_policy import PromptContextBundle, build_context_bundle
from .repair import repair_failed_action
from .state import (
    append_trace,
    increment_retry,
    reset_retry,
    set_active_task,
    set_next_action_hints,
    set_pending_interaction,
    update_last_result_summary,
)
from .tool_dispatch import dispatch_tool_action
from .tool_results import apply_tool_result_to_state, normalize_tool_result
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


def max_steps_for(task: TaskFrame) -> int:
    if task.task_shape.value == "multi_step":
        return 5
    return 3


def _task_has_lens_file(task: TaskFrame) -> bool:
    """Check if the task has a usable lens source or lens-like attachment."""
    if task.lens_source:
        return True
    for att in task.attachments:
        name = str(att.get("name") or att.get("filename") or att.get("uri") or "").lower()
        if name.endswith((".json", ".zmx")):
            return True
        if att.get("kind") == "lens":
            return True
    return False


def _recompute_missing_fields(task: TaskFrame) -> None:
    missing: list[str] = []
    if task.domain_hint.value == "design":
        if "fov" not in task.design_spec:
            missing.append("fov")
        if "fnum" not in task.design_spec:
            missing.append("fnum")
        if "foclen" not in task.design_spec and "imgh" not in task.design_spec:
            missing.append("foclen_or_imgh")
    elif task.domain_hint.value in {"analysis", "optimization", "export"}:
        if not _task_has_lens_file(task):
            missing.append("lens_source")
        if task.domain_hint.value == "export" and not task.delivery_request.get("formats"):
            missing.append("export_format")
    elif task.domain_hint.value == "run_control" and not task.run_request.get("run_id"):
        missing.append("run_id")
    task.missing_fields = list(dict.fromkeys(missing))


def _validate_action_inputs(action: LoopAction, task: TaskFrame) -> tuple[bool, list[str], str]:
    if action.kind != LoopActionKind.TOOL_CALL:
        return True, [], ""
    _recompute_missing_fields(task)
    if not task.missing_fields:
        return True, [], ""
    prompt = build_pending_interaction(task).prompt
    return False, list(task.missing_fields), prompt


async def _merge_user_answer_into_task(
    *,
    task: TaskFrame,
    answer_text: str,
    files: list[dict[str, Any]],
    pending: PendingInteraction | None,
    state: ConversationState,
    context: Any,
) -> TaskFrame:
    if answer_text:
        task.notes.append(f"user_answer:{answer_text}")
    if files:
        normalized_files = [dict(item) for item in files if isinstance(item, dict)]
        task.attachments.extend(normalized_files)
        task.source_refs.extend(normalized_files)
        if not task.lens_source:
            first = files[0]
            task.lens_source = {
                "name": first.get("name") or first.get("filename") or first.get("uri"),
                "artifact_id": first.get("artifact_id"),
                "uri": first.get("uri"),
            }
    envelope = normalize_turn(
        message=answer_text,
        attachments=files,
        state=state,
        user_meta={},
    )
    extracted = await extract_turn_fields(
        envelope=envelope,
        pending=pending,
        pre_context={
            "active_task": {
                "task_id": task.task_id,
                "domain_hint": task.domain_hint.value,
                "missing_fields": list(task.missing_fields),
                "preferred_tool": task.preferred_tool,
            },
            "pending_interaction": pending.to_dict() if pending else {},
        },
        context=context,
    )
    for key, field in extracted.field_map.items():
        task.field_map[key] = field
        if key in {"fov", "fnum", "foclen", "imgh"}:
            task.design_spec[key] = field.value
        elif key == "analysis_mode":
            task.analysis_request["mode"] = field.value
        elif key == "export_formats":
            task.delivery_request["formats"] = list(field.value or [])
        elif key == "run_id":
            task.run_request["run_id"] = field.value
        elif key == "approval_response":
            task.run_request["approval_response"] = field.value
        elif key == "lens_source" and isinstance(field.value, dict):
            task.lens_source = dict(field.value)
    task.notes.extend(extracted.notes)
    _recompute_missing_fields(task)
    return task


async def _ask_user_via_channel(
    *,
    prompt: str,
    context: Any,
    accept_files: bool = True,
) -> tuple[str, list[dict[str, Any]]]:
    reply = await context.channel("ui:session").ask_text_or_files(
        prompt=prompt,
        accept_files=accept_files,
    )
    if isinstance(reply, dict):
        return str(reply.get("text") or ""), list(reply.get("files") or [])
    return str(getattr(reply, "text", None) or ""), list(getattr(reply, "files", None) or [])


async def _ask_approval_via_channel(*, prompt: str, context: Any) -> str:
    approval = await context.channel("ui:session").ask_approval(prompt=prompt, options=["Approve", "Reject"])
    approved = getattr(approval, "approved", None)
    rejected = getattr(approval, "rejected", None)
    if isinstance(approval, dict):
        approved = approval.get("approved")
        rejected = approval.get("rejected")
    if approved:
        return "approved"
    if rejected:
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
    chan = context.channel("ui:session")

    await chan.send_phase(
        phase="execution",
        status="active",
        label="Running loop",
        detail=f"domain={task.domain_hint.value} shape={task.task_shape.value}",
    )

    # Build context bundle for LLM-based fallback and tool arg refinement.
    context_bundle: PromptContextBundle | None = None
    try:
        context_bundle = await build_context_bundle(task=task, state=state, context=context)
    except Exception:
        context.logger().warning("deeplens_v5: build_context_bundle failed at loop start", exc_info=True)

    for step_idx in range(max_steps_for(task)):
        action = select_next_action(task=task, state=state, context=context)

        # If deterministic selection returned a low-priority fallback, try LLM.
        if action.priority <= 10 and context_bundle is not None:
            try:
                action = await propose_next_action_with_llm(
                    task=task,
                    state=state,
                    context_bundle=context_bundle,
                    step_index=step_idx,
                    context=context,
                )
            except Exception:
                context.logger().warning("deeplens_v5: LLM action proposer failed, using deterministic fallback", exc_info=True)

        append_trace(
            state,
            "loop.action_selected",
            step_idx=step_idx,
            kind=action.kind.value,
            tool_name=action.tool_name,
            reason=action.reason,
        )

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
            set_pending_interaction(state, None)
            task = await _merge_user_answer_into_task(
                task=task,
                answer_text=answer_text,
                files=files,
                pending=pending,
                state=state,
                context=context,
            )
            set_active_task(state, task)
            append_trace(state, "loop.ask_user_returned", step_idx=step_idx, task_id=task.task_id)
            continue

        if action.kind == LoopActionKind.REQUEST_APPROVAL:
            pending = PendingInteraction(
                kind=PendingInteractionKind.APPROVAL,
                prompt=action.args["prompt"],
                related_task_id=task.task_id,
                response_shape=ResponseShape.YES_NO,
            )
            set_pending_interaction(state, pending)
            approval_status = await _ask_approval_via_channel(prompt=pending.prompt, context=context)
            set_pending_interaction(state, None)
            if approval_status == "approved":
                append_trace(state, "loop.approval.approved", step_idx=step_idx, task_id=task.task_id)
                continue
            if approval_status == "rejected":
                append_trace(state, "loop.approval.rejected", step_idx=step_idx, task_id=task.task_id)
                return LoopOutcome(
                    kind=LoopOutcomeKind.FAILED,
                    reason="User rejected the approval request.",
                    task_snapshot=task,
                )
            append_trace(state, "loop.approval.ambiguous", step_idx=step_idx, task_id=task.task_id)
            return LoopOutcome(
                kind=LoopOutcomeKind.ESCALATE,
                reason="Approval response was ambiguous.",
                task_snapshot=task,
                pending_interaction=pending,
            )

        if action.kind == LoopActionKind.TOOL_CALL:
            ok, missing, ask_prompt = _validate_action_inputs(action, task)
            if not ok:
                # Ask user inline (like v3) instead of exiting the loop.
                pending = PendingInteraction(
                    kind=PendingInteractionKind.MISSING_INFO,
                    prompt=ask_prompt,
                    related_task_id=task.task_id,
                    expected_fields=missing,
                    response_shape=ResponseShape.FREE_TEXT,
                )
                set_pending_interaction(state, pending)
                append_trace(state, "loop.validation_missing", step_idx=step_idx, missing=missing)
                answer_text, files = await _ask_user_via_channel(
                    prompt=ask_prompt,
                    context=context,
                    accept_files=True,
                )
                set_pending_interaction(state, None)
                task = await _merge_user_answer_into_task(
                    task=task,
                    answer_text=answer_text,
                    files=files,
                    pending=pending,
                    state=state,
                    context=context,
                )
                set_active_task(state, task)
                append_trace(state, "loop.validation_ask_returned", step_idx=step_idx, task_id=task.task_id)
                continue

            tool_label = action.tool_name or "tool"
            await chan.send_phase(
                phase="execution.step",
                status="active",
                label=f"Loop step {step_idx + 1}",
                detail=f"Executing {tool_label}.",
            )
            try:
                raw_result = await dispatch_tool_action(
                    tool_name=action.tool_name or "",
                    task=task,
                    state=state,
                    context=context,
                    override_args=action.args,
                )
            except Exception:
                context.logger().error("deeplens_v5: tool dispatch failed", exc_info=True)
                raw_result = {
                    "ok": False,
                    "summary": f"Tool dispatch failed for `{action.tool_name or 'unknown'}`.",
                    "error_code": "dispatch_failed",
                    "retryable": False,
                }

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
            result_phase = "active" if result.ok and not result.should_end_turn else "done"
            await chan.send_phase(
                phase="execution.step",
                status=result_phase,
                label=f"Loop step {step_idx + 1}",
                detail=f"{tool_label} -> {result.status}",
            )

            if result.ok:
                reset_retry(state, result.tool_name or "tool")
                apply_tool_result_to_state(result=result, state=state, task=task)
                set_active_task(state, task)
                if result.should_end_turn:
                    return LoopOutcome(
                        kind=LoopOutcomeKind.COMPLETE,
                        reason=result.user_visible_summary or result.summary,
                        task_snapshot=task,
                        last_tool_result=result,
                    )
                # Refresh context bundle after tool execution for next iteration.
                try:
                    context_bundle = await build_context_bundle(task=task, state=state, context=context)
                except Exception:
                    context.logger().warning("deeplens_v5: context refresh after tool call failed", exc_info=True)
                continue

            retries = increment_retry(state, result.tool_name or "tool")
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
                retries=retries,
            )

            if repair.task is not None:
                task = repair.task
                set_active_task(state, task)

            if repair.kind.value == "retry" and retries <= 2:
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
            if repair.kind.value == "escalate" or retries > 2:
                return LoopOutcome(
                    kind=LoopOutcomeKind.ESCALATE,
                    reason=repair.reason if retries <= 2 else f"Repeated failures while executing `{result.tool_name}`.",
                    task_snapshot=task,
                    last_tool_result=result,
                )
            return LoopOutcome(
                kind=LoopOutcomeKind.FAILED,
                reason=repair.reason,
                task_snapshot=task,
                last_tool_result=result,
            )

        if action.kind == LoopActionKind.RESPOND:
            set_next_action_hints(state, [])
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

    outcome = LoopOutcome(
        kind=LoopOutcomeKind.ESCALATE,
        reason="Loop budget exceeded.",
        task_snapshot=task,
    )
    await chan.send_phase(
        phase="execution",
        status="done",
        label="Loop finished",
        detail=outcome.reason[:120],
    )
    return outcome
