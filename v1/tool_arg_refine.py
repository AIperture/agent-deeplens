from __future__ import annotations

import json
from typing import Any


async def maybe_refine_tool_inputs_with_llm(
    *,
    spec: Any,
    draft_inputs: dict[str, Any],
    task: Any,
    state: Any,
    context: Any,
) -> dict[str, Any]:
    # Only use for tools that benefit from argument refinement.
    if spec.name not in {"run_analysis", "run_targeted_eval", "run_diagnostic"}:
        return draft_inputs

    llm = context.llm()
    skills = context.skills()

    system_prompt = skills.compile_prompt(
        "geolens-design",
        "deeplens.system",
        "deeplens.analysis",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )

    schema = {
        "type": "object",
        "properties": {
            "eval_type": {"type": "string"},
            "active_lens_ref": {"type": ["string", "null"]},
            "notes": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["eval_type"],
        "additionalProperties": True,
    }

    user_payload = {
        "tool": spec.name,
        "task_goal": task.user_goal,
        "draft_inputs": draft_inputs,
        "state": {
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
    refined.update(obj)
    return refined