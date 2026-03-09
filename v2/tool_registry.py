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
    argument_hints: dict[str, Any] = field(default_factory=dict)


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "ag.search": ToolSpec(
        name="ag.search",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="ag.search",
        description="Search scoped memory or index information.",
    ),
    "ag.send_image": ToolSpec(
        name="ag.send_image",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="ag.send_image",
        description="Push an image artifact reference to the UI.",
    ),
    "ag.send_file": ToolSpec(
        name="ag.send_file",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="ag.send_file",
        description="Push a file artifact reference to the UI.",
    ),
    "ag.spawn_graph": ToolSpec(
        name="ag.spawn_graph",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.SPAWN,
        approval_level=ApprovalLevel.HARD,
        executor_key="ag.spawn_graph",
        description="Submit a graph run in the background.",
        long_running=True,
    ),
    "ag.wait_run_short": ToolSpec(
        name="ag.wait_run_short",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.SPAWN_AND_WAIT_SHORT,
        executor_key="ag.wait_run_short",
        description="Wait briefly on a known run.",
    ),
    "ag.cancel_run": ToolSpec(
        name="ag.cancel_run",
        category=ToolCategory.AG,
        execution_style=ToolExecutionStyle.INLINE,
        approval_level=ApprovalLevel.SOFT,
        executor_key="ag.cancel_run",
        description="Best-effort cancel a known run.",
    ),
    "dl.inspect_input": ToolSpec(
        name="dl.inspect_input",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.inspect_input",
        description="Inspect a provided attachment or input.",
    ),
    "dl.simulate_standard": ToolSpec(
        name="dl.simulate_standard",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.INLINE,
        executor_key="dl.simulate_standard",
        description="Run the standard simulation or analysis path.",
    ),
    "dl.submit_optimization": ToolSpec(
        name="dl.submit_optimization",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.SPAWN,
        approval_level=ApprovalLevel.HARD,
        executor_key="dl.submit_optimization",
        description="Submit a graph-backed optimization run.",
        long_running=True,
    ),
    "dl.check_run_status": ToolSpec(
        name="dl.check_run_status",
        category=ToolCategory.DEEPLENS,
        execution_style=ToolExecutionStyle.SPAWN_AND_WAIT_SHORT,
        executor_key="dl.check_run_status",
        description="Check a known run and summarize its current status.",
    ),
}


def get_tool_spec(name: str) -> ToolSpec:
    if name not in TOOL_REGISTRY:
        raise KeyError(f"Unknown tool: {name}")
    return TOOL_REGISTRY[name]
