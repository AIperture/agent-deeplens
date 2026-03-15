from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .types import FieldSource, FieldValue, TurnEnvelope


# ----------------------------
# result container
# ----------------------------

@dataclass
class ExtractionBundle:
    field_map: dict[str, FieldValue] = field(default_factory=dict)
    design_spec: dict[str, Any] = field(default_factory=dict)
    analysis_request: dict[str, Any] = field(default_factory=dict)
    run_request: dict[str, Any] = field(default_factory=dict)
    delivery_request: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ----------------------------
# regex helpers
# ----------------------------

_FLOAT_RE = r"[-+]?\d+(?:\.\d+)?"

_RE_FOV = re.compile(rf"\b(?:fov|field\s+of\s+view)\s*(?:=|is|of)?\s*({_FLOAT_RE})\s*(deg|degree|degrees)?\b", re.I)
_RE_FNUM = re.compile(rf"\b(?:f/#|fnum|f-number|f number)\s*(?:=|is|of)?\s*({_FLOAT_RE})\b", re.I)
_RE_FOCAL = re.compile(rf"\b(?:focal\s*length|foclen)\s*(?:=|is|of)?\s*({_FLOAT_RE})\s*(mm|millimeter|millimeters)?\b", re.I)
_RE_IMGH = re.compile(rf"\b(?:image\s*height|imgh)\s*(?:=|is|of)?\s*({_FLOAT_RE})\s*(mm|millimeter|millimeters)?\b", re.I)
_RE_WAVELENGTH = re.compile(rf"\b(?:wavelength|lambda)\s*(?:=|is|of)?\s*({_FLOAT_RE})\s*(nm|um|μm|micron|microns)?\b", re.I)
_RE_APERTURE = re.compile(rf"\b(?:aperture)\s*(?:=|is|of)?\s*({_FLOAT_RE})\s*(mm|millimeter|millimeters)?\b", re.I)

_RE_MTF = re.compile(r"\bmtf\b", re.I)
_RE_SPOT = re.compile(r"\bspot\b", re.I)
_RE_RMS = re.compile(r"\brms\b", re.I)


def _to_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _set_field(
    bundle: ExtractionBundle,
    field_name: str,
    value: Any,
    *,
    source: FieldSource,
    confidence: float,
    inferred: bool = False,
    confirmed: bool = False,
    note: str | None = None,
) -> None:
    if value is None:
        return
    if field_name in bundle.field_map and bundle.field_map[field_name].value not in (None, "", []):
        return

    bundle.field_map[field_name] = FieldValue(
        value=value,
        source=source,
        confidence=confidence,
        inferred=inferred,
        confirmed=confirmed,
        notes=[note] if note else [],
    )


def _first_lens_attachment(envelope: TurnEnvelope) -> dict[str, Any] | None:
    for att in envelope.attachments:
        if att.get("kind") == "lens":
            return att
    return None


def _dedupe_missing(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


# ----------------------------
# design extraction
# ----------------------------

def extract_design_fields(envelope: TurnEnvelope) -> ExtractionBundle:
    text = envelope.cleaned_message
    bundle = ExtractionBundle()

    m = _RE_FOV.search(text)
    if m:
        v = _to_float(m.group(1))
        if v is not None:
            bundle.design_spec["fov"] = v
            _set_field(bundle, "fov", v, source=FieldSource.REGEX, confidence=0.95, note="Parsed FOV from user text.")

    m = _RE_FNUM.search(text)
    if m:
        v = _to_float(m.group(1))
        if v is not None:
            bundle.design_spec["fnum"] = v
            _set_field(bundle, "fnum", v, source=FieldSource.REGEX, confidence=0.95, note="Parsed F-number from user text.")

    m = _RE_FOCAL.search(text)
    if m:
        v = _to_float(m.group(1))
        if v is not None:
            bundle.design_spec["foclen"] = v
            _set_field(bundle, "foclen", v, source=FieldSource.REGEX, confidence=0.93, note="Parsed focal length from user text.")

    m = _RE_IMGH.search(text)
    if m:
        v = _to_float(m.group(1))
        if v is not None:
            bundle.design_spec["imgh"] = v
            _set_field(bundle, "imgh", v, source=FieldSource.REGEX, confidence=0.93, note="Parsed image height from user text.")

    m = _RE_WAVELENGTH.search(text)
    if m:
        v = _to_float(m.group(1))
        if v is not None:
            unit = (m.group(2) or "").lower()
            bundle.design_spec["wavelength"] = {"value": v, "unit": unit or "unspecified"}
            _set_field(bundle, "wavelength", bundle.design_spec["wavelength"], source=FieldSource.REGEX, confidence=0.90, note="Parsed wavelength from user text.")

    m = _RE_APERTURE.search(text)
    if m:
        v = _to_float(m.group(1))
        if v is not None:
            bundle.design_spec["aperture"] = v
            _set_field(bundle, "aperture", v, source=FieldSource.REGEX, confidence=0.88, note="Parsed aperture from user text.")

    if "fov" not in bundle.design_spec:
        bundle.missing_fields.append("fov")
    if "fnum" not in bundle.design_spec:
        bundle.missing_fields.append("fnum")
    if "foclen" not in bundle.design_spec and "imgh" not in bundle.design_spec:
        bundle.missing_fields.append("foclen_or_imgh")

    if bundle.design_spec:
        bundle.notes.append("Deterministic design field extraction succeeded for at least one field.")
    else:
        bundle.notes.append("No deterministic design fields extracted from text.")

    bundle.missing_fields = _dedupe_missing(bundle.missing_fields)
    return bundle


# ----------------------------
# analysis extraction
# ----------------------------

def extract_analysis_fields(envelope: TurnEnvelope) -> ExtractionBundle:
    text = envelope.cleaned_message
    text_lower = text.lower()
    bundle = ExtractionBundle()

    if _RE_MTF.search(text):
        bundle.analysis_request["mode"] = "mtf"
        _set_field(bundle, "analysis_mode", "mtf", source=FieldSource.HEURISTIC, confidence=0.95, note="Detected MTF analysis request.")
    elif _RE_SPOT.search(text):
        bundle.analysis_request["mode"] = "spot"
        _set_field(bundle, "analysis_mode", "spot", source=FieldSource.HEURISTIC, confidence=0.95, note="Detected spot analysis request.")
    elif _RE_RMS.search(text):
        bundle.analysis_request["mode"] = "rms"
        _set_field(bundle, "analysis_mode", "rms", source=FieldSource.HEURISTIC, confidence=0.92, note="Detected RMS analysis request.")

    lens_att = _first_lens_attachment(envelope)
    if envelope.active_source_ref:
        bundle.analysis_request["source_ref"] = dict(envelope.active_source_ref)
        _set_field(bundle, "lens_source", bundle.analysis_request["source_ref"], source=FieldSource.STATE, confidence=0.78, inferred=True, note="Using active source ref from state.")
    elif lens_att:
        bundle.analysis_request["attachment"] = lens_att
        _set_field(bundle, "lens_source", lens_att, source=FieldSource.ATTACHMENT, confidence=0.92, inferred=True, note="Using uploaded lens-like attachment.")
    else:
        bundle.missing_fields.append("lens_source")

    if "mode" not in bundle.analysis_request:
        if "analy" in text_lower or "analysis" in text_lower:
            bundle.notes.append("Generic analysis intent detected but no specific analysis mode parsed.")

    if bundle.analysis_request:
        bundle.notes.append("Deterministic analysis field extraction succeeded for at least one field.")
    else:
        bundle.notes.append("No deterministic analysis fields extracted from text.")

    bundle.missing_fields = _dedupe_missing(bundle.missing_fields)
    return bundle


# ----------------------------
# merge helper
# ----------------------------

def merge_extraction_bundle(target: ExtractionBundle, incoming: ExtractionBundle) -> ExtractionBundle:
    out = ExtractionBundle(
        field_map=dict(target.field_map),
        design_spec=dict(target.design_spec),
        analysis_request=dict(target.analysis_request),
        run_request=dict(target.run_request),
        delivery_request=dict(target.delivery_request),
        missing_fields=list(target.missing_fields),
        notes=list(target.notes),
    )

    for k, v in incoming.field_map.items():
        if k not in out.field_map:
            out.field_map[k] = v

    out.design_spec.update(incoming.design_spec)
    out.analysis_request.update(incoming.analysis_request)
    out.run_request.update(incoming.run_request)
    out.delivery_request.update(incoming.delivery_request)
    out.missing_fields = _dedupe_missing(out.missing_fields + incoming.missing_fields)
    out.notes.extend(incoming.notes)
    return out


def missing_design_fields(spec: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if spec.get("fov") is None:
        missing.append("fov")
    if spec.get("fnum") is None:
        missing.append("fnum")
    if spec.get("foclen") is None and spec.get("imgh") is None:
        missing.append("foclen_or_imgh")
    return missing
