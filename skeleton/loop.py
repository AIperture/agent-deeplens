"""Main bounded execution loop.

Same structure as v6's loop_engine.py but:
- Step budget from policy.MAX_STEPS
- Event emission via events.py
- Arg resolution via arg_resolver (generic, not optics-specific)
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from . import policy
from .agenda import mark_action_status, next_pending_action, replace_pending_tail
from .arg_resolver import build_missing_prompt
from .context_builder import build_context_bundle
from .dispatcher import dispatch_tool_action
from .events import emit_agent_event, emit_loop_update
from .planner import build_action_agenda
from .recovery import recover_failed_action
from .types import (
    ActionAgenda,
    AgendaActionStatus,
    AgendaStatus,
    AgentState,
    RecoveryDecisionKind,
    ResponseOutcomeKind,
    Task,
    TaskShape,
    ToolResult,
    save_state,
)


def _append_trace(state: AgentState, item: dict[str, Any]) -> None:
    state.loop_trace.append(item)
    state.loop_trace = state.loop_trace[-policy.LOOP_TRACE_LIMIT:]


def _increment_retry(state: AgentState, key: str) -> int:
    state.retry_counters[key] = state.retry_counters.get(key, 0) + 1
    return state.retry_counters[key]


def _reset_retry(state: AgentState, key: str) -> None:
    state.retry_counters.pop(key, None)


def _increment_recovery_attempt(state: AgentState, key: str) -> int:
    state.recovery_attempts[key] = state.recovery_attempts.get(key, 0) + 1
    return state.recovery_attempts[key]


def _reset_recovery_attempt(state: AgentState, key: str) -> None:
    state.recovery_attempts.pop(key, None)


def _sync_active_task(state: AgentState, task: Task) -> None:
    state.active_task = asdict(task)


def _sync_active_agenda(state: AgentState, agenda: ActionAgenda | None) -> None:
    state.active_agenda = agenda.to_dict() if agenda is not None else None


def _format_action_detail(action: Any) -> str:
    if action.kind == "tool_call":
        return f"tool_call:{action.name or 'unknown'}"
    if action.kind == "request_approval":
        return f"approval:{action.name or 'next tool'}"
    if action.kind == "ask_user":
        return f"ask_user:{str(action.args.get('prompt') or 'missing information')[:80]}"
    return f"{action.kind}:{str(action.args.get('text') or action.rationale or action.kind)[:80]}"


async def _save_loop_state(task: Task, state: AgentState, context: Any, agenda: ActionAgenda | None) -> None:
    _sync_active_task(state, task)
    _sync_active_agenda(state, agenda)
    await save_state(context, state=state, state_key=policy.STATE_KEY, agent_id=policy.AGENT_ID)


async def _refresh_loop_context(task: Task, state: AgentState, context: Any, context_mode: Any, agenda: ActionAgenda | None) -> Any:
    await _save_loop_state(task, state, context, agenda)
    return await build_context_bundle(context_mode=context_mode, task=task, state=state, context=context)


def _completion_reply(task: Task, state: AgentState, last_tool_result: ToolResult | None) -> str:
    if last_tool_result is not None:
        return last_tool_result.summary
    if state.next_action_hints:
        return f"Completed the requested workflow.\n\n{state.next_action_hints[0]}"
    if task.requested_capabilities:
        return f"Completed the requested workflow: {' -> '.join(task.requested_capabilities)}."
    return "Completed the requested workflow."


async def run_loop(
    *,
    task: Task,
    state: AgentState,
    context_bundle: Any,
    context_mode: Any,
    context: Any,
) -> dict[str, Any]:
    """Main bounded execution loop."""
    task.task_status = "active"
    chan = context.channel("ui:session")

    await emit_loop_update(
        chan,
        phase="execution",
        phase_status="active",
        label="Running loop",
        detail=f"domain={task.domain_hint} shape={task.task_shape.value}",
        step_index=0,
        kind="start",
        note_text=f"I'm handling this as a {task.domain_hint} workflow.",
    )
    await emit_agent_event(chan, event_type="task_started", data={"domain": task.domain_hint, "shape": task.task_shape.value})

    # Build or restore agenda
    agenda = ActionAgenda.from_dict(state.active_agenda)
    if agenda is None:
        context_bundle = await _refresh_loop_context(task, state, context, context_mode, agenda)
        agenda = await build_action_agenda(task=task, state=state, context_bundle=context_bundle, context=context)
        _sync_active_agenda(state, agenda)

    result_out: dict[str, Any] | None = None
    last_tool_result: ToolResult | None = None
    max_steps = policy.MAX_STEPS.get(task.task_shape, 4)

    try:
        for step_index in range(max_steps):
            context_bundle = await _refresh_loop_context(task, state, context, context_mode, agenda)
            action = next_pending_action(agenda)
            if action is None:
                agenda.status = AgendaStatus.COMPLETED
                task.task_status = "completed"
                result_out = {
                    "reply": _completion_reply(task, state, last_tool_result),
                    "state": state,
                    "outcome_kind": ResponseOutcomeKind.COMPLETE,
                    "last_tool_result": last_tool_result,
                }
                await emit_agent_event(chan, event_type="task_completed", data={"reply": result_out["reply"][:120]})
                return result_out

            await chan.send_phase(
                phase="execution.step",
                status="active",
                label=f"Loop step {step_index + 1}",
                detail=f"Proposed {_format_action_detail(action)}",
            )

            # ---------------------------------------------------------------
            # ask_user
            # ---------------------------------------------------------------
            if action.kind == "ask_user":
                prompt = action.args.get("prompt") or build_missing_prompt(task, state)
                action.status = AgendaActionStatus.BLOCKED
                agenda.status = AgendaStatus.WAITING
                task.task_status = "waiting"
                await emit_loop_update(
                    chan, phase="execution.step", phase_status="active",
                    label=f"Loop step {step_index + 1}", detail="Waiting for user input.",
                    step_index=step_index, kind=action.kind,
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
                    # Try to parse answer into parsed_args for simple key=value or numeric
                    _try_parse_answer_into_args(task, answer)
                if files:
                    task.attachments.extend(files)
                state.pending_action = None
                state.next_action_hints = []
                state.runtime_missing_fields = []
                state.runtime_invalid_fields = {}
                state.last_prompt_reason = None
                task.task_status = "active"
                context_bundle = await _refresh_loop_context(task, state, context, context_mode, agenda)
                agenda = await build_action_agenda(task=task, state=state, context_bundle=context_bundle, context=context)
                continue

            # ---------------------------------------------------------------
            # request_approval
            # ---------------------------------------------------------------
            if action.kind == "request_approval":
                prompt = action.args.get("approval_prompt") or action.args.get("prompt") or "Approve this action?"
                target_name = action.name or task.preferred_tool
                action.status = AgendaActionStatus.BLOCKED
                agenda.status = AgendaStatus.WAITING
                task.task_status = "waiting"
                await emit_loop_update(
                    chan, phase="execution.step", phase_status="active",
                    label=f"Loop step {step_index + 1}",
                    detail=f"Requesting approval for {target_name or 'next action'}.",
                    step_index=step_index, kind=action.kind,
                    note_text=f"I need your approval to proceed with `{target_name}`." if target_name else "I need your approval to proceed.",
                    tool_name=target_name, loop_status="awaiting_approval",
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
                    task.task_status = "canceled"
                    result_out = {
                        "reply": "Okay, I stopped before making changes.",
                        "state": state,
                        "outcome_kind": ResponseOutcomeKind.COMPLETE,
                        "approval_prompt": prompt,
                    }
                    return result_out
                state.approved_action = target_name
                task.task_status = "active"
                mark_action_status(agenda, action.action_id, AgendaActionStatus.COMPLETED)
                continue

            # ---------------------------------------------------------------
            # tool_call
            # ---------------------------------------------------------------
            if action.kind == "tool_call":
                tool_name = action.name
                state.pending_action = tool_name
                await emit_loop_update(
                    chan, phase="execution.step", phase_status="active",
                    label=f"Loop step {step_index + 1}",
                    detail=f"Executing {tool_name or 'tool'}.",
                    step_index=step_index, kind=action.kind,
                    note_text=f"Executing `{tool_name or 'tool'}` now.",
                    tool_name=tool_name, loop_status="running",
                )
                await _save_loop_state(task, state, context, agenda)
                result = await dispatch_tool_action(
                    action={
                        "action_id": action.action_id,
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
                _append_trace(state, {
                    "step_index": step_index,
                    "tool_name": tool_name,
                    "summary": result.summary,
                    "ok": result.ok,
                    "status": result.status,
                })
                state.pending_action = None

                if result.ok:
                    state.active_recovery = None
                    _reset_retry(state, tool_name or "tool")
                    _reset_recovery_attempt(state, tool_name or "tool")
                    mark_action_status(agenda, action.action_id, AgendaActionStatus.COMPLETED)
                    await emit_loop_update(
                        chan, phase="execution.step",
                        phase_status="done" if result.should_end_turn else "active",
                        label=f"Loop step {step_index + 1}",
                        detail=f"{tool_name or 'tool'} -> {result.status}",
                        step_index=step_index, kind=action.kind,
                        note_text=result.summary, tool_name=tool_name,
                        loop_status=result.status,
                    )
                    await emit_agent_event(chan, event_type="tool_completed", data={"tool": tool_name, "summary": result.summary[:120]})
                    if result.should_end_turn:
                        task.task_status = "completed"
                        result_out = {
                            "reply": result.summary,
                            "state": state,
                            "outcome_kind": ResponseOutcomeKind.COMPLETE,
                            "last_tool_result": result,
                        }
                        return result_out
                    continue

                # Tool failed -> recovery
                retries = _increment_retry(state, tool_name or "tool")
                mark_action_status(agenda, action.action_id, AgendaActionStatus.FAILED)
                await emit_agent_event(chan, event_type="tool_failed", data={"tool": tool_name, "error": result.summary[:120]})

                recovery = await recover_failed_action(
                    task=task, state=state, agenda=agenda,
                    failed_action=action, result=result,
                    context_bundle=context_bundle, context=context,
                )
                state.active_recovery = {
                    "decision": recovery.kind.value,
                    "reason": recovery.reason,
                    "tool_name": tool_name,
                }

                if recovery.task is not None:
                    task = recovery.task

                if recovery.kind == RecoveryDecisionKind.RETRY_ACTION and recovery.action is not None:
                    recovery_attempts = _increment_recovery_attempt(state, tool_name or "tool")
                    if recovery_attempts > policy.MAX_RECOVERY_ATTEMPTS or retries > policy.MAX_RETRIES_PER_TOOL:
                        result_out = {
                            "reply": f"I need help after repeated failures while executing `{tool_name}`.",
                            "state": state,
                            "outcome_kind": ResponseOutcomeKind.ESCALATE,
                            "last_tool_result": result,
                        }
                        return result_out
                    await emit_loop_update(
                        chan, phase="execution.step", phase_status="active",
                        label=f"Loop step {step_index + 1}",
                        detail=f"Repairing {tool_name or 'tool'} before retry.",
                        step_index=step_index, kind="repair",
                        note_text=recovery.reason, tool_name=tool_name,
                        loop_status="repairing",
                    )
                    await emit_agent_event(chan, event_type="recovery_attempted", data={"tool": tool_name, "kind": "retry"})
                    action.args = dict(recovery.action.args)
                    action.rationale = recovery.action.rationale
                    action.status = AgendaActionStatus.PENDING
                    agenda.status = AgendaStatus.ACTIVE
                    task.task_status = "active"
                    continue

                if recovery.kind == RecoveryDecisionKind.REPLACE_REMAINING_AGENDA:
                    replace_pending_tail(agenda, recovery.replacement_actions)
                    state.last_replan_reason = recovery.reason
                    state.runtime_missing_fields = []
                    state.runtime_invalid_fields = {}
                    state.last_prompt_reason = None
                    await emit_loop_update(
                        chan, phase="execution.step", phase_status="active",
                        label=f"Loop step {step_index + 1}",
                        detail="Replanned remaining agenda.",
                        step_index=step_index, kind="repair",
                        note_text=recovery.reason, tool_name=tool_name,
                        loop_status="replanned",
                    )
                    await emit_agent_event(chan, event_type="agenda_replanned", data={"reason": recovery.reason[:120]})
                    context_bundle = await _refresh_loop_context(task, state, context, context_mode, agenda)
                    task.task_status = "active"
                    continue

                if recovery.kind == RecoveryDecisionKind.ASK_USER:
                    task.task_status = "waiting"
                    state.pending_action = "ask_user"
                    result_out = {
                        "reply": recovery.ask_user_prompt or build_missing_prompt(task, state),
                        "state": state,
                        "outcome_kind": ResponseOutcomeKind.WAITING,
                        "last_tool_result": result,
                    }
                    return result_out

                if recovery.kind == RecoveryDecisionKind.ESCALATE or retries > policy.MAX_RETRIES_PER_TOOL:
                    task.task_status = "failed"
                    result_out = {
                        "reply": recovery.reason,
                        "state": state,
                        "outcome_kind": ResponseOutcomeKind.ESCALATE,
                        "last_tool_result": result,
                    }
                    return result_out

                if recovery.kind == RecoveryDecisionKind.FAIL:
                    task.task_status = "failed"
                    result_out = {
                        "reply": recovery.reason,
                        "state": state,
                        "outcome_kind": ResponseOutcomeKind.FAILED,
                        "last_tool_result": result,
                    }
                    await emit_agent_event(chan, event_type="task_failed", data={"reason": recovery.reason[:120]})
                    return result_out
                continue

            # ---------------------------------------------------------------
            # respond
            # ---------------------------------------------------------------
            if action.kind == "respond":
                mark_action_status(agenda, action.action_id, AgendaActionStatus.COMPLETED)
                task.task_status = "completed"
                result_out = {
                    "reply": action.args.get("text") or "Done.",
                    "state": state,
                    "outcome_kind": ResponseOutcomeKind.COMPLETE,
                    "last_tool_result": last_tool_result,
                }
                return result_out

            # ---------------------------------------------------------------
            # finish
            # ---------------------------------------------------------------
            if action.kind == "finish":
                mark_action_status(agenda, action.action_id, AgendaActionStatus.COMPLETED)
                task.task_status = "completed"
                result_out = {
                    "reply": _completion_reply(task, state, last_tool_result),
                    "state": state,
                    "outcome_kind": ResponseOutcomeKind.COMPLETE,
                    "last_tool_result": last_tool_result,
                }
                return result_out

            # Unknown action kind
            mark_action_status(agenda, action.action_id, AgendaActionStatus.FAILED)
            task.task_status = "failed"
            result_out = {
                "reply": action.args.get("text") or "I could not determine a safe next action.",
                "state": state,
                "outcome_kind": ResponseOutcomeKind.FAILED,
                "last_tool_result": last_tool_result,
            }
            return result_out

        # Step budget exhausted
        result_out = {
            "reply": "I stopped after the current step budget.",
            "state": state,
            "outcome_kind": ResponseOutcomeKind.ESCALATE,
            "last_tool_result": last_tool_result,
        }
        task.task_status = "failed"
        return result_out
    finally:
        await _save_loop_state(task, state, context, agenda)
        reply_text = (result_out or {}).get("reply", "Loop ended.")
        await chan.send_phase(phase="execution", status="done", label="Loop finished", detail=reply_text[:120])


def _try_parse_answer_into_args(task: Task, answer: str) -> None:
    """Best-effort parse user answer into task.parsed_args.

    Handles simple patterns like:
    - "5" (single number -> store as first missing field)
    - "5 and 3" or "5, 3" (two numbers)
    - "key=value" pairs
    """
    import re

    answer = answer.strip()

    # Try key=value pairs
    kv_matches = re.findall(r"(\w+)\s*=\s*([^\s,]+)", answer)
    if kv_matches:
        for key, val in kv_matches:
            task.parsed_args[key] = val
        return

    # Try to extract numbers for missing numeric fields
    numbers = re.findall(r"-?\d+(?:\.\d+)?", answer)
    missing = list(task.missing_fields or [])
    if numbers and missing:
        for i, num_str in enumerate(numbers):
            if i < len(missing):
                try:
                    task.parsed_args[missing[i]] = float(num_str)
                except ValueError:
                    task.parsed_args[missing[i]] = num_str
        return

    # Single word/phrase -> assign to first missing field
    if missing and answer:
        task.parsed_args[missing[0]] = answer
