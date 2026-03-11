from __future__ import annotations

import json
from typing import Any

from .types import (
    DEEPLENS_SKILL_ID,
    ConversationState,
    DomainHint,
    LoopAction,
    LoopActionKind,
    PendingInteraction,
    PendingInteractionKind,
    ResponseShape,
    TaskFrame,
)


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
        return "Please upload a lens file or tell me which active lens/design to use."
    if "run_id" in missing:
        return "I need the run id before I can check status or cancel the run."
    if "export_format" in missing:
        return "Which export format do you want: `json`, `zmx`, or both?"
    return "Please provide the missing information so I can continue."


def build_pending_interaction(task: TaskFrame) -> PendingInteraction:
    response_shape = ResponseShape.FREE_TEXT
    if task.missing_fields == ["run_id"]:
        response_shape = ResponseShape.SCALAR
    if task.missing_fields == ["lens_source"]:
        response_shape = ResponseShape.FILE_OR_REF
    return PendingInteraction(
        kind=PendingInteractionKind.MISSING_INFO,
        prompt=_default_missing_prompt(task),
        related_task_id=task.task_id,
        expected_fields=list(task.missing_fields),
        response_shape=response_shape,
    )


def select_next_action(
    *,
    task: TaskFrame,
    state: ConversationState,
    context: Any,
) -> LoopAction:
    del context

    pending = state.get_pending_interaction()
    if pending is not None and pending.related_task_id == task.task_id:
        return LoopAction(
            kind=LoopActionKind.ASK_USER,
            args={
                "prompt": pending.prompt,
                "expected_fields": pending.expected_fields,
                "response_shape": pending.response_shape.value,
            },
            reason="Pending interaction is still open for the active task.",
            priority=100,
        )

    if task.missing_fields:
        pending = build_pending_interaction(task)
        print(f"🍎 Built pending interaction: {pending}")
        return LoopAction(
            kind=LoopActionKind.ASK_USER,
            args={
                "prompt": pending.prompt,
                "expected_fields": pending.expected_fields,
                "response_shape": pending.response_shape.value,
            },
            reason="Task still has missing required fields.",
            priority=95,
        )

    if task.preferred_tool == "ag.spawn_graph":
        already_requested = any(
            item.get("event") == "loop.approval.approved" and item.get("payload", {}).get("task_id") == task.task_id
            for item in state.loop_trace[-8:]
        )
        if not already_requested:
            return LoopAction(
                kind=LoopActionKind.REQUEST_APPROVAL,
                args={"prompt": "I’m ready to submit the optimization run. Approve?"},
                reason="Optimization run requires explicit approval.",
                priority=90,
            )

    if task.preferred_tool:
        return LoopAction(
            kind=LoopActionKind.TOOL_CALL,
            tool_name=task.preferred_tool,
            args={},
            reason="Preferred tool is ready to execute.",
            priority=80,
        )

    if state.requested_next_step:
        requested = state.requested_next_step
        state.requested_next_step = None
        text = {
            "interpret_or_export": "Analysis complete. You can review the results above, or ask me to export or optimize.",
            "analyze_or_optimize": "Design created. You can ask me to analyze it, optimize it, or export it.",
            "share_or_optimize": "Export complete. You can optimize the lens or start a new design.",
            "status_or_cancel": "Background run submitted. Use status to check progress or cancel to stop it.",
        }.get(requested)
        if text:
            return LoopAction(
                kind=LoopActionKind.RESPOND,
                args={"text": text},
                reason="Requested next-step guidance is available.",
                priority=30,
            )

    if state.next_action_hints:
        hint = state.next_action_hints[0]
        return LoopAction(
            kind=LoopActionKind.RESPOND,
            args={"text": hint},
            reason="Compact follow-up guidance is available.",
            priority=20,
        )

    return LoopAction(
        kind=LoopActionKind.RESPOND,
        args={"text": "I interpreted the request, but I do not yet have a safe executable action."},
        reason="No safe tool or completion path was found.",
        priority=10,
    )


# ---- LLM-based fallback proposer (ported from v3) ----

_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["ask_user", "request_approval", "tool_call", "respond", "finish", "fail"],
        },
        "name": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
        "prompt": {"type": ["string", "null"]},
        "text": {"type": ["string", "null"]},
        "approval_prompt": {"type": ["string", "null"]},
    },
    "required": ["kind", "name", "rationale", "prompt", "text", "approval_prompt"],
    "additionalProperties": False,
}


async def propose_next_action_with_llm(
    *,
    task: TaskFrame,
    state: ConversationState,
    context_bundle: Any,
    step_index: int,
    context: Any,
) -> LoopAction:
    """LLM-based action proposer, used when deterministic selection has no answer."""
    llm = context.llm()
    skills = context.skills()
    domain_section = {
        "chat": "deeplens.domain_chat",
        "analysis": "deeplens.domain_analysis",
        "design": "deeplens.domain_design",
        "optimization": "deeplens.domain_optimization",
        "debug": "deeplens.domain_debug",
        "unknown": "deeplens.refusal",
    }.get(task.domain_hint.value, "deeplens.refusal")
    system_prompt = skills.compile_prompt(
        DEEPLENS_SKILL_ID,
        "deeplens.system",
        "deeplens.loop",
        domain_section,
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )

    user_payload = {
        "task_shape": task.task_shape.value,
        "domain_hint": task.domain_hint.value,
        "preferred_tool": task.preferred_tool,
        "user_goal": task.user_goal,
        "working_state": context_bundle.working_state if context_bundle else {},
        "recent_messages": (context_bundle.recent_messages if context_bundle else [])[-6:],
        "long_term_summary": context_bundle.long_term_summary if context_bundle else None,
        "prompt_segments": context_bundle.prompt_segments if context_bundle else {},
        "current_loop_trace_tail": state.loop_trace[-8:],
        "previous_loop_summaries": state.loop_history[-3:],
        "pending_runs": state.pending_runs[-5:],
        "notes": [
            "Choose exactly one next action.",
            "For direct_answer, prefer respond or finish.",
            "For single_action, prefer one workflow-level tool_call if the tool is obvious.",
            "For multi_step, ask for missing info or approval before expensive work.",
            "For run_control, prefer status or cancel related tool calls only.",
            "If unsupported, prefer fail with a concise refusal.",
            "Keep rationale under 40 words.",
            "Keep prompt, approval_prompt, and text concise.",
        ],
    }

    try:
        resp, _usage = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=_ACTION_SCHEMA,
            schema_name="DeepLensLoopAction",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=320,
            reasoning_effort="low",
        )
    except Exception:
        context.logger().warning("deeplens_v5: propose_next_action_with_llm failed", exc_info=True)
        return LoopAction(
            kind=LoopActionKind.RESPOND,
            args={"text": "I could not determine a safe next action."},
            reason="LLM proposer failed.",
            priority=5,
        )

    obj = json.loads(resp) if isinstance(resp, str) else resp
    kind_str = obj.get("kind", "fail")
    args: dict[str, Any] = {}
    for key in ("prompt", "text", "approval_prompt"):
        value = obj.get(key)
        if value is not None:
            args[key] = value

    return LoopAction(
        kind=LoopActionKind(kind_str),
        tool_name=obj.get("name"),
        args=args,
        reason=obj.get("rationale", "LLM-proposed action."),
        priority=50,
    )
