from __future__ import annotations

import ast
import json
import math
import re
from pathlib import Path
from typing import Any

from .field_specs import FIELD_SPECS
from .types import DeepLensTask, RuntimeState


CAPABILITY_ENUM = ["design", "analysis", "optimize", "export", "status", "cancel", "explain"]
ANALYSIS_MODE_ENUM = ["full", "spot", "mtf", "rms"]
FORMAT_ENUM = ["json", "zmx"]
SURFACE_TOKEN_MAP = {
    "aspheric": "Aspheric",
    "aspherical": "Aspheric",
    "spheric": "Spheric",
    "spherical": "Spheric",
    "aperture": "Aperture",
}
NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
}


def _extract_number(text: str, patterns: list[str]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except Exception:
                continue
    return None


def attachment_suggests_lens(attachments: list[dict[str, Any]]) -> bool:
    for attachment in attachments:
        name = attachment.get("name") or attachment.get("filename") or attachment.get("uri") or ""
        if Path(str(name)).suffix.lower() in {".json", ".zmx"}:
            return True
    return False


def _extract_lens_source(attachments: list[dict[str, Any]]) -> dict[str, Any]:
    for attachment in attachments:
        name = attachment.get("name") or attachment.get("filename") or attachment.get("uri") or ""
        if Path(str(name)).suffix.lower() not in {".json", ".zmx"}:
            continue
        return {
            "artifact_id": attachment.get("artifact_id"),
            "uri": attachment.get("uri"),
            "name": attachment.get("name") or attachment.get("filename"),
        }
    return {}


def _normalize_surface_token(value: str) -> str | None:
    token = SURFACE_TOKEN_MAP.get(str(value).strip().lower())
    return token


def _normalize_surf_list(raw: Any) -> list[list[str]] | None:
    if not isinstance(raw, list):
        return None
    normalized: list[list[str]] = []
    for item in raw:
        if isinstance(item, str):
            token = _normalize_surface_token(item)
            if token is None:
                return None
            normalized.append([token])
            continue
        if isinstance(item, (list, tuple)):
            element: list[str] = []
            for part in item:
                token = _normalize_surface_token(str(part))
                if token is None:
                    return None
                element.append(token)
            if not element:
                return None
            normalized.append(element)
            continue
        return None
    return normalized or None


def _find_balanced_bracket_block(text: str, start_index: int) -> str | None:
    depth = 0
    in_string = False
    string_quote = ""
    start = -1
    for index in range(start_index, len(text)):
        char = text[index]
        if in_string:
            if char == string_quote and text[index - 1] != "\\":
                in_string = False
            continue
        if char in {"'", '"'}:
            in_string = True
            string_quote = char
            continue
        if char == "[":
            if depth == 0:
                start = index
            depth += 1
            continue
        if char == "]" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : index + 1]
    return None


def _parse_explicit_surf_list(text: str) -> list[list[str]] | None:
    lowered = text.lower()
    anchor = lowered.find("surf_list")
    search_start = anchor if anchor >= 0 else 0
    candidate = _find_balanced_bracket_block(text, search_start)
    if not candidate or not any(token in candidate.lower() for token in ("aspheric", "aspherical", "spheric", "spherical", "aperture")):
        return None
    normalized_candidate = re.sub(
        r"\b(Aspheric|Aspherical|Spheric|Spherical|Aperture)\b",
        lambda match: f'"{SURFACE_TOKEN_MAP[match.group(1).lower()]}"',
        candidate,
    )
    try:
        parsed = ast.literal_eval(normalized_candidate)
    except Exception:
        return None
    return _normalize_surf_list(parsed)


def _word_to_count(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token.lower())


def _heuristic_surf_list(text: str) -> list[list[str]] | None:
    lowered = text.lower()
    count_match = re.search(
        r"\b(\d+|one|two|three|four|five|six|seven|eight)\s+(?:lens(?:es)?|elements?|lens elements?)\b",
        lowered,
    )
    if not count_match:
        return None
    count = _word_to_count(count_match.group(1))
    if not count or count <= 0:
        return None

    surface_type = None
    if any(token in lowered for token in ("all aspherical", "all aspheric", "aspherical surfaces", "aspheric surfaces")):
        surface_type = "Aspheric"
    elif any(token in lowered for token in ("all spherical", "all spheric", "spherical surfaces", "spheric surfaces")):
        surface_type = "Spheric"
    if surface_type is None:
        return None

    elements = [[surface_type, surface_type] for _ in range(count)]
    aperture_index = max(1, count // 2)
    elements.insert(aperture_index, ["Aperture"])
    return elements


def _extract_surf_list(text: str) -> list[list[str]] | None:
    explicit = _parse_explicit_surf_list(text)
    if explicit is not None:
        return explicit
    return _heuristic_surf_list(text)


def _extract_design_spec(text: str) -> dict[str, Any]:
    msg = text or ""
    lowered = msg.lower()
    spec: dict[str, Any] = {}

    fov = _extract_number(msg, [r"fov\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)", r"([0-9]+(?:\.[0-9]+)?)\s*deg(?:ree)?s?\s*fov"])
    if fov is not None:
        spec["fov"] = fov

    fnum = _extract_number(msg, [r"f/?#?\s*([0-9]+(?:\.[0-9]+)?)", r"fnum\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)", r"f\s*/\s*(\d+(?:\.\d+)?)"])
    if fnum is not None:
        spec["fnum"] = fnum

    foclen = _extract_number(msg, [r"foc(?:al)?(?:len| length)?\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)", r"([0-9]+(?:\.[0-9]+)?)\s*mm\s*focal"])
    if foclen is not None:
        spec["foclen"] = foclen

    imgh = _extract_number(msg, [r"imgh\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)", r"image height\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)"])
    if imgh is not None:
        spec["imgh"] = imgh

    bfl = _extract_number(msg, [r"bfl\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)"])
    if bfl is not None:
        spec["bfl"] = bfl

    thickness = _extract_number(msg, [r"thickness\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)"])
    if thickness is not None:
        spec["thickness"] = thickness

    sensor_match = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*mm", msg, re.IGNORECASE)
    if sensor_match:
        width = float(sensor_match.group(1))
        height = float(sensor_match.group(2))
        spec["sensor_width_mm"] = width
        spec["sensor_height_mm"] = height
        spec.setdefault("imgh", max(width, height) / 2.0)
        if spec.get("foclen") is not None and spec.get("fov") is None:
            spec["fov"] = round(2.0 * 180.0 / math.pi * math.atan((width / 2.0) / spec["foclen"]), 3)

    wavelengths = re.findall(r"(\d+(?:\.\d+)?)\s*nm", msg, re.IGNORECASE)
    if wavelengths:
        spec["wvlns"] = [float(item) for item in wavelengths]

    if "camera" in lowered:
        spec["lens_class"] = "camera"
    elif "cellphone" in lowered or "mobile" in lowered:
        spec["lens_class"] = "cellphone"

    save_match = re.search(r"\b(?:as|named?)\s+([A-Za-z0-9_.-]+)", msg)
    if save_match and any(token in lowered for token in ("json", "zmx", "design", "lens")):
        spec["save_name"] = save_match.group(1).removesuffix(".json").removesuffix(".zmx")

    surf_list = _extract_surf_list(msg)
    if surf_list is not None:
        spec["surf_list"] = surf_list

    if any(phrase in lowered for phrase in ("use defaults", "use default", "sensible defaults", "sensible default", "default config")):
        spec.setdefault("fov", 40.0)
        spec.setdefault("fnum", 2.8)
        spec.setdefault("foclen", 35.0)
        spec.setdefault("wvlns", [486.0, 588.0, 656.0])
        spec.setdefault("sensor_width_mm", 36.0)
        spec.setdefault("sensor_height_mm", 24.0)
        spec.setdefault("imgh", 18.0)
        spec.setdefault("save_name", "deeplens_design")

    return spec


def _extract_analysis_request(text: str) -> dict[str, Any]:
    lowered = (text or "").lower()
    if "spot" in lowered:
        return {"mode": "spot"}
    if "mtf" in lowered:
        return {"mode": "mtf"}
    if "rms" in lowered:
        return {"mode": "rms"}
    return {}


def _extract_run_request(text: str) -> dict[str, Any]:
    msg = text or ""
    lowered = msg.lower()
    request: dict[str, Any] = {}

    iterations = _extract_number(msg, [r"iterations?\s*[:=]?\s*([0-9]+)", r"for\s*([0-9]+)\s*iters"])
    if iterations is not None:
        request["iterations"] = int(iterations)

    checkpoint_every = _extract_number(msg, [r"checkpoint(?: every)?\s*[:=]?\s*([0-9]+)", r"every\s*([0-9]+)\s*iterations"])
    if checkpoint_every is not None:
        request["checkpoint_every"] = int(checkpoint_every)

    run_id_match = re.search(r"run(?:\s+id)?\s*[:=]?\s*([A-Za-z0-9._-]{6,})", msg, re.IGNORECASE)
    if run_id_match:
        request["run_id"] = run_id_match.group(1)

    if "stub" in lowered:
        request["use_stub"] = True

    goal_match = re.search(r"(?:goal|optimi[sz]e(?: for)?)\s*[:=]?\s*([^\n,.;]+)", msg, re.IGNORECASE)
    if goal_match:
        request["goal"] = goal_match.group(1).strip()

    constraints = re.findall(r"(?:constraint|keep|maintain)\s*[:=]?\s*([^\n,.;]+)", msg, re.IGNORECASE)
    if constraints:
        request["constraints"] = [item.strip() for item in constraints if item.strip()]

    excluded = re.findall(r"(?:do not|don't|exclude|avoid)\s+([^\n,.;]+)", msg, re.IGNORECASE)
    if excluded:
        request["excluded_objectives"] = [item.strip() for item in excluded if item.strip()]

    formats = _extract_formats(msg)
    if formats:
        request["export_formats"] = formats

    return request


def _extract_formats(text: str) -> list[str]:
    lowered = (text or "").lower()
    formats: list[str] = []
    if "json" in lowered:
        formats.append("json")
    if "zmx" in lowered:
        formats.append("zmx")
    return formats


def extract_deterministic_inputs(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    state: RuntimeState | None = None,
) -> dict[str, Any]:
    lowered = (message or "").lower()
    evidence = {
        "design_spec": _extract_design_spec(message),
        "analysis_request": _extract_analysis_request(message),
        "run_request": _extract_run_request(message),
        "delivery_request": {},
        "lens_source": _extract_lens_source(attachments),
        "use_active_lens": any(token in lowered for token in ("active lens", "current lens", "loaded lens")),
    }
    formats = _extract_formats(message)
    if formats:
        evidence["delivery_request"] = {"formats": formats}
    if not evidence["lens_source"] and evidence["use_active_lens"] and state and state.active_source_ref:
        evidence["lens_source"] = dict(state.active_source_ref)
    return evidence


def build_extraction_schema(*, include_capabilities: bool) -> dict[str, Any]:
    design_spec_properties = {
        "fov": {"type": ["number", "null"]},
        "fnum": {"type": ["number", "null"]},
        "foclen": {"type": ["number", "null"]},
        "imgh": {"type": ["number", "null"]},
        "bfl": {"type": ["number", "null"]},
        "thickness": {"type": ["number", "null"]},
        "save_name": {"type": ["string", "null"]},
        "surf_list": {
            "type": ["array", "null"],
            "items": {
                "type": "array",
                "items": {"type": "string", "enum": ["Aspheric", "Spheric", "Aperture"]},
            },
        },
        "sensor_width_mm": {"type": ["number", "null"]},
        "sensor_height_mm": {"type": ["number", "null"]},
        "wvlns": {"type": ["array", "null"], "items": {"type": "number"}},
        "lens_class": {"type": ["string", "null"]},
    }
    analysis_request_properties = {
        "mode": {"type": ["string", "null"], "enum": [*ANALYSIS_MODE_ENUM, None]},
    }
    run_request_properties = {
        "goal": {"type": ["string", "null"]},
        "constraints": {"type": ["array", "null"], "items": {"type": "string"}},
        "excluded_objectives": {"type": ["array", "null"], "items": {"type": "string"}},
        "iterations": {"type": ["integer", "null"]},
        "checkpoint_every": {"type": ["integer", "null"]},
        "export_formats": {"type": ["array", "null"], "items": {"type": "string", "enum": FORMAT_ENUM}},
        "use_stub": {"type": ["boolean", "null"]},
        "run_id": {"type": ["string", "null"]},
    }
    delivery_request_properties = {
        "formats": {"type": ["array", "null"], "items": {"type": "string", "enum": FORMAT_ENUM}},
    }
    root_properties: dict[str, Any] = {
        "design_spec": {
            "type": "object",
            "properties": design_spec_properties,
            "required": list(design_spec_properties.keys()),
            "additionalProperties": False,
        },
        "analysis_request": {
            "type": "object",
            "properties": analysis_request_properties,
            "required": list(analysis_request_properties.keys()),
            "additionalProperties": False,
        },
        "run_request": {
            "type": "object",
            "properties": run_request_properties,
            "required": list(run_request_properties.keys()),
            "additionalProperties": False,
        },
        "delivery_request": {
            "type": "object",
            "properties": delivery_request_properties,
            "required": list(delivery_request_properties.keys()),
            "additionalProperties": False,
        },
    }
    required = ["design_spec", "analysis_request", "run_request", "delivery_request"]
    if include_capabilities:
        root_properties["requested_capabilities"] = {
            "type": "array",
            "items": {"type": "string", "enum": CAPABILITY_ENUM},
        }
        root_properties["response_mode"] = {"type": "string", "enum": ["workflow", "explain"]}
        required = ["requested_capabilities", *required, "response_mode"]
    return {
        "type": "object",
        "properties": root_properties,
        "required": required,
        "additionalProperties": False,
    }


def normalize_schema_payload(payload: dict[str, Any] | None, *, include_capabilities: bool) -> dict[str, Any]:
    raw = payload or {}
    normalized = {
        "design_spec": {k: v for k, v in dict(raw.get("design_spec") or {}).items() if v is not None},
        "analysis_request": {k: v for k, v in dict(raw.get("analysis_request") or {}).items() if v is not None},
        "run_request": {k: v for k, v in dict(raw.get("run_request") or {}).items() if v is not None},
        "delivery_request": {k: v for k, v in dict(raw.get("delivery_request") or {}).items() if v is not None},
    }
    if include_capabilities:
        normalized["requested_capabilities"] = [str(item) for item in (raw.get("requested_capabilities") or []) if str(item) in CAPABILITY_ENUM]
        normalized["response_mode"] = str(raw.get("response_mode") or "workflow")
    return normalized


def merge_task_update(
    *,
    base_task: DeepLensTask,
    deterministic: dict[str, Any],
    llm_payload: dict[str, Any] | None,
    attachments: list[dict[str, Any]],
    state: RuntimeState | None = None,
    include_capabilities: bool,
) -> DeepLensTask:
    normalized_llm = normalize_schema_payload(llm_payload, include_capabilities=include_capabilities)
    merged = DeepLensTask.from_dict(base_task.to_dict()) or base_task
    merged.attachments = list(base_task.attachments) + list(attachments)

    for bucket in ("design_spec", "analysis_request", "run_request", "delivery_request"):
        current_value = dict(getattr(merged, bucket) or {})
        llm_value = dict(normalized_llm.get(bucket) or {})
        deterministic_value = dict(deterministic.get(bucket) or {})
        current_value.update(llm_value)
        current_value.update(deterministic_value)
        setattr(merged, bucket, current_value)

    if include_capabilities:
        capabilities = list(normalized_llm.get("requested_capabilities") or merged.requested_capabilities)
        if capabilities:
            merged.requested_capabilities = capabilities
        merged.response_request = {"mode": str(normalized_llm.get("response_mode") or merged.response_request.get("mode") or "workflow")}

    deterministic_lens_source = dict(deterministic.get("lens_source") or {})
    if deterministic_lens_source:
        merged.lens_source = deterministic_lens_source
    elif deterministic.get("use_active_lens") and state and state.active_source_ref:
        merged.lens_source = dict(state.active_source_ref)

    return merged


def parse_llm_json_response(response: Any) -> dict[str, Any]:
    if isinstance(response, str):
        try:
            return dict(json.loads(response))
        except Exception:
            return {}
    if isinstance(response, dict):
        return dict(response)
    return {}


def render_missing_fields_prompt(*, missing_paths: list[str], tool_name: str) -> str:
    labels: list[str] = []
    hints: list[str] = []
    for path in missing_paths:
        spec = FIELD_SPECS.get(path)
        if spec is None:
            labels.append(path)
            continue
        labels.append(spec.label)
        if spec.help_text:
            hints.append(spec.help_text)
    rendered = ", ".join(labels) if labels else "the missing inputs"
    prompt = f"I need {rendered} before I can run `{tool_name}`."
    if hints:
        prompt += "\n" + " ".join(dict.fromkeys(hints))
    return prompt
