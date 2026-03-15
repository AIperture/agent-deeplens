from __future__ import annotations

import json
import math
import re
from json import JSONDecodeError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .types import DEEPLENS_SKILL_ID
from .types import FieldSource, FieldValue, PendingInteraction, TurnEnvelope


_NUMBER_RE = r"([0-9]+(?:\.[0-9]+)?)"
_FOV_RE = re.compile(rf"(?:fov|field of view)\s*[:=]?\s*{_NUMBER_RE}", re.IGNORECASE)
_FNUM_RE = re.compile(rf"(?:fnum|f/#|f-number|f number)\s*[:=]?\s*{_NUMBER_RE}", re.IGNORECASE)
_FOCLEN_RE = re.compile(rf"(?:foclen|focal length)\s*[:=]?\s*{_NUMBER_RE}\s*(mm)?", re.IGNORECASE)
_IMGH_RE = re.compile(rf"(?:imgh|image height|sensor height)\s*[:=]?\s*{_NUMBER_RE}\s*(mm)?", re.IGNORECASE)

_MTF_RE = re.compile(r"\bmtf\b", re.IGNORECASE)
_SPOT_RE = re.compile(r"\bspot\b", re.IGNORECASE)
_RMS_RE = re.compile(r"\brms\b", re.IGNORECASE)
_FULL_RE = re.compile(r"\bfull\b", re.IGNORECASE)

_EXPORT_ZMX_RE = re.compile(r"\bzmx\b", re.IGNORECASE)
_EXPORT_JSON_RE = re.compile(r"\bjson\b", re.IGNORECASE)

_RUN_ID_RE = re.compile(r"(?:run[_\s-]?id)\s*[:=]?\s*([A-Za-z0-9_\-]+)", re.IGNORECASE)
_RUN_PICKUP_RE = re.compile(r"results?\s+for\s+(?:completed?\s+)?run\s+([A-Za-z0-9_\-]{8,})", re.IGNORECASE)

_APPROVE_YES_RE = re.compile(r"^\s*(yes|y|approve|approved|go ahead|do it|run it|sure)\s*$", re.IGNORECASE)
_APPROVE_NO_RE = re.compile(r"^\s*(no|n|reject|rejected|don't|do not|stop)\s*$", re.IGNORECASE)


@dataclass
class ExtractionBundle:
    field_map: dict[str, FieldValue] = field(default_factory=dict)
    design_spec: dict[str, Any] = field(default_factory=dict)
    analysis_request: dict[str, Any] = field(default_factory=dict)
    run_request: dict[str, Any] = field(default_factory=dict)
    delivery_request: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


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


def _first_attachment_of_kind(envelope: TurnEnvelope, kind: str) -> dict[str, Any] | None:
    for att in envelope.attachments:
        if att.get("kind") == kind:
            return att
    return None


def _merge_bundle(target: ExtractionBundle, other: ExtractionBundle) -> ExtractionBundle:
    target.field_map.update(other.field_map)
    target.design_spec.update(other.design_spec)
    target.analysis_request.update(other.analysis_request)
    target.run_request.update(other.run_request)
    target.delivery_request.update(other.delivery_request)
    target.notes.extend(other.notes)
    return target


def _extract_numeric_design_fields(text: str, bundle: ExtractionBundle) -> None:
    for key, pattern, note in (
        ("fov", _FOV_RE, "Parsed FOV from user text."),
        ("fnum", _FNUM_RE, "Parsed F-number from user text."),
        ("foclen", _FOCLEN_RE, "Parsed focal length from user text."),
        ("imgh", _IMGH_RE, "Parsed image height from user text."),
    ):
        match = pattern.search(text)
        if not match:
            continue
        value = float(match.group(1))
        bundle.design_spec[key] = value
        bundle.field_map[key] = _mk_field(
            value,
            source=FieldSource.REGEX,
            confidence=0.95,
            confirmed=True,
            note=note,
        )


def _extract_design_intent_hints(text: str, bundle: ExtractionBundle) -> None:
    lowered = text.lower()
    if any(x in lowered for x in ["default", "defaults", "reasonable", "sensible", "typical", "choose for me", "pick for me"]):
        bundle.field_map["allows_defaults"] = _mk_field(
            True,
            source=FieldSource.HEURISTIC,
            confidence=0.72,
            inferred=True,
            note="Detected default-permission language.",
        )
        bundle.notes.append("User appears to allow defaults for missing design choices.")
    if any(x in lowered for x in ["compact", "small", "starter", "simple"]):
        bundle.notes.append("User provided qualitative design preferences.")


def _extract_analysis_mode(text: str) -> str | None:
    if _SPOT_RE.search(text):
        return "spot"
    if _MTF_RE.search(text):
        return "mtf"
    if _RMS_RE.search(text):
        return "rms"
    if _FULL_RE.search(text):
        return "full"
    if "analy" in text.lower() or "simulate" in text.lower():
        return "full"
    return None


def _extract_export_formats(text: str) -> list[str]:
    formats: list[str] = []
    if _EXPORT_JSON_RE.search(text):
        formats.append("json")
    if _EXPORT_ZMX_RE.search(text):
        formats.append("zmx")
    return list(dict.fromkeys(formats))


def _extract_run_id(text: str) -> str | None:
    match = _RUN_ID_RE.search(text) or _RUN_PICKUP_RE.search(text)
    if not match:
        return None
    return match.group(1)


def _extract_approval_signal(text: str) -> str | None:
    if _APPROVE_YES_RE.match(text):
        return "approved"
    if _APPROVE_NO_RE.match(text):
        return "rejected"
    return None


def _extract_attachment_source(envelope: TurnEnvelope) -> dict[str, Any] | None:
    lens_att = _first_attachment_of_kind(envelope, "lens")
    if lens_att is None:
        return None
    return {
        "name": lens_att.get("name") or lens_att.get("filename"),
        "artifact_id": lens_att.get("artifact_id"),
        "uri": lens_att.get("uri"),
    }


def _extract_regex_bundle(envelope: TurnEnvelope) -> ExtractionBundle:
    text = envelope.cleaned_message
    bundle = ExtractionBundle()
    _extract_numeric_design_fields(text, bundle)
    _extract_design_intent_hints(text, bundle)

    mode = _extract_analysis_mode(text)
    if mode:
        bundle.analysis_request["mode"] = mode
        bundle.field_map["analysis_mode"] = _mk_field(
            mode,
            source=FieldSource.HEURISTIC,
            confidence=0.9,
            confirmed=True,
            note="Detected analysis mode from user text.",
        )

    export_formats = _extract_export_formats(text)
    if export_formats:
        bundle.delivery_request["formats"] = export_formats
        bundle.field_map["export_formats"] = _mk_field(
            export_formats,
            source=FieldSource.HEURISTIC,
            confidence=0.9,
            confirmed=True,
            note="Detected requested export formats.",
        )

    run_id = _extract_run_id(text)
    if run_id:
        bundle.run_request["run_id"] = run_id
        bundle.field_map["run_id"] = _mk_field(
            run_id,
            source=FieldSource.REGEX,
            confidence=0.95,
            confirmed=True,
            note="Parsed run id from user text.",
        )

    approval = _extract_approval_signal(text)
    if approval:
        bundle.run_request["approval_response"] = approval
        bundle.field_map["approval_response"] = _mk_field(
            approval,
            source=FieldSource.HEURISTIC,
            confidence=0.92,
            confirmed=True,
            note="Detected approval response.",
        )

    source_ref = _extract_attachment_source(envelope)
    if source_ref:
        bundle.field_map["lens_source"] = _mk_field(
            source_ref,
            source=FieldSource.ATTACHMENT,
            confidence=0.88,
            confirmed=True,
            note="Using uploaded lens attachment as source.",
        )
        bundle.analysis_request["attachment"] = source_ref
    elif envelope.active_source_ref:
        bundle.field_map["lens_source"] = _mk_field(
            dict(envelope.active_source_ref),
            source=FieldSource.STATE,
            confidence=0.75,
            inferred=True,
            note="Active source ref available from state.",
        )

    return bundle


def _llm_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": [
                                "fov",
                                "fnum",
                                "foclen",
                                "imgh",
                                "analysis_mode",
                                "export_formats",
                                "run_id",
                                "approval_response",
                                "lens_source",
                                "allows_defaults",
                            ],
                        },
                        "value_json": {"type": "string"},
                        "confidence": {"type": "number"},
                        "note": {"type": "string"},
                    },
                    "required": ["name", "value_json", "confidence", "note"],
                    "additionalProperties": False,
                },
            },
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["fields", "notes"],
        "additionalProperties": False,
    }


def _coerce_scalar_text(raw: str) -> Any:
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    if re.fullmatch(r"-?[0-9]+", raw):
        try:
            return int(raw)
        except Exception:
            return raw
    if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", raw):
        try:
            return float(raw)
        except Exception:
            return raw
    return raw


def _parse_llm_field_value(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except JSONDecodeError:
        pass

    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        inner = text[1:-1]
        try:
            return json.loads(inner)
        except Exception:
            return inner

    try:
        decoder = json.JSONDecoder()
        value, end = decoder.raw_decode(text)
        if text[end:].strip():
            return text
        return value
    except JSONDecodeError:
        return _coerce_scalar_text(text)


async def _extract_with_llm(
    *,
    envelope: TurnEnvelope,
    pending: PendingInteraction | None,
    pre_context: dict[str, Any] | None,
    context: Any,
) -> ExtractionBundle:
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
        "message": envelope.cleaned_message,
        "attachments": [
            {
                "name": item.get("name") or item.get("filename"),
                "kind": item.get("kind"),
                "artifact_id": item.get("artifact_id"),
            }
            for item in envelope.attachments[:3]
        ],
        "pending_interaction": {
            "kind": pending.kind.value,
            "expected_fields": pending.expected_fields,
            "response_shape": pending.response_shape.value,
            "prompt": pending.prompt,
        }
        if pending
        else None,
        "active_source_ref": envelope.active_source_ref or None,
        "pre_context": pre_context or {},
    }
    bundle = ExtractionBundle()
    try:
        response, _usage = await llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n\n"
                        "Extract only explicit or strongly implied structured DeepLens fields from the latest user turn. "
                        "Return no prose outside the schema. Prefer omission over guessing."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=_llm_schema(),
            schema_name="DeepLensV5Extraction",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=350,
            # reasoning_effort="low",
        )
        obj = json.loads(response) if isinstance(response, str) else response
    except Exception:
        context.logger().warning("deeplens_v5: extraction llm failed", exc_info=True)
        return bundle

    for item in obj.get("fields", []):
        try:
            name = str(item["name"])
            value = _parse_llm_field_value(item.get("value_json"))
            confidence = float(item.get("confidence") or 0.0)
            note = str(item.get("note") or "LLM extracted field.")
        except Exception:
            context.logger().warning("deeplens_v5: malformed extraction field", exc_info=True)
            continue
        bundle.field_map[name] = _mk_field(
            value,
            source=FieldSource.LLM,
            confidence=confidence,
            inferred=True,
            note=note,
        )
        if name in {"fov", "fnum", "foclen", "imgh"}:
            bundle.design_spec[name] = value
        elif name == "analysis_mode":
            bundle.analysis_request["mode"] = value
        elif name == "export_formats":
            bundle.delivery_request["formats"] = list(value or [])
        elif name == "run_id":
            bundle.run_request["run_id"] = value
        elif name == "approval_response":
            bundle.run_request["approval_response"] = value
        elif name == "lens_source":
            bundle.analysis_request.setdefault("attachment", value)
        elif name == "allows_defaults":
            pass

    bundle.notes.extend([str(note) for note in obj.get("notes", []) if str(note).strip()])
    return bundle


async def extract_turn_fields(
    *,
    envelope: TurnEnvelope,
    pending: PendingInteraction | None,
    pre_context: dict[str, Any] | None,
    context: Any,
) -> ExtractionBundle:
    bundle = _extract_regex_bundle(envelope)
    llm_bundle = await _extract_with_llm(
        envelope=envelope,
        pending=pending,
        pre_context=pre_context,
        context=context,
    )
    return _merge_bundle(bundle, llm_bundle)


def extract_design_spec(text: str) -> dict[str, Any]:
    envelope = TurnEnvelope(raw_message=text or "", cleaned_message=text or "")
    return _extract_regex_bundle(envelope).design_spec


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
    lowered = (text or "").lower()
    if "mtf" in lowered and analysis_request.get("mode") in {None, "full"}:
        analysis_request["mode"] = "mtf"
    if "spot" in lowered and analysis_request.get("mode") in {None, "full"}:
        analysis_request["mode"] = "spot"
    if "rms" in lowered and analysis_request.get("mode") in {None, "full"}:
        analysis_request["mode"] = "rms"
    if "full" in lowered or not analysis_request:
        analysis_request.setdefault("mode", "full")
    return analysis_request


def infer_export_formats(text: str, current: list[str] | None = None) -> list[str]:
    formats = list(current or [])
    lowered = (text or "").lower()
    if "json" in lowered and "json" not in formats:
        formats.append("json")
    if "zmx" in lowered and "zmx" not in formats:
        formats.append("zmx")
    if not formats:
        return ["json", "zmx"]
    return formats


def attachment_suggests_lens(attachments: list[dict[str, Any]]) -> bool:
    for attachment in attachments:
        name = attachment.get("name") or attachment.get("filename") or attachment.get("uri") or ""
        if Path(str(name)).suffix.lower() in {".json", ".zmx"}:
            return True
    return False
