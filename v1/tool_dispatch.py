from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .tool_registry import ToolSpec, get_tool_spec
from .types import PendingRun, ToolExecutionStyle, ToolResult


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_artifacts_to_state(state: Any, artifacts: list[dict[str, Any]]) -> None:
    if not artifacts:
        return
    state.last_artifacts.extend(artifacts)
    state.last_artifacts = state.last_artifacts[-20:]


def _record_pending_run(
    *,
    state: Any,
    run_id: str,
    graph_id: str,
    intent: str,
    meta: dict[str, Any] | None = None,
) -> None: 
    rec = PendingRun(
        run_id=run_id,
        graph_id=graph_id,
        intent=intent,
        submitted_at=_utc_now_iso(),
        meta=meta or {},
    )
    state.pending_runs.append(rec.__dict__)
    state.pending_runs = state.pending_runs[-20:]
    state.active_run_id = run_id


async def _record_tool_memory(
    *,
    context: Any,
    tool_name: str,
    inputs: dict[str, Any],
    result: ToolResult,
) -> None:
    await context.memory().record_tool_result(
        tool=tool_name,
        inputs=[inputs],
        outputs=[{
            "ok": result.ok,
            "status": result.status,
            "summary": result.summary,
            "run_id": result.run_id,
            "data": result.data,
            "warnings": result.warnings,
            "error_code": result.error_code,
        }],
        message=result.summary,
        tags=["ag.deeplens.tool", f"ag.deeplens.action:{tool_name}"],
    )


async def _maybe_request_approval_for_spec(
    *,
    spec: ToolSpec,
    action: dict[str, Any],
    context: Any,
) -> bool:
    if not spec.require_approval:
        return True
    
    prompt = (
        action.get("args", {}).get("approval_prompt")
        or f"Approve action `{spec.name}`?"
    )

    resp = await context.channel("ui:session").ask_approval(
        prompt=prompt, options=["Approve", "Cancel"])
    print("🍎 approval responses: ", resp)
    approved = resp.get("choice") in ("approve", "Approve")

    return approved 

async def _resolve_tool_inputs(
    *,
    spec: ToolSpec,
    action: dict[str, Any],
    task: Any,
    state: Any,
    context_bundle: Any,
    context: Any,
) -> dict[str, Any]:
    # This stays deterministic by default.
    # Later you can optionally add a small helper LLM here for argument refinement.
    args = dict(action.get("args") or {})
    args.setdefault("intent", task.intent.value)
    args.setdefault("user_goal", task.user_goal)
    args.setdefault("active_lens_ref", state.active_lens_ref)
    args.setdefault("parsed_args", task.parsed_args)
    args.setdefault("missing_fields", task.missing_fields)
    args.setdefault("constraints", task.constraints)
    args.setdefault("last_metrics", state.last_metrics)
    args.setdefault("active_artifact_refs", task.active_artifact_refs)
    return args



# -------------------------
# AG executors
# -------------------------

async def _exec_ag_save_artifact(*, resolved_inputs: dict[str, Any], state: Any, context: Any) -> ToolResult:
    # Placeholder:
    # map to context.artifacts().put/save methods in your real implementation.
    artifact = {
        "kind": resolved_inputs.get("kind", "text"),
        "name": resolved_inputs.get("name", "artifact.txt"),
        "label": resolved_inputs.get("label", "Generated artifact"),
    }
    _append_artifacts_to_state(state, [artifact])
    return ToolResult(
        ok=True,
        summary=f"Saved artifact `{artifact['name']}`.",
        artifacts=[artifact],
    )


async def _exec_ag_spawn_long_run(*, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any) -> ToolResult:
    runner = context.runner()

    graph_id = resolved_inputs["graph_id"]
    graph_inputs = resolved_inputs.get("graph_inputs", {})
    tags = resolved_inputs.get("tags") or ["ag.deeplens", f"intent:{task.intent.value}"]

    run_id = await runner.spawn_run(
        graph_id,
        inputs=graph_inputs,
        tags=tags,
    )

    _record_pending_run(
        state=state,
        run_id=run_id,
        graph_id=graph_id,
        intent=task.intent.value,
        meta={"source_action": "spawn_long_run"},
    )

    return ToolResult(
        ok=True,
        status="submitted",
        summary=(
            f"Started background run for `{graph_id}`.\n"
            f"Run ID: `{run_id}`.\n"
            "It is running in the background now. You may ask for status or cancel it."
        ),
        run_id=run_id,
        data={"graph_id": graph_id},
        should_end_turn=True,
    )


async def _exec_ag_wait_on_run_short(*, resolved_inputs: dict[str, Any], context: Any) -> ToolResult:
    runner = context.runner()
    run_id = resolved_inputs["run_id"]
    timeout_s = resolved_inputs.get("timeout_s", 30)
    record, outputs = await runner.wait_run(
        run_id,
        timeout_s=timeout_s,
        return_outputs=True,
    )

    return ToolResult(
        ok=True,
        summary=f"Run `{run_id}` reached terminal state `{getattr(record, 'status', 'done')}`.",
        data={"record": record, "outputs": outputs},
    )


async def _exec_ag_cancel_run(*, resolved_inputs: dict[str, Any], state: Any, context: Any) -> ToolResult:
    runner = context.runner()
    run_id = resolved_inputs["run_id"]
    await runner.cancel_run(run_id)

    for rec in state.pending_runs:
        if rec.get("run_id") == run_id:
            rec["status"] = "cancel_requested"

    if state.active_run_id == run_id:
        state.active_run_id = None

    return ToolResult(
        ok=True,
        status="canceled",
        summary=f"Requested cancellation for run `{run_id}`.",
        run_id=run_id,
    )


# -------------------------
# DeepLens executors
# -------------------------

async def _exec_dl_read_attachment(*, resolved_inputs: dict[str, Any], task: Any, context: Any) -> ToolResult:
    # Placeholder for your real attachment/lens loading logic.
    count = len(task.attachments or [])
    return ToolResult(
        ok=True,
        summary=f"Inspected {count} attachment(s).",
        data={"attachments_count": count},
    )


async def _exec_dl_run_analysis(*, resolved_inputs: dict[str, Any], state: Any, context: Any) -> ToolResult:
    # Placeholder for actual DeepLens analysis call.
    metrics = {
        "mtf": "placeholder",
        "psf": "placeholder",
    }
    state.last_metrics.update(metrics)
    return ToolResult(
        ok=True,
        summary="Completed DeepLens analysis.",
        data={"metrics": metrics},
    )


async def _exec_dl_run_targeted_eval(*, resolved_inputs: dict[str, Any], state: Any, context: Any) -> ToolResult:
    metrics = {
        "target_eval": resolved_inputs.get("eval_type", "mtf"),
        "value": "placeholder",
    }
    state.last_metrics.update(metrics)
    return ToolResult(
        ok=True,
        summary=f"Completed targeted evaluation `{metrics['target_eval']}`.",
        data={"metrics": metrics},
    )


async def _exec_dl_propose_initial_design(*, resolved_inputs: dict[str, Any], context: Any) -> ToolResult:
    return ToolResult(
        ok=True,
        summary="Generated initial lens-design proposal scaffold.",
        data={"proposal": "placeholder"},
    )


async def _exec_dl_generate_lens_config(*, resolved_inputs: dict[str, Any], state: Any, context: Any) -> ToolResult:
    artifact = {
        "kind": "lens_config",
        "name": resolved_inputs.get("name", "lens_config.json"),
        "label": "Generated lens config",
    }
    state.active_lens_ref = artifact["name"]
    _append_artifacts_to_state(state, [artifact])
    return ToolResult(
        ok=True,
        summary=f"Generated lens config `{artifact['name']}`.",
        artifacts=[artifact],
    )


async def _exec_dl_run_optimization_stage(*, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any) -> ToolResult:
    runner = context.runner()

    graph_id = resolved_inputs.get("graph_id", "deeplens_optimize_workflow")
    graph_inputs = resolved_inputs.get("graph_inputs", {
        "task": task.user_goal,
        "parsed_args": task.parsed_args,
        "active_lens_ref": state.active_lens_ref,
    })
    tags = ["ag.deeplens", "optimization", f"intent:{task.intent.value}"]

    run_id = await runner.spawn_run(
        graph_id,
        inputs=graph_inputs,
        tags=tags,
    )

    _record_pending_run(
        state=state,
        run_id=run_id,
        graph_id=graph_id,
        intent=task.intent.value,
        meta={"source_action": "run_optimization_stage"},
    )

    return ToolResult(
        ok=True,
        status="submitted",
        summary=(
            f"Started background optimization.\n"
            f"Run ID: `{run_id}`.\n"
            "This may take a long time, so I am not blocking the session. "
            "You may ask for status or cancel it."
        ),
        run_id=run_id,
        data={"graph_id": graph_id},
        should_end_turn=True,
    )


async def _exec_dl_inspect_failure(*, resolved_inputs: dict[str, Any], context: Any) -> ToolResult:
    return ToolResult(
        ok=True,
        summary="Inspected failure symptoms and collected diagnostic hints.",
        data={"diagnostic_hints": ["placeholder"]},
    )


async def _exec_dl_run_diagnostic(*, resolved_inputs: dict[str, Any], context: Any) -> ToolResult:
    return ToolResult(
        ok=True,
        summary="Ran diagnostic routine.",
        data={"root_cause": "placeholder"},
    )


async def _exec_dl_apply_fix_patch(*, resolved_inputs: dict[str, Any], state: Any, context: Any) -> ToolResult:
    artifact = {
        "kind": "lens_patch",
        "name": resolved_inputs.get("name", "lens_fix_patch.json"),
        "label": "Applied fix patch",
    }
    _append_artifacts_to_state(state, [artifact])
    return ToolResult(
        ok=True,
        summary=f"Applied fix patch and saved `{artifact['name']}`.",
        artifacts=[artifact],
    )


async def _exec_dl_render_plot(*, resolved_inputs: dict[str, Any], state: Any, context: Any) -> ToolResult:
    artifact = {
        "kind": "plot",
        "name": resolved_inputs.get("name", "plot.png"),
        "label": "Rendered plot",
    }
    _append_artifacts_to_state(state, [artifact])
    return ToolResult(
        ok=True,
        summary=f"Rendered plot `{artifact['name']}`.",
        artifacts=[artifact],
    )


EXECUTOR_MAP = {
    "ag.save_artifact": _exec_ag_save_artifact,
    "ag.spawn_long_run": _exec_ag_spawn_long_run,
    "ag.wait_on_run_short": _exec_ag_wait_on_run_short,
    "ag.cancel_run": _exec_ag_cancel_run,
    "dl.read_attachment": _exec_dl_read_attachment,
    "dl.run_analysis": _exec_dl_run_analysis,
    "dl.run_targeted_eval": _exec_dl_run_targeted_eval,
    "dl.propose_initial_design": _exec_dl_propose_initial_design,
    "dl.generate_lens_config": _exec_dl_generate_lens_config,
    "dl.run_optimization_stage": _exec_dl_run_optimization_stage,
    "dl.inspect_failure": _exec_dl_inspect_failure,
    "dl.run_diagnostic": _exec_dl_run_diagnostic,
    "dl.apply_fix_patch": _exec_dl_apply_fix_patch,
    "dl.render_plot": _exec_dl_render_plot,
}


async def dispatch_tool_action(
    *,
    action: dict[str, Any],
    task: Any,
    policy: Any,
    state: Any,
    context_bundle: Any,
    context: Any,
) -> ToolResult:
    tool_name = action.get("name") or ""
    spec = get_tool_spec(tool_name)

    approved = await _maybe_request_approval_for_spec(
        spec=spec,
        action=action,
        context=context,
    )
    if not approved:
        return ToolResult(
            ok=True,
            status="canceled",
            summary=f"Skipped `{tool_name}` because approval was not granted.",
            should_end_turn=True,
        )

    resolved_inputs = await _resolve_tool_inputs(
        spec=spec,
        action=action,
        task=task,
        state=state,
        context_bundle=context_bundle,
        context=context,
    )

    executor = EXECUTOR_MAP[spec.executor_key]

    if spec.execution_style == ToolExecutionStyle.INLINE:
        result = await executor(
            resolved_inputs=resolved_inputs,
            task=task,
            state=state,
            context=context,
        )
    elif spec.execution_style == ToolExecutionStyle.SPAWN:
        result = await executor(
            resolved_inputs=resolved_inputs,
            task=task,
            state=state,
            context=context,
        )
    elif spec.execution_style == ToolExecutionStyle.SPAWN_AND_WAIT_SHORT:
        result = await executor(
            resolved_inputs=resolved_inputs,
            task=task,
            state=state,
            context=context,
        )
    else:
        result = ToolResult(
            ok=False,
            status="failed",
            summary=f"Unsupported execution style for `{tool_name}`.",
            error_code="unsupported_execution_style",
        )

    await _record_tool_memory(
        context=context,
        tool_name=tool_name,
        inputs=resolved_inputs,
        result=result,
    )

    return result


    