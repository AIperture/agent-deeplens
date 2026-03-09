from __future__ import annotations

import json
import re
from typing import Any


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


def _extract_design_spec_from_text(text: str) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    if not text:
        return spec

    foclen_match = re.search(r"(\d+(?:\.\d+)?)\s*mm", text, re.IGNORECASE)
    if foclen_match:
        spec["foclen"] = float(foclen_match.group(1))

    fnum_match = re.search(r"f\s*/\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if fnum_match:
        spec["fnum"] = float(fnum_match.group(1))

    sensor_match = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*mm", text, re.IGNORECASE)
    if sensor_match:
        width = float(sensor_match.group(1))
        height = float(sensor_match.group(2))
        spec["sensor_width_mm"] = width
        spec["sensor_height_mm"] = height
        spec["imgh"] = max(width, height) / 2.0
        if spec.get("foclen") is not None and spec.get("fov") is None:
            spec["fov"] = round(2.0 * 180.0 / 3.141592653589793 * __import__("math").atan((width / 2.0) / spec["foclen"]), 3)

    wavelengths = re.findall(r"(\d+(?:\.\d+)?)\s*nm", text, re.IGNORECASE)
    if wavelengths:
        spec["wvlns"] = [float(item) for item in wavelengths]

    save_match = re.search(r"\b(?:as|named?)\s+([A-Za-z0-9_.-]+)", text)
    if save_match and ("json" in text.lower() or "zmx" in text.lower()):
        spec["save_name"] = save_match.group(1).removesuffix(".json").removesuffix(".zmx")

    lowered = text.lower()
    if (
        "use defaults" in lowered
        or "use default" in lowered
        or "sensible defaults" in lowered
        or "sensible default" in lowered
        or "default config" in lowered
    ):
        spec.setdefault("fov", 40.0)
        spec.setdefault("fnum", 2.8)
        spec.setdefault("foclen", 35.0)
        spec.setdefault("wvlns", [486.0, 588.0, 656.0])
        spec.setdefault("sensor_width_mm", 36.0)
        spec.setdefault("sensor_height_mm", 24.0)
        spec.setdefault("imgh", 18.0)
        spec.setdefault("save_name", "deeplens_design")

    return spec


def _infer_analysis_request(text: str, draft_inputs: dict[str, Any]) -> dict[str, Any]:
    analysis_request = dict(draft_inputs.get("analysis_request") or {})
    lowered = text.lower()
    if "mtf" in lowered and analysis_request.get("mode") in {None, "full"}:
        analysis_request["mode"] = "mtf"
    if "spot" in lowered and analysis_request.get("mode") in {None, "full"}:
        analysis_request["mode"] = "spot"
    if "rms" in lowered and analysis_request.get("mode") in {None, "full"}:
        analysis_request["mode"] = "rms"
    if not analysis_request:
        analysis_request["mode"] = "full"
    return analysis_request


def _infer_formats(text: str, draft_inputs: dict[str, Any]) -> list[str]:
    formats = list(draft_inputs.get("formats") or [])
    lowered = text.lower()
    if "json" in lowered and "json" not in formats:
        formats.append("json")
    if "zmx" in lowered and "zmx" not in formats:
        formats.append("zmx")
    if not formats:
        formats = ["json", "zmx"]
    return formats


def _heuristic_refine_inputs(*, spec: Any, draft_inputs: dict[str, Any], task: Any) -> dict[str, Any]:
    refined = dict(draft_inputs)
    latest_answer = _extract_latest_user_answer(task)
    hint_text = latest_answer or getattr(task, "user_goal", "") or ""
    lowered = hint_text.lower()
    if "stub" in lowered:
        refined["use_stub"] = True

    if spec.name == "dl.create_lens":
        design_spec = dict(refined.get("design_spec") or {})
        inferred_spec = _extract_design_spec_from_text(hint_text)
        for key, value in inferred_spec.items():
            design_spec.setdefault(key, value)
        if design_spec:
            refined["design_spec"] = design_spec
        refined["analysis_request"] = _infer_analysis_request(hint_text, refined)
        refined["formats"] = _infer_formats(hint_text, refined)

    if spec.name == "dl.analysis":
        refined["analysis_request"] = _infer_analysis_request(hint_text, refined)
        if refined.get("design_spec"):
            refined.setdefault("create_if_missing", True)

    if spec.name == "dl.export_lens":
        refined["formats"] = _infer_formats(hint_text, refined)

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

    llm = context.llm()
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        "aethergraph-agent-deeplens-v3",
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
        "working_state": context_bundle.working_state,
        "state": {
            "active_run_id": state.active_run_id,
            "active_lens_ref": state.active_lens_ref,
            "active_source_ref": state.active_source_ref,
            "last_metrics": state.last_metrics,
            "last_analysis_bundle": state.last_analysis_bundle,
            "design_draft": state.design_draft,
        },
    }
    response_schema = {
        "type": "object",
        "properties": {
            "refined_json": {"type": "string"},
        },
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
            schema_name="DeepLensV3ToolInputRefine",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=400,
            reasoning_effort="low",
        )
        obj = json.loads(resp) if isinstance(resp, str) else resp
        raw_refined = obj.get("refined_json") or "{}"
        parsed = json.loads(raw_refined)
        if isinstance(parsed, dict):
            merged = dict(heuristically_refined)
            merged.update(parsed)
            return merged
    except Exception:
        return heuristically_refined
    return heuristically_refined
