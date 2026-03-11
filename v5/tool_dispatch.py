from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger("ag.deeplens.v5.tool_dispatch")

from .backend import (
    _artifact_to_ref,
    build_optimization_inputs,
    create_lens_design,
    deliver_artifacts,
    export_lens,
    load_text_or_json_payload,
    maybe_extract_filename,
    missing_design_fields,
    persist_output_files,
    resolve_lens_source,
    run_analysis,
    summarize_artifacts,
    summarize_status,
)
from .tool_arg_refine import maybe_refine_tool_inputs_with_llm
from .tool_registry import get_tool_spec
from .types import ApprovalLevel, ToolResult


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_artifacts(state: Any, artifacts: list[dict[str, Any]]) -> None:
    if not artifacts:
        return
    state.last_artifacts.extend(artifacts)
    state.last_artifacts = state.last_artifacts[-20:]


def _record_pending_run(state: Any, run_id: str, graph_id: str, meta: dict[str, Any] | None = None) -> None:
    rec = {
        "run_id": run_id,
        "graph_id": graph_id,
        "status": "submitted",
        "cancelable": True,
        "submitted_at": _utc_now_iso(),
        "meta": meta or {},
    }
    state.pending_runs.append(rec)
    state.pending_runs = state.pending_runs[-20:]
    state.active_run_id = run_id
    state.pending_action = "run_control"


def _update_pending_run_status(state: Any, run_id: str, status: str) -> None:
    for rec in state.pending_runs:
        if rec.get("run_id") == run_id:
            rec["status"] = status
    if status in {"completed", "failed", "canceled", "cancelled"} and state.active_run_id == run_id:
        state.active_run_id = None
        state.pending_action = None


async def _record_tool_memory(context: Any, tool_name: str, inputs: dict[str, Any], result: ToolResult) -> None:
    try:
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
            tags=["ag.deeplens.v5.tool", f"ag.deeplens.v5.action:{tool_name}"],
        )
    except Exception:
        context.logger().warning("deeplens_v5: record_tool_result failed", exc_info=True)


async def _maybe_approval(spec: Any, override_args: dict[str, Any], state: Any, context: Any) -> bool:
    if spec.approval_level == ApprovalLevel.NONE:
        return True
    if state.approved_action == spec.name:
        state.approved_action = None
        return True
    if spec.approval_level == ApprovalLevel.HARD:
        return True
    prompt = override_args.get("approval_prompt") or f"Approve `{spec.name}`?"
    resp = await context.channel("ui:session").ask_approval(prompt=prompt, options=["Approve", "Cancel"])
    if isinstance(resp, dict):
        return bool(resp.get("approved"))
    return bool(getattr(resp, "approved", False))


def _make_context_bundle(task: Any, state: Any) -> Any:
    return SimpleNamespace(
        working_state={
            "task_shape": task.task_shape.value,
            "domain_hint": task.domain_hint.value,
            "preferred_tool": task.preferred_tool,
            "task_notes": task.notes[-8:],
            "lens_source": task.lens_source,
            "design_spec": task.design_spec,
            "analysis_request": task.analysis_request,
            "run_request": task.run_request,
            "delivery_request": task.delivery_request,
            "source_refs": task.source_refs[-4:],
            "active_artifact_refs": task.active_artifact_refs[-8:],
            "missing_fields": task.missing_fields,
            "active_run_id": state.active_run_id,
            "active_lens_ref": state.active_lens_ref,
            "active_source_ref": state.active_source_ref,
            "last_metrics": state.last_metrics,
            "last_analysis_bundle": state.last_analysis_bundle,
            "design_draft": state.design_draft,
            "last_artifacts": state.last_artifacts[-4:],
            "pending_runs": state.pending_runs[-5:],
            "pending_action": state.pending_action,
            "pending_approval": state.pending_approval,
            "retry_counters": state.retry_counters,
        }
    )


async def _resolve_inputs(
    *,
    spec: Any,
    tool_name: str,
    override_args: dict[str, Any],
    task: Any,
    state: Any,
    context: Any,
) -> dict[str, Any]:
    args = dict(override_args or {})
    args.setdefault("task_shape", task.task_shape.value)
    args.setdefault("domain_hint", task.domain_hint.value)
    args.setdefault("user_goal", task.user_goal)
    args.setdefault("missing_fields", task.missing_fields)
    args.setdefault("attachments", task.attachments)
    args.setdefault("source_refs", task.source_refs)
    args.setdefault("active_artifact_refs", task.active_artifact_refs)
    args.setdefault("active_source_ref", state.active_source_ref)
    args.setdefault("run_id", state.active_run_id)
    args.setdefault("use_stub", bool(task.run_request.get("use_stub")))
    args.setdefault("lens_source", task.lens_source or state.active_source_ref)
    args.setdefault("design_spec", task.design_spec or state.design_draft)
    args.setdefault("analysis_request", task.analysis_request)
    args.setdefault("run_request", task.run_request)
    args.setdefault("delivery_request", task.delivery_request)
    return await maybe_refine_tool_inputs_with_llm(
        spec=spec,
        draft_inputs=args,
        task=task,
        state=state,
        context_bundle=_make_context_bundle(task, state),
        context=context,
    )


async def _exec_ag_get_latest_uploads(*, context: Any, **_: Any) -> ToolResult:
    uploads = await context.channel("ui:session").get_latest_uploads(clear=False)
    names = [u.get("name") or u.get("filename") or u.get("uri") or "upload" for u in uploads]
    return ToolResult(
        ok=True,
        tool_name="ag.get_latest_uploads",
        summary=f"Found {len(uploads)} uploaded file(s).",
        outcome_type="success",
        data={"uploads": uploads, "names": names},
    )


async def _exec_ag_load_artifact_text_or_json(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    artifact_id = resolved_inputs.get("artifact_id")
    uri = resolved_inputs.get("uri")
    if artifact_id:
        raw_text = await context.artifacts().load_text_by_id(str(artifact_id))
    elif uri:
        raw_text = await context.artifacts().load_text(str(uri))
    else:
        return ToolResult(ok=False, tool_name="ag.load_artifact_text_or_json", summary="No artifact id or uri was provided.", outcome_type="failed", status="failed")
    payload = load_text_or_json_payload(raw_text)
    return ToolResult(ok=True, tool_name="ag.load_artifact_text_or_json", summary="Loaded artifact payload.", outcome_type="success", data={"payload": payload})


async def _exec_ag_save_text_artifact(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    payload = str(resolved_inputs.get("payload") or resolved_inputs.get("text") or "")
    name = str(resolved_inputs.get("name") or "deeplens-note.txt")
    artifact = await context.artifacts().save_text(payload, name=name)
    return ToolResult(ok=True, tool_name="ag.save_text_artifact", summary=f"Saved text artifact `{name}`.", outcome_type="success", artifacts=[_artifact_to_ref(artifact)], data={"artifact_id": artifact.artifact_id})


async def _exec_ag_save_json_artifact(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    payload = resolved_inputs.get("payload") or {}
    name = str(resolved_inputs.get("name") or "deeplens-data.json")
    artifact = await context.artifacts().save_json(payload, name=name)
    return ToolResult(ok=True, tool_name="ag.save_json_artifact", summary=f"Saved JSON artifact `{name}`.", outcome_type="success", artifacts=[_artifact_to_ref(artifact)], data={"artifact_id": artifact.artifact_id})


async def _exec_ag_send_image(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    url = str(resolved_inputs.get("url") or resolved_inputs.get("uri") or "")
    title = str(resolved_inputs.get("title") or resolved_inputs.get("name") or "image")
    if not url:
        return ToolResult(ok=False, tool_name="ag.send_image", summary="No image uri was provided.", outcome_type="failed", status="failed")
    await context.channel("ui:session").send_image(url=url, alt=title, title=title, memory_log=False)
    return ToolResult(ok=True, tool_name="ag.send_image", summary=f"Sent image `{title}` to the UI.", outcome_type="success")


async def _exec_ag_send_file(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    url = str(resolved_inputs.get("url") or resolved_inputs.get("uri") or "")
    filename = str(resolved_inputs.get("filename") or maybe_extract_filename(url) or "file.bin")
    if not url:
        return ToolResult(ok=False, tool_name="ag.send_file", summary="No file uri was provided.", outcome_type="failed", status="failed")
    await context.channel("ui:session").send_file(url=url, filename=filename, title=str(resolved_inputs.get("title") or filename), memory_log=False)
    return ToolResult(ok=True, tool_name="ag.send_file", summary=f"Sent file `{filename}` to the UI.", outcome_type="success")


GRAPH_INPUT_BUILDERS: dict[str, Any] = {
    "deeplens_v3_optimize_workflow": build_optimization_inputs,
}


async def _exec_ag_spawn_graph(*, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any, **_: Any) -> ToolResult:
    source = None
    if resolved_inputs.get("lens_source") or task.lens_source or state.active_source_ref:
        try:
            source = await resolve_lens_source(resolved_inputs=resolved_inputs, task=task, state=state, context=context)
        except Exception:
            logger.warning("ag.spawn_graph: resolve_lens_source failed, using fallback", exc_info=True)
            source = task.lens_source or state.active_source_ref
    graph_id = str(resolved_inputs.get("graph_id") or "deeplens_v3_optimize_workflow")
    builder = GRAPH_INPUT_BUILDERS.get(graph_id)
    graph_inputs = resolved_inputs.get("graph_inputs") or (
        builder(resolved_inputs=resolved_inputs, task=task, source=source, state=state) if builder else resolved_inputs
    )
    run_id = await context.runner().spawn_run(
        graph_id,
        inputs=graph_inputs,
        tags=["ag.deeplens.v5", f"shape:{task.task_shape.value}", f"domain:{task.domain_hint.value}"],
    )
    _record_pending_run(state, run_id, graph_id, meta={"source_action": "ag.spawn_graph"})
    return ToolResult(
        ok=True,
        tool_name="ag.spawn_graph",
        status="submitted",
        summary=f"Started background workflow `{graph_id}`.\nRun ID: `{run_id}`.\nUse status or cancel if you want to check or stop it.",
        outcome_type="run_submitted",
        run_id=run_id,
        data={"graph_id": graph_id, "graph_inputs": graph_inputs},
        state_updates={
            "requested_next_step": "status_or_cancel",
            "active_source_ref": source or state.active_source_ref,
        },
        recommended_next_actions=["Background run submitted. Ask for status to check progress or cancel to stop it."],
        should_end_turn=True,
    )


async def _exec_ag_status(*, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    run_id = str(resolved_inputs.get("run_id") or state.active_run_id or "")
    if not run_id:
        return ToolResult(ok=False, tool_name="ag.status", summary="There is no active background run to check.", outcome_type="failed", status="failed", missing_fields=["run_id"], needs_input=True)
    record, outputs = await context.runner().wait_run(run_id, timeout_s=float(resolved_inputs.get("timeout_s") or 1), return_outputs=True)
    status = str(getattr(record, "status", "unknown"))
    _update_pending_run_status(state, run_id, status)
    summary = summarize_status(record, outputs)
    return ToolResult(ok=True, tool_name="ag.status", summary=summary, outcome_type="run_updated", data={"record": {"status": status}, "outputs": outputs or {}}, run_id=run_id)


async def _exec_ag_cancel(*, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    run_id = str(resolved_inputs.get("run_id") or state.active_run_id or "")
    if not run_id:
        return ToolResult(ok=False, tool_name="ag.cancel", summary="There is no active background run to cancel.", outcome_type="failed", status="failed", missing_fields=["run_id"], needs_input=True)
    await context.runner().cancel_run(run_id)
    _update_pending_run_status(state, run_id, "cancel_requested")
    return ToolResult(ok=True, tool_name="ag.cancel", status="cancel_requested", summary=f"Requested cancellation for run `{run_id}`.", outcome_type="success", run_id=run_id, should_end_turn=True)


async def _exec_dl_load_lens(*, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any, **_: Any) -> ToolResult:
    try:
        source = await resolve_lens_source(resolved_inputs=resolved_inputs, task=task, state=state, context=context)
    except Exception as exc:
        logger.error("dl.load_lens failed", exc_info=True)
        return ToolResult(ok=False, tool_name="dl.load_lens", summary=str(exc), outcome_type="failed", status="failed")
    return ToolResult(ok=True, tool_name="dl.load_lens", summary=f"Loaded lens source `{source['name']}`.", outcome_type="success", state_updates={"active_source_ref": source})


async def _exec_dl_analysis(*, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any, **_: Any) -> ToolResult:
    try:
        out = await run_analysis(resolved_inputs=resolved_inputs, task=task, state=state, context=context)
    except Exception as exc:
        logger.error("dl.analysis failed", exc_info=True)
        return ToolResult(ok=False, tool_name="dl.analysis", summary=f"Analysis failed: {exc}", outcome_type="failed", status="failed")
    artifact_names = ", ".join(summarize_artifacts(out["artifacts"]))
    return ToolResult(
        ok=True,
        tool_name="dl.analysis",
        summary=f"{out['summary']}\nArtifacts: {artifact_names}",
        outcome_type="success",
        artifacts=out["artifacts"],
        data={"metrics": out["metrics"]},
        state_updates={
            "active_lens_ref": out.get("active_lens_ref"),
            "active_source_ref": out.get("active_source_ref"),
            "last_analysis_bundle": {"summary": out["summary"], "metrics": out["metrics"], "artifacts": out["artifacts"]},
            "last_metrics": out["metrics"],
            "requested_next_step": "interpret_or_export",
        },
        recommended_next_actions=["Analysis complete. You can review the results, export the lens, or start optimization."],
    )


async def _exec_dl_create_lens(*, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    spec = resolved_inputs.get("design_spec") or {}
    missing = missing_design_fields(spec)
    if missing:
        return ToolResult(ok=False, tool_name="dl.create_lens", summary=f"Missing required design fields: {', '.join(missing)}.", outcome_type="failed", status="failed", missing_fields=missing, needs_input=True)
    try:
        created = await create_lens_design(resolved_inputs=resolved_inputs, context=context)
    except Exception as exc:
        logger.error("dl.create_lens failed", exc_info=True)
        return ToolResult(ok=False, tool_name="dl.create_lens", summary=f"Lens creation failed: {exc}", outcome_type="failed", status="failed")
    result_dir = created["result_dir"]
    artifacts = await persist_output_files(result_dir=result_dir, context=context, tag="design")
    await deliver_artifacts(artifacts=artifacts, context=context)
    active_lens_ref = next((artifact.get("artifact_id") for artifact in artifacts if str(artifact.get("name")).endswith(".json")), None)
    active_source_ref = next((artifact for artifact in artifacts if str(artifact.get("name")).endswith(".json")), {})
    return ToolResult(
        ok=True,
        tool_name="dl.create_lens",
        summary=f"Created a new DeepLens design from the provided spec.\nArtifacts: {', '.join(summarize_artifacts(artifacts))}",
        outcome_type="success",
        artifacts=artifacts,
        data={"design_spec": spec},
        state_updates={
            "active_lens_ref": active_lens_ref,
            "active_source_ref": active_source_ref,
            "design_draft": spec,
            "requested_next_step": "analyze_or_optimize",
        },
        recommended_next_actions=["Design created. You can ask me to analyze it, export it, or optimize it."],
    )


async def _exec_dl_export_lens(*, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any, **_: Any) -> ToolResult:
    try:
        out = await export_lens(resolved_inputs=resolved_inputs, task=task, state=state, context=context)
    except Exception as exc:
        logger.error("dl.export_lens failed", exc_info=True)
        return ToolResult(ok=False, tool_name="dl.export_lens", summary=f"Export failed: {exc}", outcome_type="failed", status="failed")
    return ToolResult(
        ok=True,
        tool_name="dl.export_lens",
        summary=f"{out['summary']}\nArtifacts: {', '.join(summarize_artifacts(out['artifacts']))}",
        outcome_type="success",
        artifacts=out["artifacts"],
        state_updates={
            "active_lens_ref": out.get("active_lens_ref"),
            "active_source_ref": out.get("active_source_ref"),
            "requested_next_step": "share_or_optimize",
        },
        recommended_next_actions=["Export complete. You can optimize the lens or start a new design."],
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


def _apply_state_updates(state: Any, result: ToolResult) -> None:
    updates = result.data.get("state_updates") if isinstance(result.data, dict) else None
    if not isinstance(updates, dict):
        updates = {}
    merged = dict(updates)
    merged.update(result.state_updates or {})
    for key, value in merged.items():
        if value is not None and hasattr(state, key):
            setattr(state, key, value)


async def dispatch_tool_action(
    *,
    tool_name: str,
    task: Any,
    state: Any,
    context: Any,
    override_args: dict[str, Any] | None = None,
) -> ToolResult:
    spec = get_tool_spec(tool_name)
    args = dict(override_args or {})
    approved = await _maybe_approval(spec, args, state, context)
    if not approved:
        state.approved_action = None
        return ToolResult(
            ok=True,
            tool_name=tool_name,
            status="canceled",
            summary=f"Skipped `{tool_name}` because approval was not granted.",
            outcome_type="noop",
            should_end_turn=True,
        )

    resolved_inputs = await _resolve_inputs(
        spec=spec,
        tool_name=tool_name,
        override_args=args,
        task=task,
        state=state,
        context=context,
    )
    try:
        result = await EXECUTOR_MAP[spec.executor_key](
            resolved_inputs=resolved_inputs,
            task=task,
            state=state,
            context=context,
        )
    except Exception as exc:
        logger.error("Unhandled error in tool executor %s", tool_name, exc_info=True)
        result = ToolResult(
            ok=False,
            tool_name=tool_name,
            summary=f"`{tool_name}` failed: {exc}",
            outcome_type="failed",
            status="failed",
            error_code="unhandled_executor_error",
        )
    # Note: artifact merging and state updates are handled by
    # tool_results.apply_tool_result_to_state() in the loop engine.
    # Do not duplicate them here.
    state.approved_action = None
    await _record_tool_memory(context, tool_name, resolved_inputs, result)
    return result
