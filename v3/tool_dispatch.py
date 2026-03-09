from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("ag.deeplens.v3.tool_dispatch")

from .backend import (
    _artifact_to_ref,
    build_optimization_inputs,
    create_lens_design,
    deliver_artifacts,
    export_lens,
    guess_mime,
    load_text_or_json_payload,
    maybe_extract_filename,
    missing_design_fields,
    resolve_lens_source,
    run_analysis,
    summarize_artifacts,
    summarize_status,
)
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
        tags=["ag.deeplens.v3.tool", f"ag.deeplens.v3.action:{tool_name}"],
    )


async def _maybe_approval(spec: Any, action: dict[str, Any], state: Any, context: Any) -> bool:
    if spec.approval_level == ApprovalLevel.NONE:
        return True
    if state.approved_action == spec.name:
        state.approved_action = None
        return True
    # HARD approval is handled by the loop engine's request_approval step;
    # reaching here means the loop already secured approval or skipped it.
    if spec.approval_level == ApprovalLevel.HARD:
        return True
    # SOFT approval: prompt inline at dispatch level.
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
    args.setdefault("attachments", task.attachments)
    args.setdefault("source_refs", task.source_refs)
    args.setdefault("active_lens_ref", state.active_lens_ref)
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
        context_bundle=context_bundle,
        context=context,
    )


async def _exec_ag_get_latest_uploads(*, context: Any, **_: Any) -> ToolResult:
    uploads = await context.channel("ui:session").get_latest_uploads(clear=False)
    names = [u.get("name") or u.get("filename") or u.get("uri") or "upload" for u in uploads]
    return ToolResult(
        ok=True,
        summary=f"Found {len(uploads)} uploaded file(s).",
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
        return ToolResult(ok=False, summary="No artifact id or uri was provided.", status="failed")
    payload = load_text_or_json_payload(raw_text)
    return ToolResult(
        ok=True,
        summary="Loaded artifact payload.",
        data={"payload": payload},
    )


async def _exec_ag_save_text_artifact(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    payload = str(resolved_inputs.get("payload") or resolved_inputs.get("text") or "")
    name = str(resolved_inputs.get("name") or "deeplens-note.txt")
    artifact = await context.artifacts().save_text(payload, name=name)
    return ToolResult(
        ok=True,
        summary=f"Saved text artifact `{name}`.",
        artifacts=[_artifact_to_ref(artifact)],
        data={"artifact_id": artifact.artifact_id},
    )


async def _exec_ag_save_json_artifact(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    payload = resolved_inputs.get("payload") or {}
    name = str(resolved_inputs.get("name") or "deeplens-data.json")
    artifact = await context.artifacts().save_json(payload, name=name)
    return ToolResult(
        ok=True,
        summary=f"Saved JSON artifact `{name}`.",
        artifacts=[_artifact_to_ref(artifact)],
        data={"artifact_id": artifact.artifact_id},
    )


async def _exec_ag_send_image(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    url = str(resolved_inputs.get("url") or resolved_inputs.get("uri") or "")
    title = str(resolved_inputs.get("title") or resolved_inputs.get("name") or "image")
    if not url:
        return ToolResult(ok=False, summary="No image uri was provided.", status="failed")
    await context.channel("ui:session").send_image(
        url=url,
        alt=title,
        title=title,
        memory_log=False,
    )
    return ToolResult(ok=True, summary=f"Sent image `{title}` to the UI.")


async def _exec_ag_send_file(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    url = str(resolved_inputs.get("url") or resolved_inputs.get("uri") or "")
    filename = str(resolved_inputs.get("filename") or maybe_extract_filename(url) or "file.bin")
    if not url:
        return ToolResult(ok=False, summary="No file uri was provided.", status="failed")
    await context.channel("ui:session").send_file(
        url=url,
        filename=filename,
        title=str(resolved_inputs.get("title") or filename),
        memory_log=False,
    )
    return ToolResult(ok=True, summary=f"Sent file `{filename}` to the UI.")


# Registry of input builders keyed by graph_id. Allows ag.spawn_graph to
# work with multiple workflow types without hardcoding.
GRAPH_INPUT_BUILDERS: dict[str, Any] = {
    "deeplens_v3_optimize_workflow": build_optimization_inputs,
}


async def _exec_ag_spawn_graph(*, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any, **_: Any) -> ToolResult:
    source = None
    if resolved_inputs.get("lens_source") or task.lens_source or state.active_source_ref:
        try:
            source = await resolve_lens_source(
                resolved_inputs=resolved_inputs,
                task=task,
                state=state,
                context=context,
            )
        except Exception:
            logger.warning("ag.spawn_graph: resolve_lens_source failed, using fallback", exc_info=True)
            source = task.lens_source or state.active_source_ref

    graph_id = str(resolved_inputs.get("graph_id") or "deeplens_v3_optimize_workflow")
    # Use a registered input builder if available, otherwise pass resolved inputs directly.
    builder = GRAPH_INPUT_BUILDERS.get(graph_id)
    graph_inputs = resolved_inputs.get("graph_inputs") or (
        builder(resolved_inputs=resolved_inputs, task=task, source=source, state=state)
        if builder
        else resolved_inputs
    )
    run_id = await context.runner().spawn_run(
        graph_id,
        inputs=graph_inputs,
        tags=["ag.deeplens.v3", f"shape:{task.task_shape.value}", f"domain:{task.domain_hint.value}"],
    )
    _record_pending_run(state, run_id, graph_id, meta={"source_action": "ag.spawn_graph"})
    return ToolResult(
        ok=True,
        status="submitted",
        summary=(
            f"Started background workflow `{graph_id}`.\n"
            f"Run ID: `{run_id}`.\n"
            "Use status or cancel if you want to check or stop it."
        ),
        run_id=run_id,
        data={
            "graph_id": graph_id,
            "graph_inputs": graph_inputs,
            "state_updates": {
                "requested_next_step": "status_or_cancel",
                "active_source_ref": source or state.active_source_ref,
            },
        },
        should_end_turn=True,
    )


async def _exec_ag_status(*, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    run_id = str(resolved_inputs.get("run_id") or state.active_run_id or "")
    if not run_id:
        return ToolResult(ok=False, summary="There is no active background run to check.", status="failed")
    record, outputs = await context.runner().wait_run(
        run_id,
        timeout_s=float(resolved_inputs.get("timeout_s") or 1),
        return_outputs=True,
    )
    status = str(getattr(record, "status", "unknown"))
    _update_pending_run_status(state, run_id, status)
    summary = summarize_status(record, outputs)
    return ToolResult(
        ok=True,
        summary=summary,
        data={"record": {"status": status}, "outputs": outputs or {}},
        run_id=run_id,
    )


async def _exec_ag_cancel(*, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    run_id = str(resolved_inputs.get("run_id") or state.active_run_id or "")
    if not run_id:
        return ToolResult(ok=False, summary="There is no active background run to cancel.", status="failed")
    await context.runner().cancel_run(run_id)
    _update_pending_run_status(state, run_id, "cancel_requested")
    return ToolResult(
        ok=True,
        status="cancel_requested",
        summary=f"Requested cancellation for run `{run_id}`.",
        run_id=run_id,
        should_end_turn=True,
    )


async def _exec_dl_load_lens(*, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any, **_: Any) -> ToolResult:
    try:
        source = await resolve_lens_source(
            resolved_inputs=resolved_inputs,
            task=task,
            state=state,
            context=context,
        )
    except Exception as exc:
        logger.error("dl.load_lens failed", exc_info=True)
        return ToolResult(ok=False, summary=str(exc), status="failed")
    summary = f"Loaded lens source `{source['name']}`."
    return ToolResult(
        ok=True,
        summary=summary,
        data={"state_updates": {"active_source_ref": source}},
    )


async def _exec_dl_analysis(*, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any, **_: Any) -> ToolResult:
    try:
        out = await run_analysis(
            resolved_inputs=resolved_inputs,
            task=task,
            state=state,
            context=context,
        )
    except Exception as exc:
        logger.error("dl.analysis failed", exc_info=True)
        return ToolResult(ok=False, summary=f"Analysis failed: {exc}", status="failed")
    artifact_names = ", ".join(summarize_artifacts(out["artifacts"]))
    return ToolResult(
        ok=True,
        summary=f"{out['summary']}\nArtifacts: {artifact_names}",
        artifacts=out["artifacts"],
        data={
            "metrics": out["metrics"],
            "state_updates": {
                "active_lens_ref": out.get("active_lens_ref"),
                "active_source_ref": out.get("active_source_ref"),
                "last_analysis_bundle": {
                    "summary": out["summary"],
                    "metrics": out["metrics"],
                    "artifacts": out["artifacts"],
                },
                "last_metrics": out["metrics"],
                "requested_next_step": "interpret_or_export",
            },
        },
    )


async def _exec_dl_create_lens(*, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    spec = resolved_inputs.get("design_spec") or {}
    missing = missing_design_fields(spec)
    if missing:
        return ToolResult(
            ok=False,
            summary=f"Missing required design fields: {', '.join(missing)}.",
            status="failed",
        )
    try:
        created = await create_lens_design(resolved_inputs=resolved_inputs, context=context)
    except Exception as exc:
        logger.error("dl.create_lens failed", exc_info=True)
        return ToolResult(ok=False, summary=f"Lens creation failed: {exc}", status="failed")

    result_dir = created["result_dir"]
    artifacts = []
    for path in sorted(result_dir.rglob("*")):
        if not path.is_file():
            continue
        artifact = await context.artifacts().save_file(
            path=str(path),
            kind="image" if path.suffix.lower() in {".png", ".jpg", ".jpeg"} else ("json" if path.suffix.lower() == ".json" else "file"),
            name=path.name,
            labels={"tag": "design"},
        )
        artifacts.append(_artifact_to_ref(artifact))
    await deliver_artifacts(artifacts=artifacts, context=context)
    active_lens_ref = next(
        (artifact.get("artifact_id") for artifact in artifacts if str(artifact.get("name")).endswith(".json")),
        None,
    )
    active_source_ref = next(
        (artifact for artifact in artifacts if str(artifact.get("name")).endswith(".json")),
        {},
    )
    return ToolResult(
        ok=True,
        summary=f"Created a new DeepLens design from the provided spec.\nArtifacts: {', '.join(summarize_artifacts(artifacts))}",
        artifacts=artifacts,
        data={
            "design_spec": spec,
            "state_updates": {
                "active_lens_ref": active_lens_ref,
                "active_source_ref": active_source_ref,
                "design_draft": spec,
                "requested_next_step": "analyze_or_optimize",
            },
        },
    )


async def _exec_dl_export_lens(*, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any, **_: Any) -> ToolResult:
    try:
        out = await export_lens(
            resolved_inputs=resolved_inputs,
            task=task,
            state=state,
            context=context,
        )
    except Exception as exc:
        logger.error("dl.export_lens failed", exc_info=True)
        return ToolResult(ok=False, summary=f"Export failed: {exc}", status="failed")
    return ToolResult(
        ok=True,
        summary=f"{out['summary']}\nArtifacts: {', '.join(summarize_artifacts(out['artifacts']))}",
        artifacts=out["artifacts"],
        data={
            "state_updates": {
                "active_lens_ref": out.get("active_lens_ref"),
                "active_source_ref": out.get("active_source_ref"),
                "requested_next_step": "share_or_optimize",
            },
        },
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
        return
    for key, value in updates.items():
        if value is not None and hasattr(state, key):
            setattr(state, key, value)


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

    approved = await _maybe_approval(spec, action, state, context)
    if not approved:
        state.approved_action = None
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
        result = ToolResult(
            ok=False,
            summary=f"`{tool_name}` failed: {exc}",
            status="failed",
            error_code="unhandled_executor_error",
        )
    if result.artifacts:
        _append_artifacts(state, result.artifacts)
    _apply_state_updates(state, result)
    state.approved_action = None
    await _record_tool_memory(context, tool_name, resolved_inputs, result)
    return result
