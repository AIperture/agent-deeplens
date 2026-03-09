from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .types import ToolCategory, ToolExecutionStyle


ToolExecutor = Callable[..., Awaitable[dict[str, Any]]]


@dataclass
class ToolSpec:
    name: str
    category: ToolCategory
    execution_style: ToolExecutionStyle

    executor_key: str
    description: str = ""

    require_approval: bool = False
    mutates_state: bool = False
    writes_artifacts: bool = False
    long_running: bool = False

    argument_hints: dict[str, Any] = field(default_factory=dict)
    timeout_s: float | None = None


TOOL_REGISTRY: dict[str, ToolSpec] = {
    # ---------- AG tools ----------
    "save_artifact": ToolSpec(
        name="save_artifact",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="ag.save_artifact",
        writes_artifacts=True,
        mutates_state=True,
        description="Persist an artifact or rendered output.",
    ),
    "spawn_long_run": ToolSpec(
        name="spawn_long_run",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.SPAWN,
        executor_key="ag.spawn_long_run",
        require_approval=True,
        long_running=True,
        description="Spawn a long-running child workflow and return immediately.",
    ),
    "wait_on_run_short": ToolSpec(
        name="wait_on_run_short",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.SPAWN_AND_WAIT_SHORT,
        executor_key="ag.wait_on_run_short",
        timeout_s=30,
        description="Wait briefly on a known child run.",
    ),
    "cancel_run": ToolSpec(
        name="cancel_run",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="ag.cancel_run",
        mutates_state=True,
        description="Best-effort cancel a spawned run.",
    ),

    # ---------- DeepLens tools ----------
    "read_attachment": ToolSpec(
        name="read_attachment",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.read_attachment",
        description="Read and inspect an attached lens/config/input.",
    ),
    "run_analysis": ToolSpec(
        name="run_analysis",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.run_analysis",
        description="Run a standard DeepLens analysis routine.",
    ),
    "run_targeted_eval": ToolSpec(
        name="run_targeted_eval",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.run_targeted_eval",
        description="Run targeted evaluation such as MTF/PSF/spot.",
    ),
    "propose_initial_design": ToolSpec(
        name="propose_initial_design",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.propose_initial_design",
        description="Generate a first-pass design proposal.",
    ),
    "generate_lens_config": ToolSpec(
        name="generate_lens_config",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.generate_lens_config",
        writes_artifacts=True,
        mutates_state=True,
        description="Create a concrete lens config artifact.",
    ),
    "run_optimization_stage": ToolSpec(
        name="run_optimization_stage",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.SPAWN,
        executor_key="dl.run_optimization_stage",
        require_approval=True,
        long_running=True,
        mutates_state=True,
        writes_artifacts=True,
        description="Launch an optimization stage, often long-running.",
    ),
    "inspect_failure": ToolSpec(
        name="inspect_failure",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.inspect_failure",
        description="Inspect failure symptoms and collect diagnostic clues.",
    ),
    "run_diagnostic": ToolSpec(
        name="run_diagnostic",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.run_diagnostic",
        description="Run a structured DeepLens diagnostic routine.",
    ),
    "apply_fix_patch": ToolSpec(
        name="apply_fix_patch",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.apply_fix_patch",
        require_approval=True,
        mutates_state=True,
        writes_artifacts=True,
        description="Apply a proposed config/parameter fix.",
    ),
    "render_plot": ToolSpec(
        name="render_plot",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.render_plot",
        writes_artifacts=True,
        description="Render a figure or plot artifact.",
    ),
}


def get_tool_spec(name: str) -> ToolSpec:
    if name not in TOOL_REGISTRY:
        raise KeyError(f"Unknown tool action: {name}")
    return TOOL_REGISTRY[name]