from __future__ import annotations

from .types import ArtifactDeliveryMode, ToolSpec


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "ag.get_latest_uploads": ToolSpec(
        name="ag.get_latest_uploads",
        executor_key="ag.get_latest_uploads",
        required_args=[],
        description="Inspect the latest uploaded files available in the session.",
    ),
    "ag.load_artifact_text_or_json": ToolSpec(
        name="ag.load_artifact_text_or_json",
        executor_key="ag.load_artifact_text_or_json",
        required_args=[],
        optional_args=["artifact_id", "uri"],
        description="Load a text or JSON artifact by id or uri.",
    ),
    "ag.save_text_artifact": ToolSpec(
        name="ag.save_text_artifact",
        executor_key="ag.save_text_artifact",
        required_args=["payload"],
        optional_args=["name"],
        description="Persist a text payload as an artifact.",
    ),
    "ag.save_json_artifact": ToolSpec(
        name="ag.save_json_artifact",
        executor_key="ag.save_json_artifact",
        required_args=["payload"],
        optional_args=["name"],
        description="Persist a JSON payload as an artifact.",
    ),
    "ag.send_image": ToolSpec(
        name="ag.send_image",
        executor_key="ag.send_image",
        required_args=[],
        optional_args=["url", "title", "name", "artifact_selector"],
        required_input_paths=["url"],
        plan_arg_paths=["artifact_selector", "url", "title", "name"],
        artifact_selector_kind="image",
        description="Send an image artifact or local file to the UI.",
    ),
    "ag.send_file": ToolSpec(
        name="ag.send_file",
        executor_key="ag.send_file",
        required_args=[],
        optional_args=["url", "filename", "title", "artifact_selector"],
        required_input_paths=["url"],
        plan_arg_paths=["artifact_selector", "url", "filename", "title"],
        artifact_selector_kind="file",
        description="Send a file artifact or local file to the UI.",
    ),
    "dl.optimize": ToolSpec(
        name="dl.optimize",
        executor_key="dl.optimize",
        required_args=[],
        optional_args=["graph_id", "lens_source", "run_request", "use_stub"],
        defaults={
            "graph_id": "deeplens_v7_optimize_launcher",
            "run_request": {
                "goal": "",
                "constraints": [],
                "excluded_objectives": [],
                "iterations": 500,
                "checkpoint_every": 100,
                "export_formats": ["json", "zmx"],
            },
        },
        required_input_paths=["lens_source"],
        plan_arg_paths=["lens_source", "run_request", "use_stub"],
        tool_policy_id="approval_required",
        interaction_policy_id="approval",
        description="Submit a child graph run in the background.",
    ),
    "ag.status": ToolSpec(
        name="ag.status",
        executor_key="ag.status",
        required_args=[],
        optional_args=["timeout_s"],
        defaults={"timeout_s": 1},
        required_input_paths=["run_id"],
        plan_arg_paths=["run_id", "timeout_s"],
        description="Check a child run and summarize its current status.",
    ),
    "ag.cancel": ToolSpec(
        name="ag.cancel",
        executor_key="ag.cancel",
        required_args=[],
        required_input_paths=["run_id"],
        plan_arg_paths=["run_id"],
        tool_policy_id="soft_approval",
        interaction_policy_id="approval",
        description="Best-effort cancel a child run.",
    ),
    "dl.load_lens": ToolSpec(
        name="dl.load_lens",
        executor_key="dl.load_lens",
        required_args=[],
        required_input_paths=["lens_source"],
        plan_arg_paths=["lens_source"],
        description="Resolve a lens file or artifact and make it the active lens.",
    ),
    "dl.analysis": ToolSpec(
        name="dl.analysis",
        executor_key="dl.analysis",
        required_args=[],
        optional_args=["lens_source", "analysis_request", "design_spec", "use_stub"],
        defaults={"analysis_request": {"mode": "full"}},
        required_input_paths=["lens_source"],
        plan_arg_paths=["lens_source", "analysis_request", "use_stub"],
        artifact_output_kinds=["file"],
        artifact_delivery_mode=ArtifactDeliveryMode.ON_REQUEST.value,
        description="Run DeepLens analysis and return summary plus generated files.",
    ),
    "dl.create_lens": ToolSpec(
        name="dl.create_lens",
        executor_key="dl.create_lens",
        required_args=[],
        optional_args=["analysis_request", "formats", "use_stub"],
        defaults={
            "design_spec": {
                "bfl": 3.0,
                "foclen": 35.0,
                "save_name": "deeplens_design",
                "surf_list": [["Aspheric", "Aspheric"], ["Aperture"], ["Aspheric", "Aspheric"], ["Aspheric", "Aspheric"]],
            },
            "formats": ["json", "zmx"],
        },
        required_input_paths=["design_spec.fov", "design_spec.fnum"],
        plan_arg_paths=["design_spec", "formats", "use_stub"],
        artifact_output_kinds=["file"],
        artifact_delivery_mode=ArtifactDeliveryMode.ON_REQUEST.value,
        description="Create a starting lens from design specs.",
    ),
    "dl.export_lens": ToolSpec(
        name="dl.export_lens",
        executor_key="dl.export_lens",
        required_args=[],
        optional_args=["lens_source", "formats", "use_stub"],
        defaults={"formats": ["json", "zmx"]},
        required_input_paths=["lens_source"],
        plan_arg_paths=["lens_source", "formats", "use_stub"],
        artifact_output_kinds=["file"],
        artifact_delivery_mode=ArtifactDeliveryMode.ON_REQUEST.value,
        description="Export the active or provided lens to JSON and/or ZMX.",
    ),
}


def get_tool_spec(name: str) -> ToolSpec:
    if name not in TOOL_REGISTRY:
        raise KeyError(f"Unknown tool: {name}")
    return TOOL_REGISTRY[name]
