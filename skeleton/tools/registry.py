"""Tool registry with explicit parameter specs (defaults, required/optional, types).

This replaces v6's unstructured `argument_hints` with a structured `ToolParam` system
that drives automatic arg resolution, missing-field detection, and default filling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..types import ApprovalLevel


@dataclass
class ToolParam:
    """Declares one parameter of a tool.

    Attributes:
        name:         Parameter name (must match the key in resolved_inputs).
        type:         Expected Python type as a string: "str", "int", "float", "bool", "dict", "list".
        required:     If True, the arg resolver will block execution when the value is missing.
        default:      Default value used when the param is not provided and not required.
        description:  Human-readable description (shown to user when prompting for missing fields).
        source_paths: Ordered list of dotted paths to search for a value. The arg resolver
                      walks these in order; the first non-None hit wins.
                      Examples: "task.parsed_args.query", "state.domain_state.last_result".
    """
    name: str
    type: str = "str"
    required: bool = False
    default: Any = None
    description: str = ""
    source_paths: list[str] = field(default_factory=list)


@dataclass
class ToolSpec:
    """Specification for a registered tool.

    Attributes:
        name:           Unique tool identifier (e.g. "calculate").
        description:    Human-readable description of what the tool does.
        executor_key:   Key into the executor map that dispatches to the implementation.
        parameters:     Declared parameters with defaults, types, and source hints.
        approval_level: Whether user approval is needed before execution.
        refinable:      Whether LLM refinement is available for ambiguous inputs.
    """
    name: str
    description: str
    executor_key: str
    parameters: dict[str, ToolParam] = field(default_factory=dict)
    approval_level: ApprovalLevel = ApprovalLevel.NONE
    refinable: bool = False


# ---------------------------------------------------------------------------
# Built-in tool registry (skeleton demo tools)
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, ToolSpec] = {
    "calculate": ToolSpec(
        name="calculate",
        description="Perform arithmetic on two numbers.",
        executor_key="calculate",
        parameters={
            "a": ToolParam(
                name="a",
                type="float",
                required=True,
                description="First operand (number).",
                source_paths=["task.parsed_args.a"],
            ),
            "b": ToolParam(
                name="b",
                type="float",
                required=True,
                description="Second operand (number).",
                source_paths=["task.parsed_args.b"],
            ),
            "operation": ToolParam(
                name="operation",
                type="str",
                required=False,
                default="add",
                description="Arithmetic operation: add, subtract, multiply, divide.",
                source_paths=["task.parsed_args.operation"],
            ),
        },
    ),
    "search_knowledge": ToolSpec(
        name="search_knowledge",
        description="Search a knowledge base for information on a topic.",
        executor_key="search_knowledge",
        parameters={
            "query": ToolParam(
                name="query",
                type="str",
                required=True,
                description="The search query or topic to look up.",
                source_paths=["task.parsed_args.query", "task.user_goal"],
            ),
            "max_results": ToolParam(
                name="max_results",
                type="int",
                required=False,
                default=5,
                description="Maximum number of results to return.",
                source_paths=["task.parsed_args.max_results"],
            ),
        },
    ),
    "generate_report": ToolSpec(
        name="generate_report",
        description="Compile previous results into a summary report.",
        executor_key="generate_report",
        parameters={
            "title": ToolParam(
                name="title",
                type="str",
                required=False,
                default="Summary Report",
                description="Title for the report.",
                source_paths=["task.parsed_args.title"],
            ),
            "include_sources": ToolParam(
                name="include_sources",
                type="bool",
                required=False,
                default=True,
                description="Whether to include source references in the report.",
                source_paths=["task.parsed_args.include_sources"],
            ),
        },
    ),
}


def get_tool_spec(name: str) -> ToolSpec:
    """Return the spec for a tool, or a fallback spec if not found."""
    if name in TOOL_REGISTRY:
        return TOOL_REGISTRY[name]
    return ToolSpec(
        name=name,
        description=f"Unknown tool: {name}",
        executor_key=name,
    )


def list_tool_names() -> list[str]:
    """Return all registered tool names."""
    return list(TOOL_REGISTRY.keys())


def get_allowed_tools() -> list[str]:
    """Return tool names available for LLM planning context."""
    return list(TOOL_REGISTRY.keys())
