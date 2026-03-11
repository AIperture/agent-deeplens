from __future__ import annotations

import json
from typing import Any

from .extraction import extract_design_spec, infer_analysis_mode, infer_export_formats
from .types import DEEPLENS_SKILL_ID


REFINABLE_TOOLS = {
    "ag.spawn_graph",
    "ag.status",
    "ag.cancel",
    "dl.analysis",
    "dl.create_lens",
    "dl.export_lens",
}


def _extract_latest_user_answer(task: Any) -> str:
    notes = list(getattr(task, "notes", []) or [])
    for note in reversed(notes):
        if isinstance(note, str) and note.startswith("user_answer:"):
            raw = note[len("user_answer:") :]
            if len(raw) >= 2 and raw[0] == raw[-1] == '"':
                return raw[1:-1]
            return raw
    return ""


def _heuristic_refine_inputs(*, spec: Any, draft_inputs: dict[str, Any], task: Any) -> dict[str, Any]:
    refined = dict(draft_inputs)
    latest_answer = _extract_latest_user_answer(task)
    hint_text = latest_answer or getattr(task, "user_goal", "") or ""
    lowered = hint_text.lower()
    if "stub" in lowered:
        refined["use_stub"] = True

    if spec.name == "dl.create_lens":
        design_spec = dict(refined.get("design_spec") or {})
        inferred_spec = extract_design_spec(hint_text)
        for key, value in inferred_spec.items():
            design_spec.setdefault(key, value)
        if design_spec:
            refined["design_spec"] = design_spec
        refined["analysis_request"] = infer_analysis_mode(hint_text, refined.get("analysis_request"))
        refined["formats"] = infer_export_formats(hint_text, refined.get("formats"))

    if spec.name == "dl.analysis":
        refined["analysis_request"] = infer_analysis_mode(hint_text, refined.get("analysis_request"))
        if refined.get("design_spec"):
            refined.setdefault("create_if_missing", True)

    if spec.name == "dl.export_lens":
        refined["formats"] = infer_export_formats(hint_text, refined.get("formats"))

    return refined


async def maybe_refine_tool_inputs_with_llm(
    *,
    spec: Any,
    draft_inputs: dict[str, Any],
    task: Any,
    state: Any,
    context_bundle: Any,
    context: Any,
) -> dict[str, Any]:
    heuristically_refined = _heuristic_refine_inputs(spec=spec, draft_inputs=draft_inputs, task=task)
    if spec.name not in REFINABLE_TOOLS:
        return heuristically_refined

    llm = context.llm("fast")
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        DEEPLENS_SKILL_ID,
        "deeplens.system",
        "deeplens.tool_args",
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )
    user_payload = {
        "tool": spec.name,
        "tool_description": getattr(spec, "description", ""),
        "argument_hints": getattr(spec, "argument_hints", {}) or {},
        "task_shape": task.task_shape.value,
        "domain_hint": task.domain_hint.value,
        "task_goal": task.user_goal,
        "latest_user_answer": _extract_latest_user_answer(task),
        "draft_inputs": heuristically_refined,
        "working_state": getattr(context_bundle, "working_state", {}),
        "state": {
            "active_run_id": state.active_run_id,
            "active_lens_ref": getattr(state, "active_lens_ref", None),
            "active_source_ref": state.active_source_ref,
            "last_metrics": getattr(state, "last_metrics", {}),
            "last_analysis_bundle": getattr(state, "last_analysis_bundle", {}),
            "design_draft": state.design_draft,
        },
    }
    response_schema = {
        "type": "object",
        "properties": {"refined_json": {"type": "string"}},
        "required": ["refined_json"],
        "additionalProperties": False,
    }
    try:
        resp, _usage = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=response_schema,
            schema_name="DeepLensV5ToolInputRefine",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=400,
            # reasoning_effort="low",
        )
        obj = json.loads(resp) if isinstance(resp, str) else resp
        raw_refined = obj.get("refined_json") or "{}"
        parsed = json.loads(raw_refined)
        if isinstance(parsed, dict):
            merged = dict(heuristically_refined)
            merged.update(parsed)
            return merged
    except Exception:
        context.logger().warning("deeplens_v5: tool arg refine llm failed", exc_info=True)
        return heuristically_refined
    return heuristically_refined
