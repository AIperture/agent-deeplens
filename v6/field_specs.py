from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    ask_label: str
    ask_fragment: str


FIELD_SPECS: dict[str, FieldSpec] = {
    "fov": FieldSpec("fov", "field of view", "field of view (`fov`)"),
    "fnum": FieldSpec("fnum", "F-number", "F-number (`fnum`)"),
    "foclen_or_imgh": FieldSpec(
        "foclen_or_imgh",
        "focal length or image height",
        "target focal length (`foclen`) or image height / sensor format (`imgh`)",
    ),
    "lens_source": FieldSpec("lens_source", "lens source", "a `.json` or `.zmx` lens file"),
    "run_id": FieldSpec("run_id", "run id", "background run id (`run_id`)"),
    "analysis_mode": FieldSpec("analysis_mode", "analysis mode", "analysis mode (`full`, `spot`, `mtf`, or `rms`)"),
    "export_formats": FieldSpec("export_formats", "export formats", "export formats such as `json` or `zmx`"),
}


CAPABILITY_REQUIRED_FIELDS: dict[str, list[str]] = {
    "design": ["fov", "fnum", "foclen_or_imgh"],
    "analysis": ["lens_source"],
    "optimization": ["lens_source"],
    "export": ["lens_source", "export_formats"],
    "run_control": ["run_id"],
}


def field_prompt_fragment(field_name: str) -> str:
    spec = FIELD_SPECS.get(field_name)
    return spec.ask_fragment if spec is not None else field_name

