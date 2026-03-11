from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .agenda import mark_action_status, next_pending_action
from .agenda_planner import build_action_agenda
from .extraction import build_missing_prompt, resolve_task_fields
from .memory_policy import build_context_bundle
from .tool_dispatch import dispatch_tool_action
from .types import (
    ActionAgenda,
    AgendaActionStatus,
    AgendaStatus,
    DeepLensTask,
    ResponseOutcomeKind,
    TaskShape,
    ToolResult,
    save_state,
)


def _append_trace(state: Any, item: dict[str, Any]) -> None:
    state.loop_trace.append(item)
    state.loop_trace = state.loop_trace[-50:]


def _increment_retry(state: Any, key: str) -> int:
    state.retry_counters[key] = state.retry_counters.get(key, 0) + 1
    return state.retry_counters[key]


def _reset_retry(state: Any, key: str) -> None:
    state.retry_counters.pop(key, None)


def _sync_active_task(state: Any, task: DeepLensTask) -> None:
    state.active_task = asdict(task)


def _sync_active_agenda(state: Any, agenda: ActionAgenda | None) -> None:
    state.active_agenda = agenda.to_dict() if agenda is not None else None


def _format_action_detail(action: Any) -> str:
    if action.kind == "tool_call":
        return f"tool_call:{action.name or 'unknown'}"
    if action.kind == "request_approval":
        return f"approval:{action.name or 'next tool'}"
    if action.kind == "ask_user":
        return f"ask_user:{str(action.args.get('prompt') or 'missing information')[:80]}"
    return f"{action.kind}:{str(action.args.get('text') or action.rationale or action.kind)[:80]}"


def _max_steps(task_shape: TaskShape) -> int:
    return {
        TaskShape.DIRECT_ANSWER: 1,
        TaskShape.SINGLE_ACTION: 4,
        TaskShape.MULTI_STEP: 8,
        TaskShape.RUN_CONTROL: 4,
        TaskShape.UNSUPPORTED: 1,
    }[task_shape]


async def _emit_loop_update(
    chan: Any,
    *,
    phase: str,
    phase_status: str,
    label: str,
    detail: str,
    step_index: int,
    kind: str,
    note_text: str | None = None,
    tool_name: str | None = None,
    loop_status: str | None = None,
) -> None:
    await chan.send_phase(phase=phase, status=phase_status, label=label, detail=detail)
    if note_text:
        await chan.send_text(
            note_text,
            memory_log=True,
            memory_tags=["ag.deeplens.v6.progress", f"loop_kind:{kind}"],
            memory_data={
                "step_index": step_index,
                "kind": kind,
                "tool_name": tool_name,
                "status": loop_status or phase_status,
            },
            memory_severity=1,
        )


async def _save_loop_state(task: DeepLensTask, state: Any, context: Any, agenda: ActionAgenda | None) -> None:
    _sync_active_task(state, task)
    _sync_active_agenda(state, agenda)
    await save_state(context=context, state=state)


async def _refresh_loop_context(task: DeepLensTask, state: Any, context: Any, context_mode: Any, agenda: ActionAgenda | None) -> Any:
    await _save_loop_state(task, state, context, agenda)
    return await build_context_bundle(context_mode=context_mode, task=task, state=state, context=context)


def _completion_reply(task: DeepLensTask, state: Any, last_tool_result: ToolResult | None) -> str:
    if last_tool_result is not None:
        return last_tool_result.summary
    if state.next_action_hints:
        return f"Completed the requested workflow.\n\n{state.next_action_hints[0]}"
    if task.requested_capabilities:
        return f"Completed the requested workflow: {' -> '.join(task.requested_capabilities)}."
    return "Completed the requested workflow."


async def run_loop(
    *,
    task: DeepLensTask,
    state: Any,
    context_bundle: Any,
    context_mode: Any,
    context: Any,
) -> dict[str, Any]:
    chan = context.channel("ui:session")
    await _emit_loop_update(
        chan,
        phase="execution",
        phase_status="active",
        label="Running loop",
        detail=f"domain={task.domain_hint.value} shape={task.task_shape.value}",
        step_index=0,
        kind="start",
        note_text=f"I’m handling this as a {task.domain_hint.value} workflow.",
    )
    agenda = ActionAgenda.from_dict(state.active_agenda)
    if agenda is None:
        context_bundle = await _refresh_loop_context(task, state, context, context_mode, agenda)
        agenda = await build_action_agenda(task=task, state=state, context_bundle=context_bundle, context=context)
        _sync_active_agenda(state, agenda)

    result_out: dict[str, Any] | None = None
    last_tool_result: ToolResult | None = None

    try:
        for step_index in range(_max_steps(task.task_shape)):
            context_bundle = await _refresh_loop_context(task, state, context, context_mode, agenda)
            action = next_pending_action(agenda)
            if action is None:
                agenda.status = AgendaStatus.COMPLETED
                result_out = {
                    "reply": _completion_reply(task, state, last_tool_result),
                    "state": state,
                    "outcome_kind": ResponseOutcomeKind.COMPLETE,
                    "last_tool_result": last_tool_result,
                }
                return result_out

            await chan.send_phase(
                phase="execution.step",
                status="active",
                label=f"Loop step {step_index + 1}",
                detail=f"Proposed {_format_action_detail(action)}",
            )

            if action.kind == "ask_user":
                prompt = action.args.get("prompt") or build_missing_prompt(task)
                action.status = AgendaActionStatus.BLOCKED
                agenda.status = AgendaStatus.WAITING
                await _emit_loop_update(
                    chan,
                    phase="execution.step",
                    phase_status="active",
                    label=f"Loop step {step_index + 1}",
                    detail="Waiting for user input.",
                    step_index=step_index,
                    kind=action.kind,
                    note_text="I need a few required inputs before I can continue.",
                    loop_status="waiting_for_user",
                )
                state.pending_action = "ask_user"
                await _save_loop_state(task, state, context, agenda)
                reply = await chan.ask_text_or_files(prompt=prompt)
                answer = str(reply.get("text") or "")
                files = [item for item in list(reply.get("files") or []) if isinstance(item, dict)]
                if answer:
                    task.notes.append(f"user_answer:{answer}")
                if files:
                    task.attachments.extend(files)
                    task.source_refs.extend(files)
                await resolve_task_fields(
                    task=task,
                    state=state,
                    message=answer,
                    attachments=files,
                    context=context,
                )
                state.pending_action = None
                state.next_action_hints = []
                context_bundle = await _refresh_loop_context(task, state, context, context_mode, agenda)
                agenda = await build_action_agenda(task=task, state=state, context_bundle=context_bundle, context=context)
                continue

            if action.kind == "request_approval":
                prompt = action.args.get("approval_prompt") or action.args.get("prompt") or "Approve this action?"
                target_name = action.name or task.preferred_tool
                action.status = AgendaActionStatus.BLOCKED
                agenda.status = AgendaStatus.WAITING
                await _emit_loop_update(
                    chan,
                    phase="execution.step",
                    phase_status="active",
                    label=f"Loop step {step_index + 1}",
                    detail=f"Requesting approval for {target_name or 'next action'}.",
                    step_index=step_index,
                    kind=action.kind,
                    note_text=f"I’m ready to proceed with `{target_name}` and need your approval first." if target_name else "I’m ready to proceed and need your approval first.",
                    tool_name=target_name,
                    loop_status="awaiting_approval",
                )
                state.pending_action = "request_approval"
                state.pending_approval = {"prompt": prompt, "step_index": step_index, "action": target_name}
                await _save_loop_state(task, state, context, agenda)
                resp = await chan.ask_approval(prompt=prompt, options=["Approve", "Reject"])
                approved = bool(resp.get("approved"))
                state.pending_action = None
                state.pending_approval = None
                if not approved:
                    mark_action_status(agenda, action.action_id, AgendaActionStatus.CANCELED)
                    result_out = {
                        "reply": "Okay, I stopped before making changes.",
                        "state": state,
                        "outcome_kind": ResponseOutcomeKind.COMPLETE,
                        "approval_prompt": prompt,
                    }
                    return result_out
                state.approved_action = target_name
                mark_action_status(agenda, action.action_id, AgendaActionStatus.COMPLETED)
                continue

            if action.kind == "tool_call":
                tool_name = action.name
                state.pending_action = tool_name
                await _emit_loop_update(
                    chan,
                    phase="execution.step",
                    phase_status="active",
                    label=f"Loop step {step_index + 1}",
                    detail=f"Executing {tool_name or 'tool'}.",
                    step_index=step_index,
                    kind=action.kind,
                    note_text=f"I’m executing `{tool_name or 'tool'}` now.",
                    tool_name=tool_name,
                    loop_status="running",
                )
                await _save_loop_state(task, state, context, agenda)
                result = await dispatch_tool_action(
                    action={
                        "kind": action.kind,
                        "name": action.name,
                        "args": action.args,
                        "rationale": action.rationale,
                    },
                    task=task,
                    state=state,
                    context_bundle=context_bundle,
                    context=context,
                )
                last_tool_result = result
                _append_trace(
                    state,
                    {
                        "step_index": step_index,
                        "tool_name": tool_name,
                        "summary": result.summary,
                        "ok": result.ok,
                        "status": result.status,
                    },
                )
                state.pending_action = None
                if result.ok:
                    _reset_retry(state, tool_name or "tool")
                    mark_action_status(agenda, action.action_id, AgendaActionStatus.COMPLETED)
                    await _emit_loop_update(
                        chan,
                        phase="execution.step",
                        phase_status="done" if result.should_end_turn else "active",
                        label=f"Loop step {step_index + 1}",
                        detail=f"{tool_name or 'tool'} -> {result.status}",
                        step_index=step_index,
                        kind=action.kind,
                        note_text=result.summary,
                        tool_name=tool_name,
                        loop_status=result.status,
                    )
                    if result.should_end_turn:
                        result_out = {
                            "reply": result.summary,
                            "state": state,
                            "outcome_kind": ResponseOutcomeKind.COMPLETE,
                            "last_tool_result": result,
                        }
                        return result_out
                    continue

                retries = _increment_retry(state, tool_name or "tool")
                mark_action_status(agenda, action.action_id, AgendaActionStatus.FAILED)
                if retries > 2:
                    result_out = {
                        "reply": f"I stopped after repeated failures while executing `{tool_name}`.",
                        "state": state,
                        "outcome_kind": ResponseOutcomeKind.ESCALATE,
                        "last_tool_result": result,
                    }
                    return result_out
                continue

            if action.kind == "respond":
                mark_action_status(agenda, action.action_id, AgendaActionStatus.COMPLETED)
                result_out = {
                    "reply": action.args.get("text") or "Done.",
                    "state": state,
                    "outcome_kind": ResponseOutcomeKind.COMPLETE,
                    "last_tool_result": last_tool_result,
                }
                return result_out

            if action.kind == "finish":
                mark_action_status(agenda, action.action_id, AgendaActionStatus.COMPLETED)
                result_out = {
                    "reply": _completion_reply(task, state, last_tool_result),
                    "state": state,
                    "outcome_kind": ResponseOutcomeKind.COMPLETE,
                    "last_tool_result": last_tool_result,
                }
                return result_out

            mark_action_status(agenda, action.action_id, AgendaActionStatus.FAILED)
            result_out = {
                "reply": action.args.get("text") or "I could not determine a safe next action.",
                "state": state,
                "outcome_kind": ResponseOutcomeKind.FAILED,
                "last_tool_result": last_tool_result,
            }
            return result_out

        result_out = {
            "reply": "I stopped after the current step budget.",
            "state": state,
            "outcome_kind": ResponseOutcomeKind.ESCALATE,
            "last_tool_result": last_tool_result,
        }
        return result_out
    finally:
        await _save_loop_state(task, state, context, agenda)
        reply_text = (result_out or {}).get("reply", "Loop ended.")
        await chan.send_phase(
            phase="execution",
            status="done",
            label="Loop finished",
            detail=reply_text[:120],
        )
