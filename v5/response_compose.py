from __future__ import annotations

import json
from typing import Any

from .types import DEEPLENS_SKILL_ID
from .types import ConversationState, LoopOutcome, LoopOutcomeKind


def _deterministic_reply(*, outcome: LoopOutcome, state: ConversationState) -> str:
    if outcome.kind == LoopOutcomeKind.COMPLETE:
        if state.next_action_hints:
            return f"{outcome.reason}\n\n{state.next_action_hints[0]}"
        return outcome.reason
    if outcome.kind == LoopOutcomeKind.WAITING:
        if outcome.pending_interaction is not None:
            return outcome.pending_interaction.prompt
        return outcome.reason
    if outcome.kind == LoopOutcomeKind.ESCALATE:
        return f"I need to re-evaluate the conversation before continuing. {outcome.reason}"
    return outcome.reason


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"reply": {"type": "string"}},
        "required": ["reply"],
        "additionalProperties": False,
    }


async def compose_reply(
    *,
    outcome: LoopOutcome,
    state: ConversationState,
    context: Any,
) -> str:
    base = _deterministic_reply(outcome=outcome, state=state)
    if outcome.kind == LoopOutcomeKind.WAITING:
        return base
    if len(base) <= 220 and outcome.kind != LoopOutcomeKind.COMPLETE:
        return base

    llm = context.llm("fast")
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        DEEPLENS_SKILL_ID,
        "deeplens.system",
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )
    payload = {
        "outcome_kind": outcome.kind.value,
        "base_reply": base,
        "task": {
            "task_id": outcome.task_snapshot.task_id,
            "domain_hint": outcome.task_snapshot.domain_hint.value,
            "task_shape": outcome.task_snapshot.task_shape.value,
            "assumptions": outcome.task_snapshot.assumptions[:3],
        },
        "pending_prompt": outcome.pending_interaction.prompt if outcome.pending_interaction else None,
        "last_tool_summary": outcome.last_tool_result.summary if outcome.last_tool_result else None,
        "last_result_summary": state.last_result_summary,
    }
    try:
        response, _usage = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=_schema(),
            schema_name="DeepLensV5Reply",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=180,
            reasoning_effort="low",
        )
        obj = json.loads(response) if isinstance(response, str) else response
        reply = str(obj.get("reply") or "").strip()
        return reply or base
    except Exception:
        context.logger().warning("deeplens_v5: response compose llm failed", exc_info=True)
        return base
