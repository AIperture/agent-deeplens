from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..types import ApprovalLevel


@dataclass
class ToolParam:
    name: str
    type: str = "str"
    required: bool = False
    description: str = ""
    source_paths: list[str] = field(default_factory=list)
    default: Any = None
    confirmation_policy: str = "allow_inferred"
    validation_rule: str | None = None
    ask_hint: str = ""


@dataclass
class ToolSpec:
    name: str
    description: str
    executor_key: str
    parameters: dict[str, ToolParam] = field(default_factory=dict)
    approval_level: ApprovalLevel = ApprovalLevel.NONE
    risk_level: str = "low"
    supports_defaults: bool = True
    refinable: bool = False
    writes_state: bool = False
    may_fail_transiently: bool = False
    requires_confirmation_fields: list[str] = field(default_factory=list)


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "inspect_context": ToolSpec(
        name="inspect_context",
        description="Summarize the current task, working state, and available candidate inputs.",
        executor_key="inspect_context",
    ),
    "fetch_reference": ToolSpec(
        name="fetch_reference",
        description="Retrieve a fake reference packet for a topic.",
        executor_key="fetch_reference",
        refinable=True,
        parameters={
            "topic": ToolParam(
                name="topic",
                required=True,
                description="Topic to retrieve.",
                source_paths=["task.parsed_args.topic", "task.field_map.topic.value", "state.memory_bag.topic"],
                validation_rule="nonempty",
                ask_hint="Tell me what topic you want to retrieve.",
            ),
            "detail_level": ToolParam(
                name="detail_level",
                required=False,
                default="standard",
                description="How much detail to return.",
                source_paths=["task.parsed_args.detail_level"],
                validation_rule="oneof:brief,standard,detailed",
            ),
        },
    ),
    "analyze_options": ToolSpec(
        name="analyze_options",
        description="Analyze options for an objective using optional constraints.",
        executor_key="analyze_options",
        refinable=True,
        parameters={
            "objective": ToolParam(
                name="objective",
                required=True,
                description="Goal being optimized.",
                source_paths=["task.parsed_args.objective", "state.memory_bag.objective", "state.prior_tool_outputs.fetch_reference.topic"],
                validation_rule="nonempty",
                ask_hint="Describe the objective you want analyzed.",
            ),
            "constraints": ToolParam(
                name="constraints",
                type="list",
                required=False,
                description="Optional constraints list.",
                source_paths=["task.parsed_args.constraints", "state.memory_bag.constraints"],
                default=[],
            ),
        },
    ),
    "draft_plan": ToolSpec(
        name="draft_plan",
        description="Produce a fake step-by-step plan from the objective and prior outputs.",
        executor_key="draft_plan",
        parameters={
            "objective": ToolParam(
                name="objective",
                required=True,
                description="What the plan is for.",
                source_paths=["task.parsed_args.objective", "state.memory_bag.objective", "state.prior_tool_outputs.analyze_options.objective"],
                validation_rule="nonempty",
                ask_hint="What should the plan achieve?",
            ),
            "constraints": ToolParam(
                name="constraints",
                type="list",
                required=False,
                description="Constraints to honor.",
                source_paths=["task.parsed_args.constraints", "state.prior_tool_outputs.analyze_options.constraints"],
                default=[],
            ),
        },
    ),
    "apply_change": ToolSpec(
        name="apply_change",
        description="Apply a fake stateful change after explicit approval.",
        executor_key="apply_change",
        approval_level=ApprovalLevel.HARD,
        risk_level="high",
        writes_state=True,
        may_fail_transiently=True,
        requires_confirmation_fields=["change_summary"],
        parameters={
            "change_summary": ToolParam(
                name="change_summary",
                required=True,
                description="The change to apply.",
                source_paths=["task.parsed_args.change_summary", "state.memory_bag.change_summary"],
                validation_rule="nonempty",
                confirmation_policy="must_confirm",
                ask_hint="Describe the change to apply.",
            ),
            "target": ToolParam(
                name="target",
                required=False,
                description="Optional target of the change.",
                source_paths=["task.parsed_args.target", "state.memory_bag.target"],
                default="working_state",
            ),
        },
    ),
    "summarize_result": ToolSpec(
        name="summarize_result",
        description="Summarize the latest successful outputs.",
        executor_key="summarize_result",
        parameters={
            "audience": ToolParam(
                name="audience",
                required=False,
                description="Audience for the summary.",
                source_paths=["task.parsed_args.audience"],
                default="user",
            ),
        },
    ),
}


def get_tool_spec(name: str) -> ToolSpec:
    return TOOL_REGISTRY[name]


def list_tool_names() -> list[str]:
    return list(TOOL_REGISTRY.keys())
