from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .tool_arg_refine import maybe_refine_tool_inputs_with_llm
from .tool_registry import get_tool_spec
from .types import ApprovalLevel, PendingRun, ToolResult


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_artifacts(state: Any, artifacts: list[dict[str, Any]]) -> None:
    if not artifacts:
        return
    state.last_artifacts.extend(artifacts)
    state.last_artifacts = state.last_artifacts[-20:]


def _record_pending_run(state: Any, run_id: str, graph_id: str, meta: dict[str, Any] | None = None) -> None:
    rec = PendingRun(
        run_id=run_id,
        graph_id=graph_id,
        submitted_at=_utc_now_iso(),
        meta=meta or {},
    )
    state.pending_runs.append(rec.__dict__)
    state.pending_runs = state.pending_runs[-20:]
    state.active_run_id = run_id
    state.pending_action = "run_control"


def _update_pending_run_status(state: Any, run_id: str, status: str) -> None:
    for rec in state.pending_runs:
        if rec.get("run_id") == run_id:
            rec["status"] = status
    if status in {"completed", "failed", "canceled"} and state.active_run_id == run_id:
        state.active_run_id = None
        state.pending_action = None


async def _record_tool_memory(context: Any, tool_name: str, inputs: dict[str, Any], result: ToolResult) -> None:
    await context.memory().record_tool_result(
        tool=tool_name,
        inputs=[inputs],
        outputs=[
            {
                "ok": result.ok,
                "status": result.status,
                "summary": result.summary,
                "run_id": result.run_id,
                "data": result.data,
                "warnings": result.warnings,
                "error_code": result.error_code,
            }
        ],
        message=result.summary,
        tags=["ag.deeplens.v2.tool", f"ag.deeplens.v2.action:{tool_name}"],
    )


async def _maybe_approval(spec: Any, action: dict[str, Any], context: Any) -> bool:
    if spec.approval_level == ApprovalLevel.NONE:
        return True
    prompt = action.get("args", {}).get("approval_prompt") or f"Approve `{spec.name}`?"
    resp = await context.channel("ui:session").ask_approval(
        prompt=prompt,
        options=["Approve", "Cancel"],
    )
    return bool(resp.get("approved"))


async def _resolve_inputs(
    *,
    spec: Any,
    action: dict[str, Any],
    task: Any,
    state: Any,
    context_bundle: Any,
    context: Any,
) -> dict[str, Any]:
    args = dict(action.get("args") or {})
    args.setdefault("task_shape", task.task_shape.value)
    args.setdefault("domain_hint", task.domain_hint.value)
    args.setdefault("user_goal", task.user_goal)
    args.setdefault("parsed_args", task.parsed_args)
    args.setdefault("missing_fields", task.missing_fields)
    args.setdefault("active_lens_ref", state.active_lens_ref)
    args.setdefault("active_artifact_refs", task.active_artifact_refs)
    args.setdefault("run_id", state.active_run_id)
    return await maybe_refine_tool_inputs_with_llm(
        spec=spec,
        draft_inputs=args,
        task=task,
        state=state,
        context_bundle=context_bundle,
        context=context,
    )


async def _exec_ag_search(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    query = resolved_inputs.get("query") or resolved_inputs.get("user_goal") or ""
    return ToolResult(
        ok=True,
        summary=f"Searched scoped context for: {query}",
        data={"results": []},
    )


async def _exec_ag_send_image(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    image_ref = resolved_inputs.get("image_ref", "placeholder.png")
    await context.channel("ui:session").send_text(f"[image] {image_ref}")
    return ToolResult(ok=True, summary=f"Sent image `{image_ref}` to the UI.")


async def _exec_ag_send_file(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    file_ref = resolved_inputs.get("file_ref", "placeholder.dat")
    await context.channel("ui:session").send_text(f"[file] {file_ref}")
    return ToolResult(ok=True, summary=f"Sent file `{file_ref}` to the UI.")


async def _exec_ag_spawn_graph(*, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any, **_: Any) -> ToolResult:
    graph_id = resolved_inputs["graph_id"]
    graph_inputs = resolved_inputs.get("graph_inputs", {})
    run_id = await context.runner().spawn_run(
        graph_id,
        inputs=graph_inputs,
        tags=["ag.deeplens.v2", f"shape:{task.task_shape.value}", f"domain:{task.domain_hint.value}"],
    )
    _record_pending_run(state, run_id, graph_id, meta={"source_action": "ag.spawn_graph"})
    return ToolResult(
        ok=True,
        status="submitted",
        summary=(
            f"Started background graph `{graph_id}`.\n"
            f"Run ID: `{run_id}`.\n"
            "It is running in the background now."
        ),
        run_id=run_id,
        should_end_turn=True,
    )


async def _exec_ag_wait_run_short(*, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    run_id = resolved_inputs["run_id"]
    record, outputs = await context.runner().wait_run(
        run_id,
        timeout_s=resolved_inputs.get("timeout_s", 20),
        return_outputs=True,
    )
    status = str(getattr(record, "status", "unknown"))
    _update_pending_run_status(state, run_id, status)
    return ToolResult(
        ok=True,
        summary=f"Run `{run_id}` status: `{status}`.",
        data={"record": {"status": status}, "outputs": outputs or {}},
        run_id=run_id,
    )


async def _exec_ag_cancel_run(*, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    run_id = resolved_inputs["run_id"]
    await context.runner().cancel_run(run_id)
    _update_pending_run_status(state, run_id, "cancel_requested")
    return ToolResult(
        ok=True,
        status="canceled",
        summary=f"Requested cancellation for run `{run_id}`.",
        run_id=run_id,
        should_end_turn=True,
    )


async def _exec_dl_inspect_input(*, task: Any, **_: Any) -> ToolResult:
    count = len(task.attachments or [])
    return ToolResult(
        ok=True,
        summary=f"Inspected {count} attachment(s).",
        data={"attachments_count": count},
    )


async def _exec_dl_simulate_standard(*, resolved_inputs: dict[str, Any], state: Any, **_: Any) -> ToolResult:
    metrics = {
        "mtf": resolved_inputs.get("metric", "placeholder"),
        "psf": "placeholder",
    }
    state.last_metrics.update(metrics)
    state.pending_action = None
    return ToolResult(
        ok=True,
        summary="Completed standard DeepLens simulation or analysis.",
        data={"metrics": metrics},
    )


async def _exec_dl_submit_optimization(*, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any, **_: Any) -> ToolResult:
    graph_id = resolved_inputs.get("graph_id", "deeplens_optimize_workflow")
    graph_inputs = resolved_inputs.get(
        "graph_inputs",
        {
            "goal": task.user_goal,
            "parsed_args": task.parsed_args,
            "active_lens_ref": state.active_lens_ref,
        },
    )
    run_id = await context.runner().spawn_run(
        graph_id,
        inputs=graph_inputs,
        tags=["ag.deeplens.v2", "optimization"],
    )
    _record_pending_run(state, run_id, graph_id, meta={"source_action": "dl.submit_optimization"})
    return ToolResult(
        ok=True,
        status="submitted",
        summary=(
            f"Started background optimization via graph `{graph_id}`.\n"
            f"Run ID: `{run_id}`.\n"
            "I am not blocking the session while it runs."
        ),
        run_id=run_id,
        should_end_turn=True,
    )


async def _exec_dl_check_run_status(*, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    run_id = resolved_inputs["run_id"]
    record = await context.runner().wait_run(run_id, timeout_s=1)
    status = str(getattr(record, "status", "unknown"))
    _update_pending_run_status(state, run_id, status)
    return ToolResult(
        ok=True,
        summary=f"Run `{run_id}` current or terminal status: `{status}`.",
        run_id=run_id,
        data={"status": status},
    )


EXECUTOR_MAP = {
    "ag.search": _exec_ag_search,
    "ag.send_image": _exec_ag_send_image,
    "ag.send_file": _exec_ag_send_file,
    "ag.spawn_graph": _exec_ag_spawn_graph,
    "ag.wait_run_short": _exec_ag_wait_run_short,
    "ag.cancel_run": _exec_ag_cancel_run,
    "dl.inspect_input": _exec_dl_inspect_input,
    "dl.simulate_standard": _exec_dl_simulate_standard,
    "dl.submit_optimization": _exec_dl_submit_optimization,
    "dl.check_run_status": _exec_dl_check_run_status,
}


async def dispatch_tool_action(
    *,
    action: dict[str, Any],
    task: Any,
    state: Any,
    context_bundle: Any,
    context: Any,
) -> ToolResult:
    tool_name = action.get("name") or ""
    spec = get_tool_spec(tool_name)

    approved = await _maybe_approval(spec, action, context)
    if not approved:
        return ToolResult(
            ok=True,
            status="canceled",
            summary=f"Skipped `{tool_name}` because approval was not granted.",
            should_end_turn=True,
        )

    resolved_inputs = await _resolve_inputs(
        spec=spec,
        action=action,
        task=task,
        state=state,
        context_bundle=context_bundle,
        context=context,
    )
    result = await EXECUTOR_MAP[spec.executor_key](
        resolved_inputs=resolved_inputs,
        task=task,
        state=state,
        context_bundle=context_bundle,
        context=context,
    )
    if result.artifacts:
        _append_artifacts(state, result.artifacts)
    await _record_tool_memory(context, tool_name, resolved_inputs, result)
    return result
