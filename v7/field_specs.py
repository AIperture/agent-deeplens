from __future__ import annotations

from collections import OrderedDict
from typing import Any

from .tool_registry import TOOL_REGISTRY
from .types import FieldSpec, MissingFieldInfo


FIELD_SPECS: "OrderedDict[str, FieldSpec]" = OrderedDict(
    (
        spec.path,
        spec,
    )
    for spec in [
        FieldSpec(
            path="lens_source",
            bucket="lens_source",
            label="a lens file or active lens",
            help_text="Upload a `.json` or `.zmx` lens file, or tell me to use the active lens.",
            examples=["Use the uploaded lens.", "Use the active lens."],
            value_type="object",
            extract_from_text=True,
            extract_from_attachments=True,
            extract_from_state=True,
            tool_names=["dl.load_lens", "dl.analysis", "dl.optimize", "dl.export_lens"],
        ),
        FieldSpec(
            path="design_spec.fov",
            bucket="design_spec",
            label="field of view (`fov`)",
            help_text="Provide the field of view in degrees.",
            examples=["40 deg FOV", "fov 55"],
            value_type="number",
            tool_names=["dl.create_lens"],
        ),
        FieldSpec(
            path="design_spec.fnum",
            bucket="design_spec",
            label="f-number (`fnum`)",
            help_text="Provide the lens f-number.",
            examples=["f/2.8", "fnum 4.0"],
            value_type="number",
            tool_names=["dl.create_lens"],
        ),
        FieldSpec(
            path="design_spec.foclen",
            bucket="design_spec",
            label="focal length (`foclen`)",
            help_text="Provide focal length in millimeters.",
            examples=["35 mm focal length", "foclen 24"],
            value_type="number",
            tool_names=["dl.create_lens"],
            has_default=True,
            default_value=35.0,
        ),
        FieldSpec(
            path="design_spec.imgh",
            bucket="design_spec",
            label="image height (`imgh`)",
            help_text="Provide image height if focal length is not specified.",
            examples=["imgh 18", "image height 12"],
            value_type="number",
            tool_names=["dl.create_lens"],
        ),
        FieldSpec(
            path="design_spec.bfl",
            bucket="design_spec",
            label="back focal length (`bfl`)",
            help_text="Provide back focal length in millimeters.",
            examples=["bfl 3.0"],
            value_type="number",
            tool_names=["dl.create_lens"],
            has_default=True,
            default_value=3.0,
        ),
        FieldSpec(
            path="design_spec.thickness",
            bucket="design_spec",
            label="thickness",
            help_text="Provide lens thickness if needed.",
            examples=["thickness 1.2"],
            value_type="number",
            tool_names=["dl.create_lens"],
        ),
        FieldSpec(
            path="design_spec.save_name",
            bucket="design_spec",
            label="output name",
            help_text="Provide a base filename for created artifacts.",
            examples=["named mobile_v1", "save as deeplens_design"],
            value_type="string",
            tool_names=["dl.create_lens"],
            has_default=True,
            default_value="deeplens_design",
        ),
        FieldSpec(
            path="design_spec.surf_list",
            bucket="design_spec",
            label="surface layout (`surf_list`)",
            help_text="Provide either the explicit surface list or a shorthand lens-layout description.",
            examples=[
                'surf_list [["Aspheric", "Aspheric"], ["Aperture"], ["Aspheric", "Aspheric"]]',
                "lens set with four lenses, all with aspherical surfaces",
            ],
            value_type="array",
            tool_names=["dl.create_lens"],
            has_default=True,
            default_value=[["Aspheric", "Aspheric"], ["Aperture"], ["Aspheric", "Aspheric"], ["Aspheric", "Aspheric"]],
        ),
        FieldSpec(
            path="analysis_request.mode",
            bucket="analysis_request",
            label="analysis mode",
            help_text="Choose a supported analysis mode.",
            examples=["run MTF", "spot analysis", "RMS analysis"],
            value_type="string",
            enum_values=["full", "spot", "mtf", "rms"],
            tool_names=["dl.analysis"],
            has_default=True,
            default_value="full",
        ),
        FieldSpec(
            path="run_request.goal",
            bucket="run_request",
            label="optimization goal",
            help_text="Describe what the optimization should improve.",
            examples=["optimize for sharpness", "improve edge MTF"],
            value_type="string",
            tool_names=["dl.optimize"],
        ),
        FieldSpec(
            path="run_request.constraints",
            bucket="run_request",
            label="optimization constraints",
            help_text="Provide constraints the optimizer should respect.",
            examples=["keep compact packaging", "constraint: maintain F/2.8"],
            value_type="array",
            tool_names=["dl.optimize"],
        ),
        FieldSpec(
            path="run_request.excluded_objectives",
            bucket="run_request",
            label="excluded optimization objectives",
            help_text="List objectives the optimizer should avoid changing.",
            examples=["do not optimize distortion"],
            value_type="array",
            tool_names=["dl.optimize"],
        ),
        FieldSpec(
            path="run_request.iterations",
            bucket="run_request",
            label="iteration count",
            help_text="Provide the number of optimization iterations.",
            examples=["500 iterations", "for 1000 iters"],
            value_type="integer",
            tool_names=["dl.optimize"],
            has_default=True,
            default_value=500,
        ),
        FieldSpec(
            path="run_request.checkpoint_every",
            bucket="run_request",
            label="checkpoint interval",
            help_text="Provide the checkpoint frequency in iterations.",
            examples=["checkpoint every 100", "every 50 iterations"],
            value_type="integer",
            tool_names=["dl.optimize"],
            has_default=True,
            default_value=100,
        ),
        FieldSpec(
            path="run_request.export_formats",
            bucket="run_request",
            label="optimization export formats",
            help_text="Choose which output formats to save from optimization.",
            examples=["save JSON and ZMX"],
            value_type="array",
            tool_names=["dl.optimize"],
            has_default=True,
            default_value=["json", "zmx"],
        ),
        FieldSpec(
            path="run_request.use_stub",
            bucket="run_request",
            label="stub mode",
            help_text="Say `stub` to run the optimization in stub mode.",
            examples=["use stub mode"],
            value_type="boolean",
            tool_names=["dl.optimize"],
        ),
        FieldSpec(
            path="run_request.run_id",
            bucket="run_request",
            label="run id",
            help_text="Provide the run id to check or cancel a background run.",
            examples=["run id abc123"],
            value_type="string",
            tool_names=["ag.status", "ag.cancel"],
            extract_from_state=True,
        ),
        FieldSpec(
            path="delivery_request.formats",
            bucket="delivery_request",
            label="delivery formats",
            help_text="Choose which artifact formats you want sent or exported.",
            examples=["send JSON", "download ZMX"],
            value_type="array",
            tool_names=["dl.create_lens", "dl.export_lens"],
        ),
    ]
)


def get_field_spec(path: str) -> FieldSpec | None:
    return FIELD_SPECS.get(path)


def field_specs_for_tool(tool_name: str) -> list[FieldSpec]:
    return [spec for spec in FIELD_SPECS.values() if tool_name in spec.tool_names]


def required_field_specs_for_tool(tool_name: str) -> list[FieldSpec]:
    tool_spec = TOOL_REGISTRY[tool_name]
    specs: list[FieldSpec] = []
    for path in tool_spec.required_input_paths:
        field_spec = get_field_spec(path)
        if field_spec is not None:
            specs.append(field_spec)
            continue
        specs.append(
            FieldSpec(
                path=path,
                bucket=path.split(".", 1)[0],
                label=path,
                tool_names=[tool_name],
            )
        )
    return specs


def describe_missing_fields(paths: list[str]) -> list[MissingFieldInfo]:
    details: list[MissingFieldInfo] = []
    for path in paths:
        spec = get_field_spec(path)
        if spec is None:
            details.append(MissingFieldInfo(path=path, label=path))
            continue
        details.append(
            MissingFieldInfo(
                path=path,
                label=spec.label,
                help_text=spec.help_text,
                examples=list(spec.examples),
            )
        )
    return details


def render_parse_contract() -> str:
    lines = [
        "Parse contract:",
        "- Only extract fields that are clearly provided by the user, attachments, or active state references.",
        "- Do not invent artifact ids, run ids, filenames, or unsupported fields.",
        "- Leave optional fields unset if the user did not specify them unless the user explicitly asked for defaults.",
        "- Downstream binding applies ToolSpec defaults; the parser should focus on explicit user intent.",
        "- Supported canonical fields:",
    ]
    for spec in FIELD_SPECS.values():
        line = f"  - `{spec.path}` ({spec.value_type})"
        if spec.enum_values:
            line += f"; allowed: {', '.join(spec.enum_values)}"
        if spec.tool_names:
            line += f"; tools: {', '.join(spec.tool_names)}"
        if spec.has_default:
            line += f"; downstream default: {spec.default_value!r}"
        if spec.help_text:
            line += f"; {spec.help_text}"
        lines.append(line)
    return "\n".join(lines)


def summarize_required_fields(tool_name: str) -> dict[str, list[str]]:
    required = [spec.path for spec in required_field_specs_for_tool(tool_name)]
    labeled = [spec.label for spec in required_field_specs_for_tool(tool_name)]
    return {"paths": required, "labels": labeled}


def tool_parse_hints() -> dict[str, Any]:
    hints: dict[str, Any] = {}
    for tool_name in TOOL_REGISTRY:
        hints[tool_name] = {
            "required_fields": summarize_required_fields(tool_name),
            "fields": [spec.path for spec in field_specs_for_tool(tool_name)],
        }
    return hints
