from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .backend import (
    artifact_to_ref,
    build_optimization_inputs,
    create_lens_design,
    deliver_artifacts,
    export_lens,
    load_text_or_json_payload,
    maybe_extract_filename,
    missing_design_fields,
    resolve_lens_source,
    run_analysis,
    summarize_artifacts,
    summarize_status,
)
from .tool_registry import get_tool_spec
from .tool_results import failure_result, needs_input_result, normalize_tool_result, run_submitted_result, run_updated_result, success_result
from .types import ExecutionRequest, PendingRun, TaskFrame, ToolResult


logger = logging.getLogger("ag.deeplens.v4.tool_dispatch")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    if status in {"completed", "failed", "canceled", "cancel_requested"} and state.active_run_id == run_id:
        state.active_run_id = None if status in {"completed", "failed", "canceled"} else state.active_run_id


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
        tags=["ag.deeplens.v4.tool", f"ag.deeplens.v4.action:{tool_name}"],
    )


async def _maybe_approval(spec: Any, execution: ExecutionRequest, state: Any, context: Any) -> bool:
    if spec.approval_level.value == "none":
        return True
    if state.approved_action == spec.name:
        state.approved_action = None
        return True
    if spec.approval_level.value == "hard":
        prompt = execution.execution_constraints.get("approval_prompt") or f"Approve `{spec.name}`?"
        resp = await context.channel("ui:session").ask_approval(prompt=prompt, options=["Approve", "Reject"])
        approved = bool(resp.get("approved"))
        state.approved_action = spec.name if approved else None
        return approved
    prompt = execution.execution_constraints.get("approval_prompt") or f"Approve `{spec.name}`?"
    resp = await context.channel("ui:session").ask_approval(prompt=prompt, options=["Approve", "Cancel"])
    approved = bool(resp.get("approved"))
    state.approved_action = spec.name if approved else None
    return approved


def _base_inputs(execution: ExecutionRequest, task: TaskFrame, state: Any) -> dict[str, Any]:
    args = dict(execution.normalized_args or {})
    args.setdefault("task_shape", task.task_shape.value)
    args.setdefault("domain_hint", task.domain_hint.value)
    args.setdefault("user_goal", task.user_goal)
    args.setdefault("missing_fields", list(task.missing_fields))
    args.setdefault("source_refs", list(task.source_refs))
    args.setdefault("active_lens_ref", state.active_lens_ref)
    args.setdefault("active_artifact_refs", list(task.active_artifact_refs))
    args.setdefault("active_source_ref", state.active_source_ref)
    args.setdefault("run_id", state.active_run_id or task.run_request.get("run_id"))
    args.setdefault("use_stub", bool(task.run_request.get("use_stub")))
    args.setdefault("lens_source", task.lens_source or task.analysis_request.get("source_ref") or state.active_source_ref)
    args.setdefault("design_spec", task.design_spec or state.design_draft)
    args.setdefault("analysis_request", task.analysis_request)
    args.setdefault("run_request", task.run_request)
    args.setdefault("delivery_request", task.delivery_request)
    if "mode" not in args and task.analysis_request.get("mode"):
        args["mode"] = task.analysis_request["mode"]
    if "formats" not in args:
        export_formats = task.delivery_request.get("formats")
        export_format = task.delivery_request.get("export_format")
        if export_formats:
            args["formats"] = list(export_formats)
        elif export_format:
            args["formats"] = [str(export_format)]
    return args


async def _exec_ag_get_latest_uploads(*, context: Any, **_: Any) -> ToolResult:
    uploads = await context.channel("ui:session").get_latest_uploads(clear=False)
    names = [u.get("name") or u.get("filename") or u.get("uri") or "upload" for u in uploads]
    return success_result(tool_name="ag.get_latest_uploads", summary=f"Found {len(uploads)} uploaded file(s).", data={"uploads": uploads, "names": names})


async def _exec_ag_load_artifact_text_or_json(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    artifact_id = resolved_inputs.get("artifact_id")
    uri = resolved_inputs.get("uri")
    if artifact_id:
        raw_text = await context.artifacts().load_text_by_id(str(artifact_id))
    elif uri:
        raw_text = await context.artifacts().load_text(str(uri))
    else:
        return failure_result(tool_name="ag.load_artifact_text_or_json", summary="No artifact id or uri was provided.", error_code="missing_input", missing_fields=["lens_source"])
    payload = load_text_or_json_payload(raw_text)
    return success_result(tool_name="ag.load_artifact_text_or_json", summary="Loaded artifact payload.", data={"payload": payload})


async def _exec_ag_save_text_artifact(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    payload = str(resolved_inputs.get("payload") or resolved_inputs.get("text") or "")
    name = str(resolved_inputs.get("name") or "deeplens-note.txt")
    artifact = await context.artifacts().save_text(payload, name=name)
    return success_result(tool_name="ag.save_text_artifact", summary=f"Saved text artifact `{name}`.", artifacts=[artifact_to_ref(artifact)], data={"artifact_id": artifact.artifact_id})


async def _exec_ag_save_json_artifact(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    payload = resolved_inputs.get("payload") or {}
    name = str(resolved_inputs.get("name") or "deeplens-data.json")
    artifact = await context.artifacts().save_json(payload, name=name)
    return success_result(tool_name="ag.save_json_artifact", summary=f"Saved JSON artifact `{name}`.", artifacts=[artifact_to_ref(artifact)], data={"artifact_id": artifact.artifact_id})


async def _exec_ag_send_image(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    url = str(resolved_inputs.get("url") or resolved_inputs.get("uri") or "")
    title = str(resolved_inputs.get("title") or resolved_inputs.get("name") or "image")
    if not url:
        return failure_result(tool_name="ag.send_image", summary="No image uri was provided.", error_code="missing_input")
    await context.channel("ui:session").send_image(url=url, alt=title, title=title, memory_log=False)
    return success_result(tool_name="ag.send_image", summary=f"Sent image `{title}` to the UI.")


async def _exec_ag_send_file(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    url = str(resolved_inputs.get("url") or resolved_inputs.get("uri") or "")
    filename = str(resolved_inputs.get("filename") or maybe_extract_filename(url) or "file.bin")
    if not url:
        return failure_result(tool_name="ag.send_file", summary="No file uri was provided.", error_code="missing_input")
    await context.channel("ui:session").send_file(url=url, filename=filename, title=str(resolved_inputs.get("title") or filename), memory_log=False)
    return success_result(tool_name="ag.send_file", summary=f"Sent file `{filename}` to the UI.")


GRAPH_INPUT_BUILDERS: dict[str, Any] = {
    "deeplens_v4_optimize_workflow": build_optimization_inputs,
}


async def _exec_ag_spawn_graph(*, resolved_inputs: dict[str, Any], task: TaskFrame, state: Any, context: Any, **_: Any) -> ToolResult:
    source = None
    if resolved_inputs.get("lens_source") or task.lens_source or state.active_source_ref:
        try:
            source = await resolve_lens_source(resolved_inputs=resolved_inputs, task=task, state=state, context=context)
        except Exception:
            logger.warning("ag.spawn_graph: resolve_lens_source failed, using fallback", exc_info=True)
            source = task.lens_source or state.active_source_ref
    if not source:
        return needs_input_result(
            tool_name="ag.spawn_graph",
            summary="I need a lens source before I can submit the optimization workflow.",
            missing_fields=["lens_source"],
        )
    graph_id = str(resolved_inputs.get("graph_id") or "deeplens_v4_optimize_workflow")
    builder = GRAPH_INPUT_BUILDERS.get(graph_id)
    graph_inputs = resolved_inputs.get("graph_inputs") or (
        builder(resolved_inputs=resolved_inputs, task=task, source=source, state=state) if builder else resolved_inputs
    )
    run_id = await context.runner().spawn_run(
        graph_id,
        inputs=graph_inputs,
        tags=["ag.deeplens.v4", f"shape:{task.task_shape.value}", f"domain:{task.domain_hint.value}"],
    )
    _record_pending_run(state, run_id, graph_id, meta={"source_action": "ag.spawn_graph"})
    return run_submitted_result(
        tool_name="ag.spawn_graph",
        summary=(
            f"Started background workflow `{graph_id}`.\n"
            f"Run ID: `{run_id}`.\n"
            "Use status or cancel if you want to check or stop it."
        ),
        run_id=run_id,
        data={"graph_id": graph_id, "graph_inputs": graph_inputs},
        state_updates={
            "active_run_id": run_id,
            "active_source_ref": source or state.active_source_ref,
            "requested_next_step": "status_or_cancel",
            "pending_runs": list(state.pending_runs),
        },
    )


async def _exec_ag_status(*, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    run_id = str(resolved_inputs.get("run_id") or state.active_run_id or "")
    if not run_id:
        return needs_input_result(tool_name="ag.status", summary="There is no active background run to check.", missing_fields=["run_id"])
    record, outputs = await context.runner().wait_run(run_id, timeout_s=float(resolved_inputs.get("timeout_s") or 1), return_outputs=True)
    status = str(getattr(record, "status", "unknown"))
    _update_pending_run_status(state, run_id, status)
    summary = summarize_status(record, outputs)
    result = run_updated_result(
        tool_name="ag.status",
        summary=summary,
        run_id=run_id,
        data={"record": {"status": status}, "outputs": outputs or {}},
        state_updates={"pending_runs": list(state.pending_runs)},
        raw_status=status,
    )
    if status in {"completed", "failed", "canceled"}:
        result.state_updates["active_run_id"] = None
    return result


async def _exec_ag_cancel(*, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    run_id = str(resolved_inputs.get("run_id") or state.active_run_id or "")
    if not run_id:
        return needs_input_result(tool_name="ag.cancel", summary="There is no active background run to cancel.", missing_fields=["run_id"])
    await context.runner().cancel_run(run_id)
    _update_pending_run_status(state, run_id, "cancel_requested")
    return run_updated_result(
        tool_name="ag.cancel",
        summary=f"Requested cancellation for run `{run_id}`.",
        run_id=run_id,
        state_updates={"pending_runs": list(state.pending_runs)},
        raw_status="cancel_requested",
    )


async def _exec_dl_load_lens(*, resolved_inputs: dict[str, Any], task: TaskFrame, state: Any, context: Any, **_: Any) -> ToolResult:
    try:
        source = await resolve_lens_source(resolved_inputs=resolved_inputs, task=task, state=state, context=context)
    except Exception as exc:
        logger.error("dl.load_lens failed", exc_info=True)
        return normalize_tool_result(tool_name="dl.load_lens", error=exc)
    return success_result(
        tool_name="dl.load_lens",
        summary=f"Loaded lens source `{source['name']}`.",
        state_updates={"active_source_ref": source},
    )


async def _exec_dl_analysis(*, resolved_inputs: dict[str, Any], task: TaskFrame, state: Any, context: Any, **_: Any) -> ToolResult:
    try:
        out = await run_analysis(resolved_inputs=resolved_inputs, task=task, state=state, context=context)
    except Exception as exc:
        logger.error("dl.analysis failed", exc_info=True)
        return normalize_tool_result(tool_name="dl.analysis", error=exc)
    artifact_names = ", ".join(summarize_artifacts(out["artifacts"]))
    return success_result(
        tool_name="dl.analysis",
        summary=f"{out['summary']}\nArtifacts: {artifact_names}",
        artifacts=out["artifacts"],
        data={"metrics": out["metrics"]},
        state_updates={
            "active_lens_ref": out.get("active_lens_ref"),
            "active_source_ref": out.get("active_source_ref"),
            "last_analysis_bundle": {"summary": out["summary"], "metrics": out["metrics"], "artifacts": out["artifacts"]},
            "last_metrics": out["metrics"],
            "requested_next_step": "interpret_or_export",
        },
        recommended_next_actions=["export", "optimize"],
    )


async def _exec_dl_create_lens(*, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    spec = resolved_inputs.get("design_spec") or {}
    missing = missing_design_fields(spec)
    if missing:
        return needs_input_result(
            tool_name="dl.create_lens",
            summary=f"Missing required design fields: {', '.join(missing)}.",
            missing_fields=missing,
        )
    try:
        created = await create_lens_design(resolved_inputs=resolved_inputs, context=context)
    except Exception as exc:
        logger.error("dl.create_lens failed", exc_info=True)
        return normalize_tool_result(tool_name="dl.create_lens", error=exc)
    persisted = []
    for path in sorted(created["result_dir"].rglob("*")):
        if not path.is_file():
            continue
        artifact = await context.artifacts().save_file(
            path=str(path),
            kind="image" if path.suffix.lower() in {".png", ".jpg", ".jpeg"} else ("json" if path.suffix.lower() == ".json" else "file"),
            name=path.name,
            labels={"tag": "design"},
        )
        persisted.append(artifact_to_ref(artifact))
    await deliver_artifacts(artifacts=persisted, context=context)
    active_lens_ref = next((artifact.get("artifact_id") for artifact in persisted if str(artifact.get("name")).endswith(".json")), None)
    active_source_ref = next((artifact for artifact in persisted if str(artifact.get("name")).endswith(".json")), {})
    return success_result(
        tool_name="dl.create_lens",
        summary=f"Created a new DeepLens design.\nArtifacts: {', '.join(summarize_artifacts(persisted))}",
        artifacts=persisted,
        data={"design_spec": spec},
        state_updates={
            "active_lens_ref": active_lens_ref,
            "active_source_ref": active_source_ref,
            "design_draft": spec,
            "requested_next_step": "analyze_or_optimize",
        },
        recommended_next_actions=["analyze", "optimize", "export"],
    )


async def _exec_dl_export_lens(*, resolved_inputs: dict[str, Any], task: TaskFrame, state: Any, context: Any, **_: Any) -> ToolResult:
    try:
        out = await export_lens(resolved_inputs=resolved_inputs, task=task, state=state, context=context)
    except Exception as exc:
        logger.error("dl.export_lens failed", exc_info=True)
        return normalize_tool_result(tool_name="dl.export_lens", error=exc)
    return success_result(
        tool_name="dl.export_lens",
        summary=f"{out['summary']}\nArtifacts: {', '.join(summarize_artifacts(out['artifacts']))}",
        artifacts=out["artifacts"],
        state_updates={
            "active_lens_ref": out.get("active_lens_ref"),
            "active_source_ref": out.get("active_source_ref"),
            "requested_next_step": "share_or_optimize",
        },
        recommended_next_actions=["optimize", "new_design"],
    )


EXECUTOR_MAP = {
    "ag.get_latest_uploads": _exec_ag_get_latest_uploads,
    "ag.load_artifact_text_or_json": _exec_ag_load_artifact_text_or_json,
    "ag.save_text_artifact": _exec_ag_save_text_artifact,
    "ag.save_json_artifact": _exec_ag_save_json_artifact,
    "ag.send_image": _exec_ag_send_image,
    "ag.send_file": _exec_ag_send_file,
    "ag.spawn_graph": _exec_ag_spawn_graph,
    "ag.status": _exec_ag_status,
    "ag.cancel": _exec_ag_cancel,
    "dl.load_lens": _exec_dl_load_lens,
    "dl.analysis": _exec_dl_analysis,
    "dl.create_lens": _exec_dl_create_lens,
    "dl.export_lens": _exec_dl_export_lens,
}


async def dispatch_execution(
    *,
    execution: ExecutionRequest,
    task: TaskFrame,
    state: Any,
    context_bundle: Any,
    context: Any,
) -> ToolResult:
    tool_name = execution.selected_tool or ""
    spec = get_tool_spec(tool_name)

    approved = await _maybe_approval(spec, execution, state, context)
    if not approved:
        state.approved_action = None
        return run_updated_result(
            tool_name=tool_name,
            summary=f"Skipped `{tool_name}` because approval was not granted.",
            run_id=state.active_run_id,
            raw_status="approval_rejected",
        )

    resolved_inputs = _base_inputs(execution, task, state)
    try:
        result = await EXECUTOR_MAP[spec.executor_key](
            resolved_inputs=resolved_inputs,
            task=task,
            state=state,
            context_bundle=context_bundle,
            context=context,
        )
    except Exception as exc:
        logger.error("Unhandled error in tool executor %s", tool_name, exc_info=True)
        result = normalize_tool_result(tool_name=tool_name, error=exc)
    state.approved_action = None
    await _record_tool_memory(context, tool_name, resolved_inputs, result)
    return result
