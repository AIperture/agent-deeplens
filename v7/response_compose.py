from __future__ import annotations

import json
from typing import Any

from .types import DeepLensTask, Plan, PlanStatus, RuntimeState


def _base_reply(*, task: DeepLensTask, plan: Plan, state: RuntimeState, tool_summaries: list[str]) -> str:
    if task.response_request.get("mode") == "explain":
        if state.last_analysis_bundle.get("summary"):
            return state.last_analysis_bundle["summary"]
        if state.active_run_id:
            return f"The active optimization run is `{state.active_run_id}`."
        return "I can help with DeepLens lens creation, analysis, optimization, export, and run control."
    if tool_summaries:
        return "\n".join(tool_summaries[-2:])
    if plan.status == PlanStatus.CANCELLED:
        return "Okay, I stopped before making changes."
    if plan.status == PlanStatus.FAILED:
        return state.final_reply or "The DeepLens workflow failed before producing a result."
    return "Completed the DeepLens workflow."


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"reply": {"type": "string"}},
        "required": ["reply"],
        "additionalProperties": False,
    }


async def compose_reply(
    *,
    task: DeepLensTask,
    plan: Plan,
    state: RuntimeState,
    tool_summaries: list[str],
    context: Any,
) -> str:
    base = _base_reply(task=task, plan=plan, state=state, tool_summaries=tool_summaries)
    if len(base) <= 220:
        return base
    llm = context.llm("fast")
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        "aethergraph-agent-deeplens-v7",
        "deeplens.system",
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )
    payload = {
        "base_reply": base,
        "plan_status": plan.status.value,
        "goal": task.user_goal,
    }
    try:
        response, _usage = await llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n\n"
                        "Rewrite the reply for clarity and brevity. "
                        "Do not add new facts or new actions."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=_schema(),
            schema_name="DeepLensV7Reply",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=180,
        )
        obj = json.loads(response) if isinstance(response, str) else response
        reply = str(obj.get("reply") or "").strip()
        return reply or base
    except Exception:
        context.logger().warning("deeplens_v7: response compose llm failed", exc_info=True)
        return base
