"""Consolidated text extraction helpers for design specs, analysis requests, and run params.

Merges overlapping logic previously duplicated across router.py and tool_arg_refine.py.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any


def _extract_number(text: str, patterns: list[str]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except Exception:
                continue
    return None


def extract_design_spec(text: str) -> dict[str, Any]:
    """Extract design specification parameters from free-form text.

    Merges regex coverage from router and tool_arg_refine extractors.
    """
    msg = text or ""
    lowered = msg.lower()
    spec: dict[str, Any] = {}

    # FOV
    fov = _extract_number(msg, [
        r"fov\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",
        r"([0-9]+(?:\.[0-9]+)?)\s*deg(?:ree)?s?\s*fov",
    ])
    if fov is not None:
        spec["fov"] = fov

    # F-number
    fnum = _extract_number(msg, [
        r"f/?#?\s*([0-9]+(?:\.[0-9]+)?)",
        r"fnum\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",
        r"f\s*/\s*(\d+(?:\.\d+)?)",
    ])
    if fnum is not None:
        spec["fnum"] = fnum

    # Focal length
    foclen = _extract_number(msg, [
        r"foc(?:al)?(?:len| length)?\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",
        r"([0-9]+(?:\.[0-9]+)?)\s*mm\s*focal",
        r"(\d+(?:\.\d+)?)\s*mm",
    ])
    if foclen is not None:
        spec["foclen"] = foclen

    # Image height
    imgh = _extract_number(msg, [
        r"imgh\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",
        r"image height\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",
    ])
    if imgh is not None:
        spec["imgh"] = imgh

    # BFL
    bfl = _extract_number(msg, [r"bfl\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)"])
    if bfl is not None:
        spec["bfl"] = bfl

    # Thickness
    thickness = _extract_number(msg, [r"thickness\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)"])
    if thickness is not None:
        spec["thickness"] = thickness

    # Sensor dimensions (from tool_arg_refine)
    sensor_match = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*mm", msg, re.IGNORECASE)
    if sensor_match:
        width = float(sensor_match.group(1))
        height = float(sensor_match.group(2))
        spec["sensor_width_mm"] = width
        spec["sensor_height_mm"] = height
        spec.setdefault("imgh", max(width, height) / 2.0)
        if spec.get("foclen") is not None and spec.get("fov") is None:
            spec["fov"] = round(2.0 * 180.0 / math.pi * math.atan((width / 2.0) / spec["foclen"]), 3)

    # Wavelengths (from tool_arg_refine)
    wavelengths = re.findall(r"(\d+(?:\.\d+)?)\s*nm", msg, re.IGNORECASE)
    if wavelengths:
        spec["wvlns"] = [float(w) for w in wavelengths]

    # Lens class
    if "camera" in lowered:
        spec["lens_class"] = "camera"
    if "cellphone" in lowered or "mobile" in lowered:
        spec["lens_class"] = "cellphone"

    # Exclusions
    if "not " in lowered or "don't" in lowered:
        exclusions = re.findall(r"(?:not|don't)\s+([a-zA-Z_ ]+)", lowered)
        if exclusions:
            spec["excluded_objectives"] = [x.strip() for x in exclusions[:5]]

    # Save name (from tool_arg_refine)
    save_match = re.search(r"\b(?:as|named?)\s+([A-Za-z0-9_.-]+)", msg)
    if save_match and ("json" in lowered or "zmx" in lowered):
        spec["save_name"] = save_match.group(1).removesuffix(".json").removesuffix(".zmx")

    # Sensible defaults
    if any(phrase in lowered for phrase in (
        "use defaults", "use default", "sensible defaults",
        "sensible default", "default config",
    )):
        spec.setdefault("fov", 40.0)
        spec.setdefault("fnum", 2.8)
        spec.setdefault("foclen", 35.0)
        spec.setdefault("wvlns", [486.0, 588.0, 656.0])
        spec.setdefault("sensor_width_mm", 36.0)
        spec.setdefault("sensor_height_mm", 24.0)
        spec.setdefault("imgh", 18.0)
        spec.setdefault("save_name", "deeplens_design")

    return spec


def extract_analysis_request(message: str, attachments: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract analysis mode and source refs from message text."""
    msg = (message or "").lower()
    mode = "full"
    if "spot" in msg:
        mode = "spot"
    elif "mtf" in msg:
        mode = "mtf"
    elif "rms" in msg:
        mode = "rms"
    source_refs = []
    for attachment in attachments:
        name = attachment.get("name") or attachment.get("filename") or attachment.get("uri")
        source_refs.append({"name": name, "artifact_id": attachment.get("artifact_id"), "uri": attachment.get("uri")})
    return {"mode": mode, "source_refs": source_refs}


def extract_run_request(message: str) -> dict[str, Any]:
    """Extract optimization run parameters from message text."""
    msg = message or ""
    iterations = _extract_number(msg, [r"iterations?\s*[:=]?\s*([0-9]+)", r"for\s*([0-9]+)\s*iters"])
    checkpoint_every = _extract_number(msg, [r"checkpoint(?: every)?\s*[:=]?\s*([0-9]+)", r"every\s*([0-9]+)\s*iterations"])
    request: dict[str, Any] = {}
    if iterations is not None:
        request["iterations"] = int(iterations)
    if checkpoint_every is not None:
        request["checkpoint_every"] = int(checkpoint_every)
    if "stub" in msg.lower():
        request["use_stub"] = True
    return request


def missing_design_fields(spec: dict[str, Any]) -> list[str]:
    """Check which required design fields are absent from the spec."""
    missing: list[str] = []
    if spec.get("fov") is None:
        missing.append("fov")
    if spec.get("fnum") is None:
        missing.append("fnum")
    if spec.get("foclen") is None and spec.get("imgh") is None:
        missing.append("foclen_or_imgh")
    return missing


def infer_analysis_mode(text: str, current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Infer analysis request from text, optionally merging with existing request."""
    analysis_request = dict(current or {})
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


def infer_export_formats(text: str, current: list[str] | None = None) -> list[str]:
    """Infer export formats from text."""
    formats = list(current or [])
    lowered = text.lower()
    if "json" in lowered and "json" not in formats:
        formats.append("json")
    if "zmx" in lowered and "zmx" not in formats:
        formats.append("zmx")
    if not formats:
        formats = ["json", "zmx"]
    return formats


def attachment_suggests_lens(attachments: list[dict[str, Any]]) -> bool:
    """Check if any attachment looks like a lens file."""
    for attachment in attachments:
        name = attachment.get("name") or attachment.get("filename") or attachment.get("uri") or ""
        if Path(str(name)).suffix.lower() in {".json", ".zmx"}:
            return True
    return False
