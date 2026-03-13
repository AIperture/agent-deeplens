from __future__ import annotations

from typing import Any

from ..types import FailureKind, ToolResult


async def execute_inspect_context(
    *,
    tool_name: str,
    task: Any,
    state: Any,
    **_: Any,
) -> ToolResult:
    return ToolResult(
        ok=True,
        tool_name=tool_name,
        summary="Inspected current context.",
        data={
            "inspection": {
                "user_goal": getattr(task, "user_goal", ""),
                "parsed_args": dict(getattr(task, "parsed_args", {}) or {}),
                "memory_bag": dict(getattr(state, "memory_bag", {}) or {}),
                "prior_outputs": list((getattr(state, "prior_tool_outputs", {}) or {}).keys()),
            }
        },
    )


async def execute_fetch_reference(
    *,
    tool_name: str,
    resolved_inputs: dict[str, Any],
    **_: Any,
) -> ToolResult:
    topic = str(resolved_inputs.get("topic") or "").strip()
    detail_level = str(resolved_inputs.get("detail_level") or "standard")
    if not topic:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            summary="Missing topic for reference retrieval.",
            failure_kind=FailureKind.MISSING_INPUTS,
            needs_user_input=True,
            missing_fields=["topic"],
        )
    return ToolResult(
        ok=True,
        tool_name=tool_name,
        summary=f"Fetched fake reference for {topic}.",
        data={"topic": topic, "detail_level": detail_level, "reference": f"{topic.title()} reference packet ({detail_level})."},
    )


async def execute_analyze_options(
    *,
    tool_name: str,
    resolved_inputs: dict[str, Any],
    state: Any,
    **_: Any,
) -> ToolResult:
    objective = str(resolved_inputs.get("objective") or "").strip()
    constraints = resolved_inputs.get("constraints") or []
    if not objective:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            summary="Missing objective for option analysis.",
            failure_kind=FailureKind.MISSING_INPUTS,
            needs_user_input=True,
            missing_fields=["objective"],
        )
    if isinstance(constraints, str) and constraints.lower() == "explode":
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            summary="Constraint format invalid.",
            failure_kind=FailureKind.INVALID_INPUTS,
            invalid_fields={"constraints": "expected a list or comma-separated list"},
        )
    if isinstance(constraints, list) and any(str(item).lower() == "explode" for item in constraints):
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            summary="Constraint value `explode` is not allowed.",
            failure_kind=FailureKind.INVALID_INPUTS,
            invalid_fields={"constraints": "remove the unsupported constraint value `explode`"},
        )
    prior_reference = (getattr(state, "prior_tool_outputs", {}) or {}).get("fetch_reference", {})
    options = [f"Use direct approach for {objective}", f"Use staged approach for {objective}"]
    if prior_reference.get("topic"):
        options.append(f"Reference-guided approach using {prior_reference['topic']}")
    return ToolResult(
        ok=True,
        tool_name=tool_name,
        summary=f"Analyzed options for {objective}.",
        data={"objective": objective, "constraints": constraints, "options": options},
    )


async def execute_draft_plan(
    *,
    tool_name: str,
    resolved_inputs: dict[str, Any],
    state: Any,
    **_: Any,
) -> ToolResult:
    objective = str(resolved_inputs.get("objective") or "").strip()
    if not objective:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            summary="Missing objective for plan drafting.",
            failure_kind=FailureKind.MISSING_INPUTS,
            needs_user_input=True,
            missing_fields=["objective"],
        )
    analysis = (getattr(state, "prior_tool_outputs", {}) or {}).get("analyze_options", {})
    steps = ["Clarify objective", "Collect inputs", "Evaluate options", "Choose next steps"]
    if analysis.get("options"):
        steps.insert(2, f"Review {len(analysis['options'])} analyzed option(s)")
    return ToolResult(
        ok=True,
        tool_name=tool_name,
        summary=f"Drafted a plan for {objective}.",
        data={"objective": objective, "constraints": resolved_inputs.get("constraints") or [], "steps": steps},
    )


async def execute_apply_change(
    *,
    tool_name: str,
    resolved_inputs: dict[str, Any],
    state: Any,
    **_: Any,
) -> ToolResult:
    change_summary = str(resolved_inputs.get("change_summary") or "").strip()
    if not change_summary:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            summary="Missing change summary.",
            failure_kind=FailureKind.MISSING_INPUTS,
            needs_user_input=True,
            missing_fields=["change_summary"],
        )
    retry_key = f"apply_change:{change_summary}"
    attempts = int((getattr(state, "retry_counters", {}) or {}).get(retry_key, 0))
    if "transient" in change_summary.lower() and attempts < 1:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            summary="Temporary backend glitch while applying change.",
            failure_kind=FailureKind.TRANSIENT_ERROR,
            retryable=True,
        )
    target = str(resolved_inputs.get("target") or "working_state")
    return ToolResult(
        ok=True,
        tool_name=tool_name,
        summary=f"Applied fake change to {target}: {change_summary}.",
        data={"applied_change": change_summary, "target": target, "state_updates": {"memory_bag.last_applied_change": change_summary}},
    )


async def execute_summarize_result(
    *,
    tool_name: str,
    resolved_inputs: dict[str, Any],
    state: Any,
    **_: Any,
) -> ToolResult:
    audience = str(resolved_inputs.get("audience") or "user")
    outputs = getattr(state, "prior_tool_outputs", {}) or {}
    if not outputs:
        return ToolResult(ok=True, tool_name=tool_name, summary="Nothing substantial ran yet, so there is nothing to summarize.", should_end_turn=True)
    last_name = list(outputs.keys())[-1]
    return ToolResult(
        ok=True,
        tool_name=tool_name,
        summary=f"Summary for {audience}: latest result came from {last_name}.",
        data={"audience": audience, "latest_tool": last_name, "latest_output": outputs[last_name]},
        should_end_turn=True,
    )


EXECUTOR_MAP: dict[str, Any] = {
    "inspect_context": execute_inspect_context,
    "fetch_reference": execute_fetch_reference,
    "analyze_options": execute_analyze_options,
    "draft_plan": execute_draft_plan,
    "apply_change": execute_apply_change,
    "summarize_result": execute_summarize_result,
}
