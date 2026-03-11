from __future__ import annotations

from typing import Any

from .types import (
    ConversationState,
    DomainHint,
    LoopAction,
    LoopActionKind,
    PendingInteraction,
    PendingInteractionKind,
    ResponseShape,
    TaskFrame,
)


# ---------------------------------------------------------------------
# What to implement here
# ---------------------------------------------------------------------
# - One-step action selection only
# - Priority order:
#     1) ask for missing info
#     2) approval gate
#     3) tool call
#     4) respond / finish
#
# What should NOT be included here
# - Tool execution
# - Tool repair
# - Broad conversational reinterpretation
# - Multi-controller branching
# ---------------------------------------------------------------------


def _default_missing_prompt(task: TaskFrame) -> str:
    missing = list(task.missing_fields or [])

    if task.domain_hint == DomainHint.DESIGN:
        prompts: list[str] = []
        if "fov" in missing:
            prompts.append("field of view (`fov`)")
        if "fnum" in missing:
            prompts.append("F-number (`fnum`)")
        if "foclen_or_imgh" in missing:
            prompts.append("either focal length (`foclen`) or image height / sensor format (`imgh`)")
        joined = ", ".join(prompts) if prompts else "the remaining design inputs"
        return f"I need {joined} before I can create the lens."

    if "lens_source" in missing:
        return "Please upload a lens file or point me to the active lens/design to use."

    if "run_id" in missing:
        return "I need the run id before I can check status or cancel the run."

    return "Please provide the missing information so I can continue."


def build_pending_interaction(task: TaskFrame) -> PendingInteraction:
    return PendingInteraction(
        kind=PendingInteractionKind.MISSING_INFO,
        prompt=_default_missing_prompt(task),
        related_task_id=task.task_id,
        expected_fields=list(task.missing_fields),
        response_shape=ResponseShape.FREE_TEXT,
    )


def select_next_action(
    *,
    task: TaskFrame,
    state: ConversationState,
    context: Any,
) -> LoopAction:
    del state
    del context

    # 1) Missing information gate
    if task.missing_fields:
        pending = build_pending_interaction(task)
        return LoopAction(
            kind=LoopActionKind.ASK_USER,
            args={
                "prompt": pending.prompt,
                "expected_fields": pending.expected_fields,
                "response_shape": pending.response_shape.value,
            },
            reason="Task still has missing required fields.",
            priority=100,
        )

    # 2) Approval gate for optimization / spawn flows
    if task.preferred_tool == "ag.spawn_graph":
        return LoopAction(
            kind=LoopActionKind.REQUEST_APPROVAL,
            args={
                "prompt": "I’m ready to submit the optimization run. Approve?",
            },
            reason="Optimization run requires explicit approval.",
            priority=90,
        )

    # 3) Normal tool call
    if task.preferred_tool:
        return LoopAction(
            kind=LoopActionKind.TOOL_CALL,
            tool_name=task.preferred_tool,
            args={},
            reason="Preferred tool is available and required inputs appear present.",
            priority=80,
        )

    # 4) Direct response fallback
    return LoopAction(
        kind=LoopActionKind.RESPOND,
        args={"text": "I interpreted the request, but I do not yet have a safe executable action."},
        reason="No safe tool or task completion path found.",
        priority=10,
    )