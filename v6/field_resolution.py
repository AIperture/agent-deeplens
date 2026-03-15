from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from .field_specs import CAPABILITY_REQUIRED_FIELDS, field_prompt_fragment
from .types import DEEPLENS_SKILL_ID, DeepLensState, DeepLensTask, DomainHint, FieldSource, FieldValue


@dataclass
class FieldResolutionResult:
    field_map: dict[str, FieldValue] = field(default_factory=dict)
    design_spec: dict[str, Any] = field(default_factory=dict)
    analysis_request: dict[str, Any] = field(default_factory=dict)
    run_request: dict[str, Any] = field(default_factory=dict)
    delivery_request: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    salvage_attempted: bool = False


_NUMBER_RE = r"([0-9]+(?:\.[0-9]+)?)"
_FOV_RE = re.compile(rf"(?:fov|field of view)\s*[:=]?\s*{_NUMBER_RE}", re.IGNORECASE)
_FNUM_RE = re.compile(rf"(?:fnum|f/#|f-number|f number|f/)\s*[:=]?\s*{_NUMBER_RE}", re.IGNORECASE)
_FOCLEN_RE = re.compile(rf"(?:foclen|focal length)\s*[:=]?\s*{_NUMBER_RE}\s*(mm)?", re.IGNORECASE)
_IMGH_RE = re.compile(rf"(?:imgh|image height|sensor height)\s*[:=]?\s*{_NUMBER_RE}\s*(mm)?", re.IGNORECASE)
_BFL_RE = re.compile(rf"bfl\s*[:=]?\s*{_NUMBER_RE}", re.IGNORECASE)
_THICKNESS_RE = re.compile(rf"thickness\s*[:=]?\s*{_NUMBER_RE}", re.IGNORECASE)
_RUN_ID_RE = re.compile(r"(?:run[_\s-]?id)\s*[:=]?\s*([A-Za-z0-9_\-]+)", re.IGNORECASE)


def _mk_field(
    value: Any,
    *,
    source: FieldSource,
    confidence: float,
    inferred: bool = False,
    confirmed: bool = False,
    note: str | None = None,
) -> FieldValue:
    return FieldValue(
        value=value,
        source=source,
        confidence=confidence,
        inferred=inferred,
        confirmed=confirmed,
        notes=[note] if note else [],
    )


def attachment_suggests_lens(attachments: list[dict[str, Any]]) -> bool:
    for attachment in attachments:
        name = attachment.get("name") or attachment.get("filename") or attachment.get("uri") or ""
        if Path(str(name)).suffix.lower() in {".json", ".zmx"}:
            return True
        if attachment.get("kind") == "lens":
            return True
    return False


def extract_design_spec(text: str) -> dict[str, Any]:
    result = _deterministic_extract(message=text or "", attachments=[], state=None)
    return result.design_spec


def extract_analysis_request(message: str, attachments: list[dict[str, Any]]) -> dict[str, Any]:
    result = _deterministic_extract(message=message or "", attachments=attachments, state=None)
    return result.analysis_request


def extract_run_request(message: str) -> dict[str, Any]:
    result = _deterministic_extract(message=message or "", attachments=[], state=None)
    return result.run_request


def missing_design_fields(spec: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if spec.get("fov") is None:
        missing.append("fov")
    if spec.get("fnum") is None:
        missing.append("fnum")
    if spec.get("foclen") is None and spec.get("imgh") is None:
        missing.append("foclen_or_imgh")
    return missing


def infer_analysis_mode(text: str, current: dict[str, Any] | None = None) -> dict[str, Any]:
    analysis_request = dict(current or {})
    mode = _detect_analysis_mode(text or "")
    if mode and analysis_request.get("mode") in {None, "full"}:
        analysis_request["mode"] = mode
    analysis_request.setdefault("mode", "full")
    return analysis_request


def infer_export_formats(text: str, current: list[str] | None = None) -> list[str]:
    formats = list(current or [])
    for item in _detect_export_formats(text or ""):
        if item not in formats:
            formats.append(item)
    return formats or ["json", "zmx"]


def _detect_analysis_mode(text: str) -> str | None:
    lowered = text.lower()
    if "spot" in lowered:
        return "spot"
    if "mtf" in lowered:
        return "mtf"
    if "rms" in lowered:
        return "rms"
    if "full" in lowered or "analy" in lowered or "simulat" in lowered:
        return "full"
    return None


def _detect_export_formats(text: str) -> list[str]:
    formats: list[str] = []
    lowered = text.lower()
    if "json" in lowered:
        formats.append("json")
    if "zmx" in lowered:
        formats.append("zmx")
    return list(dict.fromkeys(formats))


def _deterministic_extract(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    state: DeepLensState | None,
) -> FieldResolutionResult:
    text = message or ""
    result = FieldResolutionResult()

    for key, pattern, note in (
        ("fov", _FOV_RE, "Parsed FOV from user text."),
        ("fnum", _FNUM_RE, "Parsed F-number from user text."),
        ("foclen", _FOCLEN_RE, "Parsed focal length from user text."),
        ("imgh", _IMGH_RE, "Parsed image height from user text."),
        ("bfl", _BFL_RE, "Parsed BFL from user text."),
        ("thickness", _THICKNESS_RE, "Parsed thickness from user text."),
    ):
        match = pattern.search(text)
        if not match:
            continue
        value = float(match.group(1))
        result.design_spec[key] = value
        result.field_map[key] = _mk_field(value, source=FieldSource.REGEX, confidence=0.95, confirmed=True, note=note)

    sensor_match = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*mm", text, re.IGNORECASE)
    if sensor_match:
        width = float(sensor_match.group(1))
        height = float(sensor_match.group(2))
        result.design_spec["sensor_width_mm"] = width
        result.design_spec["sensor_height_mm"] = height
        result.design_spec.setdefault("imgh", max(width, height) / 2.0)
        if result.design_spec.get("foclen") is not None and result.design_spec.get("fov") is None:
            result.design_spec["fov"] = round(2.0 * 180.0 / math.pi * math.atan((width / 2.0) / result.design_spec["foclen"]), 3)

    analysis_mode = _detect_analysis_mode(text)
    if analysis_mode:
        result.analysis_request["mode"] = analysis_mode
        result.field_map["analysis_mode"] = _mk_field(
            analysis_mode,
            source=FieldSource.HEURISTIC,
            confidence=0.88,
            confirmed=True,
            note="Detected analysis mode from user text.",
        )

    export_formats = _detect_export_formats(text)
    if export_formats:
        result.delivery_request["formats"] = export_formats
        result.field_map["export_formats"] = _mk_field(
            export_formats,
            source=FieldSource.HEURISTIC,
            confidence=0.9,
            confirmed=True,
            note="Detected export format request from user text.",
        )

    run_match = _RUN_ID_RE.search(text)
    if run_match:
        result.run_request["run_id"] = run_match.group(1)
        result.field_map["run_id"] = _mk_field(
            run_match.group(1),
            source=FieldSource.REGEX,
            confidence=0.95,
            confirmed=True,
            note="Parsed run id from user text.",
        )

    iterations_match = re.search(r"iterations?\s*[:=]?\s*([0-9]+)", text, re.IGNORECASE)
    if iterations_match:
        result.run_request["iterations"] = int(iterations_match.group(1))
    checkpoint_match = re.search(r"checkpoint(?: every)?\s*[:=]?\s*([0-9]+)", text, re.IGNORECASE)
    if checkpoint_match:
        result.run_request["checkpoint_every"] = int(checkpoint_match.group(1))
    if "stub" in text.lower():
        result.run_request["use_stub"] = True

    if attachment_suggests_lens(attachments):
        first = next((item for item in attachments if attachment_suggests_lens([item])), attachments[0])
        source_ref = {
            "name": first.get("name") or first.get("filename") or first.get("uri"),
            "artifact_id": first.get("artifact_id"),
            "uri": first.get("uri"),
        }
        result.field_map["lens_source"] = _mk_field(
            source_ref,
            source=FieldSource.ATTACHMENT,
            confidence=0.9,
            confirmed=True,
            note="Using uploaded lens attachment as source.",
        )

    if state is not None:
        if state.active_source_ref and "lens_source" not in result.field_map:
            result.field_map["lens_source"] = _mk_field(
                dict(state.active_source_ref),
                source=FieldSource.STATE,
                confidence=0.72,
                inferred=True,
                note="Using active source from state.",
            )
        if state.active_run_id and "run_id" not in result.run_request:
            result.run_request["run_id"] = state.active_run_id
            result.field_map["run_id"] = _mk_field(
                state.active_run_id,
                source=FieldSource.STATE,
                confidence=0.7,
                inferred=True,
                note="Using active run id from state.",
            )

    return result


def required_fields_for_task(task: DeepLensTask) -> list[str]:
    capabilities = list(task.requested_capabilities or [])
    if not capabilities:
        domain_map = {
            DomainHint.DESIGN: ["design"],
            DomainHint.ANALYSIS: ["analysis"],
            DomainHint.OPTIMIZATION: ["optimization"],
            DomainHint.EXPORT: ["export"],
            DomainHint.RUN_CONTROL: ["run_control"],
        }
        capabilities = domain_map.get(task.domain_hint, [])

    required: list[str] = []
    for capability in capabilities:
        for field_name in CAPABILITY_REQUIRED_FIELDS.get(capability, []):
            if field_name not in required:
                required.append(field_name)
    return required


def _task_can_create_lens_before_followup(task: DeepLensTask) -> bool:
    capabilities = list(task.requested_capabilities or [])
    return "design" in capabilities


def compute_missing_fields(task: DeepLensTask) -> list[str]:
    missing: list[str] = []
    for field_name in required_fields_for_task(task):
        if field_name == "fov" and task.design_spec.get("fov") is None:
            missing.append(field_name)
        elif field_name == "fnum" and task.design_spec.get("fnum") is None:
            missing.append(field_name)
        elif field_name == "foclen_or_imgh" and task.design_spec.get("foclen") is None and task.design_spec.get("imgh") is None:
            missing.append(field_name)
        elif (
            field_name == "lens_source"
            and not _task_can_create_lens_before_followup(task)
            and not (task.lens_source or attachment_suggests_lens(task.attachments))
        ):
            missing.append(field_name)
        elif field_name == "run_id" and not task.run_request.get("run_id"):
            missing.append(field_name)
        elif field_name == "export_formats" and not task.delivery_request.get("formats"):
            missing.append(field_name)
    return list(dict.fromkeys(missing))


def build_missing_prompt(task: DeepLensTask, state: DeepLensState | None = None) -> str:
    runtime_invalid = dict(getattr(state, "runtime_invalid_fields", {}) or {}) if state is not None else {}
    if runtime_invalid:
        labels = ", ".join(f"`{name.split('.')[-1]}`" for name in runtime_invalid)
        return f"I found invalid values that need to be corrected before I can continue: {labels}."

    runtime_missing = list(getattr(state, "runtime_missing_fields", []) or []) if state is not None else []
    missing = runtime_missing or compute_missing_fields(task)
    if not missing:
        return "Please provide the missing information so I can continue."
    joined = ", ".join(field_prompt_fragment(name) for name in missing)
    if "lens_source" in missing and len(missing) == 1:
        return "Please upload a `.json` or `.zmx` lens file, or confirm the active lens to use."
    if "run_id" in missing and len(missing) == 1:
        return "I need the background run id before I can check status or cancel it."
    return f"I need the remaining inputs before I can continue: {joined}."


def _salvage_schema(target_fields: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": target_fields},
                        "value_json": {"type": "string"},
                        "confidence": {"type": "number"},
                        "note": {"type": "string"},
                    },
                    "required": ["name", "value_json", "confidence", "note"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["fields"],
        "additionalProperties": False,
    }


def _parse_value(raw: str) -> Any:
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except JSONDecodeError:
        pass
    if re.fullmatch(r"-?[0-9]+", text):
        return int(text)
    if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", text):
        return float(text)
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return text


async def _llm_salvage(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    target_fields: list[str],
    context: Any,
) -> FieldResolutionResult:
    result = FieldResolutionResult(salvage_attempted=True)
    if not target_fields:
        return result
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
        "message": message,
        "target_fields": target_fields,
        "attachments": [
            {
                "name": item.get("name") or item.get("filename"),
                "kind": item.get("kind"),
            }
            for item in attachments[:3]
        ],
    }
    try:
        response, _usage = await llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n\n"
                        "Extract only the requested DeepLens fields from the latest user turn. "
                        "Prefer omission over guessing."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=_salvage_schema(target_fields),
            schema_name="DeepLensV6FieldSalvage",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=220,
            # reasoning_effort="low",
        )
        obj = json.loads(response) if isinstance(response, str) else response
    except Exception:
        context.logger().warning("deeplens_v6: field salvage llm failed", exc_info=True)
        return result

    for item in obj.get("fields", []):
        name = str(item.get("name") or "")
        if name not in target_fields:
            continue
        value = _parse_value(str(item.get("value_json") or ""))
        field = _mk_field(
            value,
            source=FieldSource.LLM,
            confidence=float(item.get("confidence") or 0.0),
            inferred=True,
            note=str(item.get("note") or "LLM extracted field."),
        )
        result.field_map[name] = field
        if name in {"fov", "fnum", "foclen", "imgh"}:
            result.design_spec[name] = value
        elif name == "analysis_mode":
            result.analysis_request["mode"] = value
        elif name == "run_id":
            result.run_request["run_id"] = value
        elif name == "export_formats":
            result.delivery_request["formats"] = list(value or [])
        elif name == "lens_source" and isinstance(value, dict):
            result.field_map[name] = field
    return result


def _merge_result(task: DeepLensTask, result: FieldResolutionResult, *, allow_llm_override: bool = False) -> None:
    for key, field in result.field_map.items():
        current = task.field_map.get(key)
        if current is not None and current.confidence >= field.confidence and field.source == FieldSource.LLM and not allow_llm_override:
            continue
        task.field_map[key] = field
        if key in {"fov", "fnum", "foclen", "imgh", "bfl", "thickness"}:
            task.design_spec[key] = field.value
        elif key == "analysis_mode":
            task.analysis_request["mode"] = field.value
        elif key == "run_id":
            task.run_request["run_id"] = field.value
        elif key == "export_formats":
            task.delivery_request["formats"] = list(field.value or [])
        elif key == "lens_source" and isinstance(field.value, dict):
            task.lens_source = dict(field.value)
    task.design_spec.update({k: v for k, v in result.design_spec.items() if v is not None})
    task.analysis_request.update({k: v for k, v in result.analysis_request.items() if v is not None})
    task.run_request.update({k: v for k, v in result.run_request.items() if v is not None})
    task.delivery_request.update({k: v for k, v in result.delivery_request.items() if v is not None})
    task.notes.extend(result.notes)


async def resolve_task_fields(
    *,
    task: DeepLensTask,
    state: DeepLensState,
    message: str,
    attachments: list[dict[str, Any]],
    context: Any | None,
) -> FieldResolutionResult:
    deterministic = _deterministic_extract(message=message, attachments=attachments, state=state)
    _merge_result(task, deterministic)
    task.missing_fields = compute_missing_fields(task)

    if task.missing_fields and context is not None:
        salvage = await _llm_salvage(
            message=message,
            attachments=attachments,
            target_fields=list(task.missing_fields),
            context=context,
        )
        _merge_result(task, salvage)
        task.missing_fields = compute_missing_fields(task)
        salvage.missing_fields = list(task.missing_fields)
        deterministic.notes.extend(salvage.notes)
        deterministic.salvage_attempted = salvage.salvage_attempted

    deterministic.missing_fields = list(task.missing_fields)
    return deterministic
