from __future__ import annotations

from typing import Any

from aethergraph.core.runtime.node_context import NodeContext

from .loop_engine import run_reliable_loop
from .memory_policy import build_context_bundle, maybe_distill_session_summary
from .policy import get_policy
from .types import ExecutionMode, RouterDecision

async def execute_task(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    session_id: str | None,
    user_meta: dict[str, Any] | None,
    decision: RouterDecision,
    state: Any,
    context: Any,
) -> dict[str, Any]:
    policy = get_policy(decision["intent"])
    task = decision["task"]

    # Persist active task into state for continuation.
    state.active_task = {
        "user_goal": task.user_goal,
        "intent": task.intent,
        "execution_preference": task.execution_preference,
        "attachments": task.attachments,
        "parsed_args": task.parsed_args,
        "missing_fields": task.missing_fields,
        "constraints": task.constraints,
        "preferred_outputs": task.preferred_outputs,
        "active_artifact_refs": task.active_artifact_refs,
        "notes": task.notes,
    }
    state.last_execution_mode = decision["execution_mode"]

    await maybe_distill_session_summary(context)

    context_bundle = await build_context_bundle(
        context_mode=decision["context_mode"],
        task=task,
        state=state,
        context=context,
    )

    mode = decision["execution_mode"]

    if mode == ExecutionMode.WORKFLOW:
        return await execute_workflow(
            task=task,
            policy=policy,
            state=state,
            context_bundle=context_bundle,
            context=context,
        )

    if mode == ExecutionMode.LOOP:
        return await run_reliable_loop(
            task=task,
            policy=policy,
            state=state,
            context_bundle=context_bundle,
            context=context,
        )

    # AUTO
    if policy.default_workflow_enabled and not state.agent_mode_enabled:
        return await execute_workflow(
            task=task,
            policy=policy,
            state=state,
            context_bundle=context_bundle,
            context=context,
        )

    if policy.default_workflow_enabled and not task.missing_fields:
        return await execute_workflow(
            task=task,
            policy=policy,
            state=state,
            context_bundle=context_bundle,
            context=context,
        )

    if policy.default_loop_enabled:
        return await run_reliable_loop(
            task=task,
            policy=policy,
            state=state,
            context_bundle=context_bundle,
            context=context,
        )

    # fallback direct response
    return {
        "reply": (
            f"I routed this to `{task.intent.value}` but no specialized executor was selected. "
            "You can now implement a direct answer path or delegate to a workflow/loop."
        ),
        "state": state,
    }


async def execute_workflow(
    *,
    task: Any,
    policy: Any,
    state: Any,
    context_bundle: Any,
    context: NodeContext
) -> dict[str, Any]:
    # Placeholder:
    # - Later dispatch to @graphify rigid flows:
    #   - simulate_workflow
    #   - optimize_workflow
    #   - viz_workflow
    # - Or to direct deterministic handlers.
    return {
        "reply": (
            f"[workflow:{task.intent.value}] Scaffold hit.\n"
            "This is where the rigid graphify/programmed flow should run."
        ),
        "state": state,
    }