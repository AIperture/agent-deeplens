"""Response framing and composition.

Same two-stage pattern as v6: deterministic base reply + optional LLM polish.
"""
from __future__ import annotations

import json
from typing import Any

from . import policy
from .types import (
    ActionAgenda,
    AgentState,
    ResponseFrame,
    ResponseOutcomeKind,
    ToolResult,
)


def build_response_frame(
    *,
    reply: str,
    outcome_kind: ResponseOutcomeKind,
    state: AgentState,
    agenda: ActionAgenda | None,
    last_tool_result: ToolResult | None = None,
    approval_prompt: str | None = None,
) -> ResponseFrame:
    completed_actions: list[str] = []
    pending_actions: list[str] = []
    agenda_status = ""
    if agenda is not None:
        agenda_status = agenda.status.value
        for action in agenda.actions:
            label = action.name or action.kind
            if action.status.value == "completed":
                completed_actions.append(label)
            elif action.status.value in {"pending", "blocked"}:
                pending_actions.append(label)

    return ResponseFrame(
        outcome_kind=outcome_kind,
        reply=reply,
        agenda_status=agenda_status,
        completed_actions=completed_actions,
        pending_actions=pending_actions,
        missing_fields=(
            list(state.runtime_missing_fields or [])
            or (list((state.active_intent or {}).get("missing_fields", [])) if isinstance(state.active_intent, dict) else [])
        ),
        approval_prompt=approval_prompt,
        last_tool_summary=last_tool_result.summary if last_tool_result is not None else None,
        next_action_hints=list(state.next_action_hints),
    )


def _deterministic_reply(frame: ResponseFrame) -> str:
    if frame.outcome_kind == ResponseOutcomeKind.WAITING:
        return frame.reply
    if frame.outcome_kind == ResponseOutcomeKind.FAILED:
        return frame.reply
    if frame.outcome_kind == ResponseOutcomeKind.ESCALATE:
        return f"I need to re-evaluate before continuing. {frame.reply}"
    if frame.next_action_hints:
        return f"{frame.reply}\n\n{frame.next_action_hints[0]}"
    return frame.reply


async def compose_reply(*, frame: ResponseFrame, context: Any) -> str:
    """Compose final reply: deterministic base + optional LLM polish."""
    base = _deterministic_reply(frame)
    if frame.outcome_kind == ResponseOutcomeKind.WAITING:
        return base
    if len(base) <= 220 and frame.outcome_kind != ResponseOutcomeKind.COMPLETE:
        return base

    # LLM polish for longer/complete replies
    try:
        llm = context.llm("fast")
        skills = context.skills()
        system_prompt = skills.compile_prompt(
            policy.SKILL_ID,
            "skeleton.system",
            separator="\n\n",
            fallback_keys=["skeleton.system"],
        )
        payload = {
            "base_reply": base,
            "outcome_kind": frame.outcome_kind.value,
            "agenda_status": frame.agenda_status,
            "completed_actions": frame.completed_actions[:4],
            "pending_actions": frame.pending_actions[:4],
            "last_tool_summary": frame.last_tool_summary,
            "next_action_hints": frame.next_action_hints[:2],
        }
        schema = {
            "type": "object",
            "properties": {"reply": {"type": "string"}},
            "required": ["reply"],
            "additionalProperties": False,
        }
        response, _usage = await llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n\n"
                        "Rewrite the reply for clarity and brevity. "
                        "Do not add facts, new actions, or requirements."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=schema,
            schema_name="SkeletonReply",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=180,
        )
        obj = json.loads(response) if isinstance(response, str) else response
        reply = str(obj.get("reply") or "").strip()
        return reply or base
    except Exception:
        if hasattr(context, "logger"):
            context.logger().warning("skeleton: response compose llm failed", exc_info=True)
        return base
