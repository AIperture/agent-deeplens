from __future__ import annotations

import json
from typing import Any

from .binding import bind_step
from .interaction import apply_user_inputs, ask_for_approval, ask_for_missing_inputs, confirm_plan
from .policies import get_plan_policy, get_tool_policy, should_confirm_plan
from .tool_executor import execute_tool
from .types import DeepLensTask, ErrorType, Plan, PlanStatus, RuntimeState, StepStatus


async def _emit_tool_phase(*, step: Any, status: str, context: Any) -> None:
    phase_status = {"running": "active", "succeeded": "done", "failed": "failed"}[status]
    detail = {
        "running": f"Running `{step.tool_name}`.",
        "succeeded": f"Completed `{step.tool_name}`.",
        "failed": f"`{step.tool_name}` failed.",
    }[status]
    await context.channel("ui:session").send_phase(
        phase=f"tool.{step.tool_name}",
        status=phase_status,
        label=step.title,
        detail=detail,
        key_suffix=step.step_id,
    )


def _archive(state: RuntimeState, task: DeepLensTask, plan: Plan, tool_summaries: list[str]) -> None:
    state.loop_history.append(
        {
            "task": task.to_dict(),
            "plan": plan.to_dict(),
            "summaries": tool_summaries[-4:],
        }
    )
    state.loop_history = state.loop_history[-6:]


def _plan_summary(task: DeepLensTask, plan: Plan, state: RuntimeState) -> str:
    lines = [f"Plan for: {task.user_goal or 'DeepLens request'}"]
    for index, step in enumerate(plan.steps, start=1):
        binding = bind_step(step, task, state)
        args_json = json.dumps(binding.resolved_args, indent=2, ensure_ascii=False, sort_keys=True)
        lines.append(f"{index}. {step.title}")
        lines.append(f"Tool: {step.tool_name}")
        lines.append(f"Goal: {step.goal}")
        # lines.append("Args:")
        # lines.append(args_json)
        if binding.missing_fields:
            lines.append(f"Missing before execution: {', '.join(binding.missing_fields)}")
    return "\n".join(lines)


async def _confirm_plan_if_needed(*, task: DeepLensTask, plan: Plan, state: RuntimeState, context: Any) -> bool:
    if not should_confirm_plan(plan):
        return True
    summary = _plan_summary(task, plan, state)
    policy = get_plan_policy(plan)
    await context.channel("ui:session").send_phase(
        phase="plan.confirmation",
        status="active",
        label="Plan confirmation",
        detail="Waiting for plan confirmation before execution.",
    )
    confirmed = await confirm_plan(summary=summary, prompt=policy.confirmation_prompt, context=context)
    if confirmed:
        await context.channel("ui:session").send_phase(
            phase="plan.confirmation",
            status="done",
            label="Plan confirmed",
            detail="Plan confirmed. Starting execution.",
        )
        return True
    plan.status = PlanStatus.CANCELLED
    state.final_reply = "Okay, I stopped before executing the plan."
    await context.channel("ui:session").send_phase(
        phase="plan.confirmation",
        status="failed",
        label="Plan cancelled",
        detail="Execution cancelled before the first step.",
    )
    return False


async def run_loop(
    *,
    task: DeepLensTask,
    plan: Plan,
    state: RuntimeState,
    context: Any,
) -> dict[str, Any]:
    tool_summaries: list[str] = []
    state.active_task = task.to_dict()
    state.active_plan = plan.to_dict()

    if not plan.steps:
        plan.status = PlanStatus.COMPLETED
        _archive(state, task, plan, tool_summaries)
        return {"plan": plan, "state": state, "tool_summaries": tool_summaries}

    if not await _confirm_plan_if_needed(task=task, plan=plan, state=state, context=context):
        state.active_plan = plan.to_dict()
        _archive(state, task, plan, tool_summaries)
        return {"plan": plan, "state": state, "tool_summaries": tool_summaries}

    plan.status = PlanStatus.RUNNING
    state.active_plan = plan.to_dict()

    while plan.current_step < len(plan.steps):
        step = plan.steps[plan.current_step]
        step.status = StepStatus.RUNNING
        step.attempts += 1

        binding = bind_step(step, task, state)
        if not binding.ok:
            reply_text, files = await ask_for_missing_inputs(
                missing_fields=binding.missing_fields,
                task=task,
                context=context,
            )
            task = await apply_user_inputs(
                task=task,
                text=reply_text,
                attachments=files,
                context=context,
            )
            state.active_task = task.to_dict()
            state.active_plan = plan.to_dict()
            retry_binding = bind_step(step, task, state)
            if not retry_binding.ok:
                step.status = StepStatus.FAILED
                plan.status = PlanStatus.FAILED
                state.final_reply = retry_binding.message
                break
            binding = retry_binding

        policy = get_tool_policy(step.tool_policy_id)
        if policy.requires_approval and not step.approval_granted:
            approved = await ask_for_approval(
                prompt=policy.approval_prompt or f"Approve `{step.tool_name}`?",
                context=context,
            )
            if not approved:
                step.status = StepStatus.CANCELLED
                plan.status = PlanStatus.CANCELLED
                state.final_reply = "Okay, I stopped before making changes."
                break
            step.approval_granted = True

        await _emit_tool_phase(step=step, status="running", context=context)
        result = await execute_tool(action=binding.action, task=task, state=state, context=context)
        if result.ok:
            await _emit_tool_phase(step=step, status="succeeded", context=context)
            tool_summaries.append(result.summary)
            step.status = StepStatus.SUCCEEDED
            plan.current_step += 1
            state.active_task = task.to_dict()
            state.active_plan = plan.to_dict()
            if result.should_end_turn:
                plan.status = PlanStatus.COMPLETED
                break
            continue

        await _emit_tool_phase(step=step, status="failed", context=context)
        if result.error_type == ErrorType.MISSING_INPUT.value:
            reply_text, files = await ask_for_missing_inputs(
                missing_fields=binding.missing_fields or step.required_fields or ["lens_source"],
                task=task,
                context=context,
            )
            task = await apply_user_inputs(
                task=task,
                text=reply_text,
                attachments=files,
                context=context,
            )
            state.active_task = task.to_dict()
            state.active_plan = plan.to_dict()
            if step.attempts < 2:
                step.status = StepStatus.READY
                continue
        step.status = StepStatus.FAILED
        plan.status = PlanStatus.FAILED
        state.final_reply = result.summary or result.error_message or "The workflow failed."
        break

    if plan.status == PlanStatus.RUNNING:
        plan.status = PlanStatus.COMPLETED
    state.active_plan = plan.to_dict()
    _archive(state, task, plan, tool_summaries)
    return {"plan": plan, "state": state, "tool_summaries": tool_summaries}
