from __future__ import annotations

import json
from typing import Any

from .types import DEEPLENS_SKILL_ID, ResponseFrame, ResponseOutcomeKind


def _deterministic_reply(frame: ResponseFrame) -> str:
    if frame.outcome_kind == ResponseOutcomeKind.WAITING:
        return frame.reply
    if frame.outcome_kind == ResponseOutcomeKind.FAILED:
        return frame.reply
    if frame.outcome_kind == ResponseOutcomeKind.ESCALATE:
        return f"I need to re-evaluate the conversation before continuing. {frame.reply}"
    if frame.next_action_hints:
        return f"{frame.reply}\n\n{frame.next_action_hints[0]}"
    return frame.reply


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"reply": {"type": "string"}},
        "required": ["reply"],
        "additionalProperties": False,
    }


async def compose_reply(*, frame: ResponseFrame, context: Any) -> str:
    base = _deterministic_reply(frame)
    if frame.outcome_kind == ResponseOutcomeKind.WAITING:
        return base
    if len(base) <= 220 and frame.outcome_kind != ResponseOutcomeKind.COMPLETE:
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
        "base_reply": base,
        "outcome_kind": frame.outcome_kind.value,
        "agenda_status": frame.agenda_status,
        "completed_actions": frame.completed_actions[:4],
        "pending_actions": frame.pending_actions[:4],
        "last_tool_summary": frame.last_tool_summary,
        "next_action_hints": frame.next_action_hints[:2],
        "active_run_id": frame.active_run_id,
    }
    try:
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
            json_schema=_schema(),
            schema_name="DeepLensV6Reply",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=180,
            # reasoning_effort="low",
        )
        obj = json.loads(response) if isinstance(response, str) else response
        reply = str(obj.get("reply") or "").strip()
        return reply or base
    except Exception:
        context.logger().warning("deeplens_v6: response compose llm failed", exc_info=True)
        return base
