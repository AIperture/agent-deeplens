from __future__ import annotations

from typing import Any

from .types import ConversationState, TaskFrame


# ---------------------------------------------------------------------
# What to implement here
# ---------------------------------------------------------------------
# - Thin adapter from loop action -> actual tool invocation
# - Build per-tool args from TaskFrame + override_args
# - Call the AG / local tool runtime
#
# What should NOT be included here
# - Repair logic
# - Broad task interpretation
# - Multi-step workflow control
# - Response composition
# ---------------------------------------------------------------------


def _merge_args(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(base)
    if override:
        merged.update(override)
    return merged


def _build_create_lens_args(task: TaskFrame, override_args: dict[str, Any] | None) -> dict[str, Any]:
    base = {
        "design_spec": dict(task.design_spec or {}),
        "analysis_request": dict(task.analysis_request or {}),
    }
    return _merge_args(base, override_args)


def _build_analysis_args(task: TaskFrame, override_args: dict[str, Any] | None) -> dict[str, Any]:
    base = {
        "lens_source": dict(task.lens_source or {}),
        "analysis_request": dict(task.analysis_request or {}),
        "mode": task.analysis_request.get("mode", "full"),
        "design_spec": dict(task.design_spec or {}),
    }
    return _merge_args(base, override_args)


def _build_export_args(task: TaskFrame, override_args: dict[str, Any] | None) -> dict[str, Any]:
    delivery = dict(task.delivery_request or {})
    base = {
        "lens_source": dict(task.lens_source or {}),
        "delivery_request": delivery,
        "formats": list(delivery.get("formats", [])) or [delivery["export_format"]] if delivery.get("export_format") else [],
    }
    return _merge_args(base, override_args)


def _build_spawn_graph_args(task: TaskFrame, override_args: dict[str, Any] | None) -> dict[str, Any]:
    base = {
        "run_request": dict(task.run_request or {}),
        "lens_source": dict(task.lens_source or {}),
        "design_spec": dict(task.design_spec or {}),
        "analysis_request": dict(task.analysis_request or {}),
    }
    return _merge_args(base, override_args)


def _build_run_control_args(task: TaskFrame, override_args: dict[str, Any] | None) -> dict[str, Any]:
    base = {
        "run_id": task.run_request.get("run_id"),
        "timeout_s": task.run_request.get("timeout_s", 1),
    }
    return _merge_args(base, override_args)


def build_tool_args(
    *,
    tool_name: str,
    task: TaskFrame,
    override_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if tool_name == "dl.create_lens":
        return _build_create_lens_args(task, override_args)

    if tool_name == "dl.analysis":
        return _build_analysis_args(task, override_args)

    if tool_name == "dl.export_lens":
        return _build_export_args(task, override_args)

    if tool_name == "ag.spawn_graph":
        return _build_spawn_graph_args(task, override_args)

    if tool_name in {"ag.status", "ag.cancel"}:
        return _build_run_control_args(task, override_args)

    return dict(override_args or {})


async def _dispatch_via_registry(
    *,
    tool_name: str,
    args: dict[str, Any],
    context: Any,
) -> Any:
    """
    Replace this with your actual AG tool dispatch mechanism.

    Expected behaviors:
    - resolve tool by name
    - invoke it with kwargs / normalized args
    - return the raw tool output

    Keep this function thin.
    """
    registry = getattr(context, "tool_registry", None)
    if registry is None:
        raise RuntimeError("context.tool_registry is not available for tool dispatch.")

    tool = registry.get(tool_name)
    if tool is None:
        raise RuntimeError(f"Tool not found: {tool_name}")

    # If your runtime uses a different invocation API, adapt it here.
    return await tool(**args)


async def dispatch_tool_action(
    *,
    tool_name: str,
    task: TaskFrame,
    state: ConversationState,
    context: Any,
    override_args: dict[str, Any] | None = None,
) -> Any:
    """
    Thin dispatch layer.

    Responsibilities:
    - build actual tool args from TaskFrame + overrides
    - inject a few runtime refs when appropriate
    - call the underlying tool runtime

    Non-responsibilities:
    - no repair
    - no retries
    - no task switching
    """
    del state  # available if you later need a small amount of runtime injection

    args = build_tool_args(
        tool_name=tool_name,
        task=task,
        override_args=override_args,
    )

    return await _dispatch_via_registry(
        tool_name=tool_name,
        args=args,
        context=context,
    )