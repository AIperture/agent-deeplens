"""Fake tool implementations for the skeleton agent.

Each executor follows the contract:
    async def execute_xxx(*, tool_name, resolved_inputs, task, state, context, **kw) -> ToolResult

State updates go in result.data["state_updates"] and are applied by the dispatcher.
"""
from __future__ import annotations

from typing import Any

from ..types import FailureKind, ToolResult


async def execute_calculate(
    *,
    tool_name: str,
    resolved_inputs: dict[str, Any],
    task: Any = None,
    state: Any = None,
    context: Any = None,
    **_: Any,
) -> ToolResult:
    """Perform arithmetic on two numbers."""
    a = resolved_inputs.get("a")
    b = resolved_inputs.get("b")
    operation = str(resolved_inputs.get("operation") or "add").lower()

    if a is None or b is None:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            summary="Missing required operands (a and/or b).",
            failure_kind=FailureKind.MISSING_INPUTS,
            needs_user_input=True,
            missing_fields=[k for k in ("a", "b") if resolved_inputs.get(k) is None],
        )

    try:
        a = float(a)
        b = float(b)
    except (TypeError, ValueError) as exc:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            summary=f"Invalid numeric inputs: {exc}",
            failure_kind=FailureKind.INVALID_INPUTS,
            repairable=True,
            invalid_fields={"a": str(a), "b": str(b)},
        )

    ops = {
        "add": lambda: a + b,
        "subtract": lambda: a - b,
        "multiply": lambda: a * b,
        "divide": lambda: a / b if b != 0 else None,
    }
    if operation not in ops:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            summary=f"Unsupported operation: {operation}. Use add, subtract, multiply, or divide.",
            failure_kind=FailureKind.INVALID_INPUTS,
            repairable=True,
            invalid_fields={"operation": operation},
        )

    result_value = ops[operation]()
    if result_value is None:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            summary="Division by zero.",
            failure_kind=FailureKind.INVALID_INPUTS,
            repairable=True,
            invalid_fields={"b": "0"},
        )

    return ToolResult(
        ok=True,
        tool_name=tool_name,
        summary=f"Result: {a} {operation} {b} = {result_value}",
        data={
            "result": result_value,
            "state_updates": {
                "domain_state.last_calculation": result_value,
            },
        },
    )


async def execute_search_knowledge(
    *,
    tool_name: str,
    resolved_inputs: dict[str, Any],
    task: Any = None,
    state: Any = None,
    context: Any = None,
    **_: Any,
) -> ToolResult:
    """Simulate a knowledge search with mock results."""
    query = str(resolved_inputs.get("query") or "")
    max_results = int(resolved_inputs.get("max_results") or 5)

    if not query.strip():
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            summary="No search query provided.",
            failure_kind=FailureKind.MISSING_INPUTS,
            needs_user_input=True,
            missing_fields=["query"],
        )

    # Fake search results
    mock_results = [
        {"title": f"Result {i+1} for '{query}'", "snippet": f"This is a mock result about {query}.", "relevance": round(1.0 - i * 0.15, 2)}
        for i in range(min(max_results, 5))
    ]

    return ToolResult(
        ok=True,
        tool_name=tool_name,
        summary=f"Found {len(mock_results)} result(s) for '{query}'.",
        data={
            "results": mock_results,
            "query": query,
            "state_updates": {
                "domain_state.last_search_query": query,
                "domain_state.last_search_results": mock_results,
            },
        },
    )


async def execute_generate_report(
    *,
    tool_name: str,
    resolved_inputs: dict[str, Any],
    task: Any = None,
    state: Any = None,
    context: Any = None,
    **_: Any,
) -> ToolResult:
    """Compile previous results from state into a text report."""
    title = str(resolved_inputs.get("title") or "Summary Report")
    include_sources = bool(resolved_inputs.get("include_sources", True))

    domain_state = getattr(state, "domain_state", {}) if state else {}
    sections: list[str] = [f"# {title}", ""]

    # Include calculation results if available
    last_calc = domain_state.get("last_calculation")
    if last_calc is not None:
        sections.append(f"## Calculation Result\n{last_calc}")

    # Include search results if available
    last_search = domain_state.get("last_search_results")
    last_query = domain_state.get("last_search_query")
    if last_search:
        sections.append(f"## Search Results for '{last_query}'")
        for item in last_search[:3]:
            sections.append(f"- **{item.get('title', 'N/A')}**: {item.get('snippet', '')}")
        if include_sources:
            sections.append(f"\n_Source: knowledge base query '{last_query}'_")

    if len(sections) <= 2:
        sections.append("No previous results available. Run some tools first, then generate the report.")

    report_text = "\n".join(sections)
    return ToolResult(
        ok=True,
        tool_name=tool_name,
        summary=f"Generated report: '{title}' ({len(sections)} sections).",
        data={
            "report": report_text,
            "state_updates": {
                "domain_state.last_report": report_text,
            },
        },
    )


# ---------------------------------------------------------------------------
# Executor map: tool name -> async executor function
# ---------------------------------------------------------------------------

EXECUTOR_MAP: dict[str, Any] = {
    "calculate": execute_calculate,
    "search_knowledge": execute_search_knowledge,
    "generate_report": execute_generate_report,
}
