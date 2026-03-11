from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .types import FieldSource, FieldValue, TurnEnvelope


# ---------------------------------------------------------------------
# What to implement here
# ---------------------------------------------------------------------
# - Lightweight lexical / regex extraction
# - Broad candidate field parsing
# - Provenance-aware field output
#
# What should NOT be included here
# - Task switching
# - Tool selection
# - Loop control
# - Repair policy
# ---------------------------------------------------------------------


# ---------------------------
# Regex helpers
# ---------------------------

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

_APPROVE_YES_RE = re.compile(r"^\s*(yes|y|approve|approved|go ahead|do it|run it)\s*$", re.IGNORECASE)
_APPROVE_NO_RE = re.compile(r"^\s*(no|n|reject|rejected|don't|do not)\s*$", re.IGNORECASE)


@dataclass
class ExtractionBundle:
    field_map: dict[str, FieldValue] = field(default_factory=dict)

    design_spec: dict[str, Any] = field(default_factory=dict)
    analysis_request: dict[str, Any] = field(default_factory=dict)
    run_request: dict[str, Any] = field(default_factory=dict)
    delivery_request: dict[str, Any] = field(default_factory=dict)

    missing_fields: list[str] = field(default_factory=list)
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


def _extract_numeric_design_fields(text: str, bundle: ExtractionBundle) -> None:
    m = _FOV_RE.search(text)
    if m:
        val = float(m.group(1))
        bundle.design_spec["fov"] = val
        bundle.field_map["fov"] = _mk_field(
            val,
            source=FieldSource.REGEX,
            confidence=0.95,
            confirmed=True,
            note="Parsed FOV from user text.",
        )

    m = _FNUM_RE.search(text)
    if m:
        val = float(m.group(1))
        bundle.design_spec["fnum"] = val
        bundle.field_map["fnum"] = _mk_field(
            val,
            source=FieldSource.REGEX,
            confidence=0.95,
            confirmed=True,
            note="Parsed F-number from user text.",
        )

    m = _FOCLEN_RE.search(text)
    if m:
        val = float(m.group(1))
        bundle.design_spec["foclen"] = val
        bundle.field_map["foclen"] = _mk_field(
            val,
            source=FieldSource.REGEX,
            confidence=0.94,
            confirmed=True,
            note="Parsed focal length from user text.",
        )

    m = _IMGH_RE.search(text)
    if m:
        val = float(m.group(1))
        bundle.design_spec["imgh"] = val
        bundle.field_map["imgh"] = _mk_field(
            val,
            source=FieldSource.REGEX,
            confidence=0.94,
            confirmed=True,
            note="Parsed image height from user text.",
        )


def _extract_design_intent_hints(text: str, bundle: ExtractionBundle) -> None:
    lowered = text.lower()

    if any(x in lowered for x in ["default", "defaults", "reasonable", "sensible", "typical", "choose for me", "pick for me"]):
        bundle.notes.append("User appears to allow assumptions/defaults for design.")
        bundle.field_map["allows_defaults"] = _mk_field(
            True,
            source=FieldSource.HEURISTIC,
            confidence=0.72,
            inferred=True,
            note="Detected default-permission language.",
        )

    if any(x in lowered for x in ["compact", "small", "starter", "simple"]):
        bundle.notes.append("User provided qualitative design preferences.")


def extract_design_fields(envelope: TurnEnvelope) -> ExtractionBundle:
    text = envelope.cleaned_message
    bundle = ExtractionBundle()

    _extract_numeric_design_fields(text, bundle)
    _extract_design_intent_hints(text, bundle)

    # Broad parse only; do not decide whether these are the current task's required fields.
    if envelope.active_source_ref:
        bundle.field_map["lens_source"] = _mk_field(
            dict(envelope.active_source_ref),
            source=FieldSource.STATE,
            confidence=0.75,
            inferred=True,
            note="Active source ref available from state.",
        )

    lens_att = _first_attachment_of_kind(envelope, "lens")
    if lens_att is not None:
        source_ref = {
            "name": lens_att.get("name") or lens_att.get("filename"),
            "artifact_id": lens_att.get("artifact_id"),
            "uri": lens_att.get("uri"),
        }
        bundle.field_map["lens_source"] = _mk_field(
            source_ref,
            source=FieldSource.ATTACHMENT,
            confidence=0.85,
            confirmed=True,
            note="Using uploaded lens attachment as source.",
        )

    return bundle


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
    m = _RUN_ID_RE.search(text)
    if m:
        return m.group(1)
    return None


def _extract_approval_signal(text: str) -> str | None:
    if _APPROVE_YES_RE.match(text):
        return "approved"
    if _APPROVE_NO_RE.match(text):
        return "rejected"
    return None


def extract_analysis_fields(envelope: TurnEnvelope) -> ExtractionBundle:
    text = envelope.cleaned_message
    bundle = ExtractionBundle()

    mode = _extract_analysis_mode(text)
    if mode:
        bundle.analysis_request["mode"] = mode
        bundle.field_map["analysis_mode"] = _mk_field(
            mode,
            source=FieldSource.HEURISTIC,
            confidence=0.9,
            confirmed=True,
            note="Detected requested analysis mode.",
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
            note="Detected yes/no approval response.",
        )

    if envelope.active_source_ref:
        bundle.field_map["lens_source"] = _mk_field(
            dict(envelope.active_source_ref),
            source=FieldSource.STATE,
            confidence=0.75,
            inferred=True,
            note="Active source ref available from state.",
        )

    lens_att = _first_attachment_of_kind(envelope, "lens")
    if lens_att is not None:
        source_ref = {
            "name": lens_att.get("name") or lens_att.get("filename"),
            "artifact_id": lens_att.get("artifact_id"),
            "uri": lens_att.get("uri"),
        }
        bundle.analysis_request["attachment"] = source_ref
        bundle.field_map["lens_source"] = _mk_field(
            source_ref,
            source=FieldSource.ATTACHMENT,
            confidence=0.88,
            confirmed=True,
            note="Using uploaded lens attachment as source.",
        )

    return bundle