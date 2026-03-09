from __future__ import annotations

import json
from typing import Any


REFINABLE_TOOLS = {
    "ag.spawn_graph",
    "ag.wait_run_short",
    "ag.cancel_run",
    "dl.simulate_standard",
    "dl.submit_optimization",
    "dl.check_run_status",
}


async def maybe_refine_tool_inputs_with_llm(
    *,
    spec: Any,
    draft_inputs: dict[str, Any],
    task: Any,
    state: Any,
    context_bundle: Any,
    context: Any,
) -> dict[str, Any]:
    if spec.name not in REFINABLE_TOOLS:
        return draft_inputs

    llm = context.llm()
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        "aethergraph-agent-deeplens",
        "deeplens.system",
        "deeplens.tool_args",
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )
    schema = {
        "type": "object",
        "additionalProperties": True,
    }
    user_payload = {
        "tool": spec.name,
        "task_shape": task.task_shape.value,
        "domain_hint": task.domain_hint.value,
        "task_goal": task.user_goal,
        "draft_inputs": draft_inputs,
        "working_state": context_bundle.working_state,
        "state": {
            "active_run_id": state.active_run_id,
            "active_lens_ref": state.active_lens_ref,
            "last_metrics": state.last_metrics,
        },
    }
    resp, _usage = await llm.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        output_format="json",
        json_schema=schema,
        schema_name="DeepLensToolInputRefine",
        strict_schema=True,
        validate_json=True,
        max_output_tokens=256,
    )
    obj = json.loads(resp) if isinstance(resp, str) else resp
    refined = dict(draft_inputs)
    if isinstance(obj, dict):
        refined.update(obj)
    return refined
