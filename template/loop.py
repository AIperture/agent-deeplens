from __future__ import annotations

from typing import Any

from . import policy
from .agenda import mark_action_status, next_pending_action, replace_pending_tail
from .binder import bind_capability_action
from .context_builder import build_context_bundle
from .events import emit_agent_event, emit_loop_update
from .planner import build_plan_agenda
from .recovery import recover_failed_action
from .tools.fake_tools import EXECUTOR_MAP
from .tools.registry import get_tool_spec
from .types import (
    AgendaAction,
    AgendaActionStatus,
    AgendaStatus,
    FailureKind,
    PlanAgenda,
    RecoveryDecisionKind,
    ResponseOutcomeKind,
    TemplateState,
    TemplateTask,
    ToolResult,
    save_state,
)


def _append_trace(state: TemplateState, item: dict[str, Any]) -> None:
    state.loop_trace.append(item)
    state.loop_trace = state.loop_trace[-policy.LOOP_TRACE_LIMIT:]


def _sync(state: TemplateState, task: TemplateTask, agenda: PlanAgenda | None) -> None:
    state.active_task = task.to_dict()
    state.active_agenda = agenda.to_dict() if agenda is not None else None


async def _save(context: Any, state: TemplateState, task: TemplateTask, agenda: PlanAgenda | None) -> None:
    _sync(state, task, agenda)
    await save_state(context, state=state, state_key=policy.STATE_KEY, agent_id=policy.AGENT_ID)


def _apply_state_updates(state: TemplateState, result: ToolResult) -> None:
    updates = result.data.get("state_updates") if isinstance(result.data, dict) else None
    if isinstance(updates, dict):
        for key, value in updates.items():
            if "." in key:
                root, child = key.split(".", 1)
                container = getattr(state, root, None)
                if isinstance(container, dict):
                    container[child] = value
            elif hasattr(state, key):
                setattr(state, key, value)


async def _execute_bound_action(*, action: AgendaAction, task: TemplateTask, state: TemplateState, context_bundle: Any, context: Any) -> ToolResult:
    intent = action.capability
    if intent is None:
        return ToolResult(ok=False, tool_name=None, summary="Missing capability intent.", failure_kind=FailureKind.INTERNAL_ERROR)
    binding = await bind_capability_action(
        action_id=action.action_id,
        capability_name=intent.capability_name,
        planner_args=dict(intent.planner_args),
        task=task,
        state=state,
        context_bundle=context_bundle,
        context=context,
    )
    state.last_binding = binding.executable_action.to_dict()
    state.runtime_missing_fields = list(binding.executable_action.missing_fields)
    state.runtime_invalid_fields = dict(binding.executable_action.invalid_fields)
    task.missing_fields = list(binding.executable_action.missing_fields)

    if binding.suggested_action == "ask_user":
        return ToolResult(
            ok=False,
            tool_name=binding.executable_action.tool_name,
            summary=binding.prompt or "Need user input.",
            failure_kind=FailureKind.BINDING_FAILURE,
            needs_user_input=True,
            missing_fields=list(binding.executable_action.missing_fields),
            invalid_fields=dict(binding.executable_action.invalid_fields),
        )
    if binding.suggested_action == "request_approval":
        return ToolResult(
            ok=False,
            tool_name=binding.executable_action.tool_name,
            summary=binding.prompt or "Approval required.",
            failure_kind=FailureKind.POLICY_FAILURE,
        )

    spec = get_tool_spec(binding.executable_action.tool_name or intent.capability_name)
    if spec.approval_level.value != "none" and not state.approval_tokens.get(action.action_id):
        return ToolResult(
            ok=False,
            tool_name=spec.name,
            summary=f"`{spec.name}` requires explicit approval before execution.",
            failure_kind=FailureKind.POLICY_FAILURE,
        )
    executor = EXECUTOR_MAP[spec.executor_key]
    retry_key = f"{spec.name}:{binding.executable_action.resolved_inputs.get('change_summary', '')}"
    result = await executor(
        tool_name=spec.name,
        resolved_inputs=binding.executable_action.resolved_inputs,
        task=task,
        state=state,
        context_bundle=context_bundle,
        context=context,
    )
    if spec.may_fail_transiently:
        state.retry_counters[retry_key] = state.retry_counters.get(retry_key, 0) + (0 if result.ok else 1)
    if result.ok:
        state.prior_tool_outputs[spec.name] = dict(result.data)
        _apply_state_updates(state, result)
        state.runtime_missing_fields = []
        state.runtime_invalid_fields = {}
    else:
        state.failure_history.append({
            "tool_name": spec.name,
            "summary": result.summary,
            "failure_kind": result.failure_kind.value if result.failure_kind else None,
        })
        state.failure_history = state.failure_history[-policy.FAILURE_HISTORY_LIMIT:]
    state.last_tool_result = {"tool_name": spec.name, "ok": result.ok, "summary": result.summary, "status": result.status}
    return result


async def run_loop(
    *,
    task: TemplateTask,
    state: TemplateState,
    context_bundle: Any,
    context_mode: Any,
    context: Any,
) -> dict[str, Any]:
    del context_bundle
    chan = context.channel("ui:session")
    await emit_agent_event(chan, event_type="task_started", data={"task_shape": task.task_shape.value})
    agenda = PlanAgenda.from_dict(state.active_agenda)
    if agenda is None:
        fresh_bundle = await build_context_bundle(context_mode=context_mode, task=task, state=state, context=context)
        agenda = await build_plan_agenda(task=task, state=state, context_bundle=fresh_bundle, context=context)
        await emit_agent_event(chan, event_type="agenda_built", data={"actions": len(agenda.actions)})

    last_tool_result: ToolResult | None = None
    max_steps = policy.MAX_STEPS[task.task_shape]

    try:
        for step_index in range(max_steps):
            await _save(context, state, task, agenda)
            current_bundle = await build_context_bundle(context_mode=context_mode, task=task, state=state, context=context)
            action = next_pending_action(agenda)
            print(f"🍎 Loop step {step_index + 1}/{max_steps}, pending action: {action.kind if action else 'None'}, agenda status: {agenda.status.value}")
            if action is None:
                agenda.status = AgendaStatus.COMPLETED
                task.task_status = "completed"
                await emit_agent_event(chan, event_type="task_completed", data={"reply": (last_tool_result.summary if last_tool_result else "complete")[:80]})
                return {
                    "reply": last_tool_result.summary if last_tool_result is not None else "Completed the requested workflow.",
                    "state": state,
                    "outcome_kind": ResponseOutcomeKind.COMPLETE,
                    "last_tool_result": last_tool_result,
                }

            await emit_loop_update(chan, phase="execution.step", phase_status="active", label=f"Loop step {step_index + 1}", detail=action.kind)

            if action.kind == "ask_user":
                action.status = AgendaActionStatus.BLOCKED
                agenda.status = AgendaStatus.WAITING
                task.task_status = "waiting"
                state.pending_action = "ask_user"
                state.waiting_prompt = str(action.args.get("prompt") or "I need more information.")
                return {"reply": state.waiting_prompt, "state": state, "outcome_kind": ResponseOutcomeKind.WAITING}

            if action.kind == "request_approval":
                action.status = AgendaActionStatus.BLOCKED
                agenda.status = AgendaStatus.WAITING
                task.task_status = "waiting"
                state.pending_action = "request_approval"
                prompt = str(action.args.get("approval_prompt") or "Approve this action?")
                state.pending_approval = {"action_id": action.action_id, "prompt": prompt}
                return {"reply": prompt, "state": state, "outcome_kind": ResponseOutcomeKind.WAITING, "approval_prompt": prompt}

            if action.kind == "bind_and_execute":
                print(f"🍎 Binding and executing action: {action.action_id} with capability: {action.capability.capability_name if action.capability else None}")
                await emit_agent_event(chan, event_type="capability_binding_started", data={"capability": action.capability.capability_name if action.capability else None})
                result = await _execute_bound_action(action=action, task=task, state=state, context_bundle=current_bundle, context=context)
                last_tool_result = result
                _append_trace(state, {
                    "action_id": action.action_id,
                    "kind": action.kind,
                    "tool_name": result.tool_name,
                    "ok": result.ok,
                    "summary": result.summary,
                })
                if result.ok:
                    mark_action_status(agenda, action.action_id, AgendaActionStatus.COMPLETED)
                    await emit_agent_event(chan, event_type="tool_completed", data={"tool": result.tool_name})
                    if result.should_end_turn:
                        task.task_status = "completed"
                        return {"reply": result.summary, "state": state, "outcome_kind": ResponseOutcomeKind.COMPLETE, "last_tool_result": result}
                    continue

                mark_action_status(agenda, action.action_id, AgendaActionStatus.FAILED)
                await emit_agent_event(chan, event_type="tool_failed", data={"tool": result.tool_name, "summary": result.summary[:80]})
                recovery = await recover_failed_action(
                    task=task,
                    state=state,
                    agenda=agenda,
                    failed_action=action,
                    result=result,
                    context_bundle=current_bundle,
                    context=context,
                )
                await emit_agent_event(chan, event_type="recovery_decided", data={"kind": recovery.kind.value, "reason": recovery.reason[:80]})
                if recovery.task is not None:
                    task = recovery.task
                if recovery.kind == RecoveryDecisionKind.ASK_USER:
                    follow_up = AgendaAction(action_id=f"{action.action_id}:ask", kind="ask_user", args={"prompt": recovery.ask_user_prompt or result.summary}, rationale=recovery.reason)
                    replace_pending_tail(agenda, [follow_up])
                    task.task_status = "waiting"
                    state.pending_action = "ask_user"
                    state.waiting_prompt = recovery.ask_user_prompt or result.summary
                    return {"reply": state.waiting_prompt, "state": state, "outcome_kind": ResponseOutcomeKind.WAITING, "last_tool_result": result}
                if recovery.kind == RecoveryDecisionKind.REQUEST_APPROVAL:
                    follow_up = AgendaAction(
                        action_id=f"{action.action_id}:approve",
                        kind="request_approval",
                        name=result.tool_name,
                        args={"approval_prompt": recovery.approval_prompt or result.summary},
                        rationale=recovery.reason,
                    )
                    replace_pending_tail(agenda, [follow_up, action, AgendaAction(action_id=f"{action.action_id}:finish", kind="finish", rationale="Resume after approval")])
                    task.task_status = "waiting"
                    state.pending_action = "request_approval"
                    state.pending_approval = {"action_id": action.action_id, "prompt": recovery.approval_prompt or result.summary}
                    return {
                        "reply": recovery.approval_prompt or result.summary,
                        "state": state,
                        "outcome_kind": ResponseOutcomeKind.WAITING,
                        "approval_prompt": recovery.approval_prompt or result.summary,
                    }
                if recovery.kind == RecoveryDecisionKind.RETRY_ACTION and recovery.action is not None:
                    action.capability = recovery.action.capability
                    action.status = AgendaActionStatus.PENDING
                    agenda.status = AgendaStatus.ACTIVE
                    continue
                if recovery.kind == RecoveryDecisionKind.REPLACE_REMAINING_AGENDA:
                    replace_pending_tail(agenda, recovery.replacement_actions)
                    await emit_agent_event(chan, event_type="agenda_replanned", data={"reason": recovery.reason[:80]})
                    continue
                if recovery.kind == RecoveryDecisionKind.ESCALATE:
                    task.task_status = "failed"
                    return {"reply": recovery.reason, "state": state, "outcome_kind": ResponseOutcomeKind.ESCALATE, "last_tool_result": result}
                task.task_status = "failed"
                return {"reply": recovery.reason, "state": state, "outcome_kind": ResponseOutcomeKind.FAILED, "last_tool_result": result}

            if action.kind == "respond":
                mark_action_status(agenda, action.action_id, AgendaActionStatus.COMPLETED)
                task.task_status = "completed"
                return {"reply": str(action.args.get("text") or "Done."), "state": state, "outcome_kind": ResponseOutcomeKind.COMPLETE}

            if action.kind == "finish":
                mark_action_status(agenda, action.action_id, AgendaActionStatus.COMPLETED)
                task.task_status = "completed"
                return {
                    "reply": last_tool_result.summary if last_tool_result is not None else "Completed the requested workflow.",
                    "state": state,
                    "outcome_kind": ResponseOutcomeKind.COMPLETE,
                    "last_tool_result": last_tool_result,
                }

            mark_action_status(agenda, action.action_id, AgendaActionStatus.FAILED)
            task.task_status = "failed"
            return {"reply": str(action.args.get("text") or "I could not determine a safe next action."), "state": state, "outcome_kind": ResponseOutcomeKind.FAILED}

        task.task_status = "failed"
        return {"reply": "I stopped after the current step budget.", "state": state, "outcome_kind": ResponseOutcomeKind.ESCALATE, "last_tool_result": last_tool_result}
    finally:
        await _save(context, state, task, agenda)
