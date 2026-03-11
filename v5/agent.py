from __future__ import annotations

from typing import Any

from .interpreter import interpret_turn
from .loop_engine import run_task_loop
from .response_compose import compose_reply
from .state import append_trace, set_active_task, set_pending_interaction
from .types import (
    ConversationState,
    LoopOutcomeKind,
    TurnRole,
    load_state,
    save_state,
)


# ---------------------------------------------------------------------
# What to implement here
# ---------------------------------------------------------------------
# - Top-level orchestration only
# - Load state
# - Run master interpreter
# - Optionally run local loop
# - Compose final reply
# - Save state
#
# What should NOT be included here
# - Detailed tool logic
# - Field extraction internals
# - Repair internals
# - Large controller split
# ---------------------------------------------------------------------


async def run_agent_turn(
    *,
    message: str,
    attachments: list[dict[str, Any]] | None,
    context: Any,
    user_meta: dict[str, Any] | None = None,
) -> str:
    state = await load_state(context)

    decision = await interpret_turn(
        message=message,
        attachments=attachments,
        state=state,
        context=context,
        user_meta=user_meta,
    )

    append_trace(state, "agent.interpreter_decision", turn_role=decision.turn_role.value)

    # Direct reply path
    if decision.turn_role == TurnRole.DIRECT_REPLY:
        reply = decision.direct_reply or "I’m not sure how to help with that yet."
        await save_state(context, state)
        return reply

    # Update runtime state from interpreter decision
    if decision.clear_pending_interaction:
        set_pending_interaction(state, None)

    if decision.task is not None:
        if decision.replace_active_task or state.get_active_task() is None:
            set_active_task(state, decision.task)
        else:
            set_active_task(state, decision.task)

    active_task = state.get_active_task()
    if active_task is None:
        reply = "I could not determine an active task to run."
        await save_state(context, state)
        return reply

    outcome = await run_task_loop(
        task=active_task,
        state=state,
        context=context,
    )

    # Persist latest task snapshot from loop outcome
    set_active_task(state, outcome.task_snapshot)

    # If task completed, you may decide to clear active_task here later.
    # For now keep it so follow-up explanation / export / optimization can reuse it.

    reply = compose_reply(
        outcome=outcome,
        state=state,
    )

    await save_state(context, state)
    return reply