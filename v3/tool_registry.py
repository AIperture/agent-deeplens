from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .types import ApprovalLevel, ToolCategory, ToolExecutionStyle


@dataclass
class ToolSpec:
    name: str
    category: ToolCategory
    execution_style: ToolExecutionStyle
    approval_level: ApprovalLevel = ApprovalLevel.NONE
    executor_key: str = ""
    description: str = ""
    long_running: bool = False
    writes_artifacts: bool = False
    requires_lens_input: bool = False
    produces_artifacts: bool = False
    supports_stub: bool = True
    backgroundable: bool = False
    default_delivery: str = "summary"
    argument_hints: dict[str, Any] = field(default_factory=dict)


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "ag.get_latest_uploads": ToolSpec(
        name="ag.get_latest_uploads",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="ag.get_latest_uploads",
        description="Inspect the latest uploaded files available in the session channel.",
        argument_hints={
            "inputs": [],
            "returns": ["uploads", "names"],
        },
    ),
    "ag.load_artifact_text_or_json": ToolSpec(
        name="ag.load_artifact_text_or_json",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="ag.load_artifact_text_or_json",
        description="Load a text or JSON artifact by id or uri.",
        argument_hints={
            "inputs": ["artifact_id or uri"],
            "returns": ["payload"],
        },
    ),
    "ag.save_text_artifact": ToolSpec(
        name="ag.save_text_artifact",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="ag.save_text_artifact",
        description="Persist a text payload as an artifact.",
        writes_artifacts=True,
        produces_artifacts=True,
        argument_hints={
            "inputs": ["payload", "name"],
            "returns": ["artifact_id"],
        },
    ),
    "ag.save_json_artifact": ToolSpec(
        name="ag.save_json_artifact",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="ag.save_json_artifact",
        description="Persist a JSON payload as an artifact.",
        writes_artifacts=True,
        produces_artifacts=True,
        argument_hints={
            "inputs": ["payload", "name"],
            "returns": ["artifact_id"],
        },
    ),
    "ag.send_image": ToolSpec(
        name="ag.send_image",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="ag.send_image",
        description="Send an image artifact or file to the UI.",
        default_delivery="image",
        argument_hints={
            "inputs": ["url or uri", "title"],
        },
    ),
    "ag.send_file": ToolSpec(
        name="ag.send_file",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="ag.send_file",
        description="Send a file artifact or local file to the UI.",
        default_delivery="file",
        argument_hints={
            "inputs": ["url or uri", "filename", "title"],
        },
    ),
    "ag.spawn_graph": ToolSpec(
        name="ag.spawn_graph",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.SPAWN,
        approval_level=ApprovalLevel.HARD,
        executor_key="ag.spawn_graph",
        description="Submit a child graph run in the background.",
        long_running=True,
        backgroundable=True,
        argument_hints={
            "inputs": [
                "graph_id",
                "lens_source",
                "run_request.goal",
                "run_request.constraints",
                "run_request.excluded_objectives",
                "run_request.iterations",
                "run_request.checkpoint_every",
                "run_request.export_formats",
            ],
            "defaults": {
                "graph_id": "deeplens_v3_optimize_workflow",
                "iterations": 500,
                "checkpoint_every": 100,
                "export_formats": ["json", "zmx"],
            },
        },
    ),
    "ag.status": ToolSpec(
        name="ag.status",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.SPAWN_AND_WAIT_SHORT,
        executor_key="ag.status",
        description="Check a child run and summarize its current status.",
        argument_hints={
            "inputs": ["run_id", "timeout_s"],
            "defaults": {"timeout_s": 1},
        },
    ),
    "ag.cancel": ToolSpec(
        name="ag.cancel",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.INLINE,
        approval_level=ApprovalLevel.SOFT,
        executor_key="ag.cancel",
        description="Best-effort cancel a child run.",
        argument_hints={
            "inputs": ["run_id"],
        },
    ),
    "dl.load_lens": ToolSpec(
        name="dl.load_lens",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.load_lens",
        description="Resolve a lens file or artifact and make it the active lens.",
        requires_lens_input=True,
        argument_hints={
            "inputs": ["lens_source"],
        },
    ),
    "dl.analysis": ToolSpec(
        name="dl.analysis",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.analysis",
        description="Run a workflow-level DeepLens analysis and return summary plus files.",
        writes_artifacts=True,
        requires_lens_input=True,
        produces_artifacts=True,
        default_delivery="summary_plus_files",
        argument_hints={
            "inputs": ["lens_source", "analysis_request.mode", "delivery_request.mode", "use_stub"],
            "defaults": {
                "analysis_request.mode": "full",
                "delivery_request.mode": "summary_plus_files",
            },
            "allowed_analysis_modes": ["full", "spot", "mtf", "rms"],
        },
    ),
    "dl.create_lens": ToolSpec(
        name="dl.create_lens",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.create_lens",
        description="Create a starting lens from design specs and optionally baseline-analyze it.",
        writes_artifacts=True,
        produces_artifacts=True,
        default_delivery="summary_plus_files",
        argument_hints={
            "inputs": [
                "design_spec.fov",
                "design_spec.fnum",
                "design_spec.foclen or design_spec.imgh",
                "design_spec.bfl",
                "design_spec.thickness",
                "design_spec.surf_list",
                "design_spec.save_name",
                "analysis_request.mode",
                "formats",
            ],
            "required": ["design_spec.fov", "design_spec.fnum", "design_spec.foclen or design_spec.imgh"],
            "defaults": {
                "analysis_request.mode": "full",
                "formats": ["json", "zmx"],
                "design_spec.save_name": "deeplens_design",
                "design_spec.baseline_analysis": True,
            },
        },
    ),
    "dl.export_lens": ToolSpec(
        name="dl.export_lens",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.export_lens",
        description="Export the active or provided lens to JSON and/or ZMX.",
        writes_artifacts=True,
        requires_lens_input=True,
        produces_artifacts=True,
        default_delivery="summary_plus_files",
        argument_hints={
            "inputs": ["lens_source", "formats"],
            "defaults": {"formats": ["json", "zmx"]},
        },
    ),
}


def get_tool_spec(name: str) -> ToolSpec:
    if name not in TOOL_REGISTRY:
        raise KeyError(f"Unknown tool: {name}")
    return TOOL_REGISTRY[name]
