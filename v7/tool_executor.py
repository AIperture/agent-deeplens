from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .backend import (
    _artifact_to_ref,
    build_optimization_inputs,
    create_lens_design,
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
from .types import ErrorType, RuntimeState, ToolResult


logger = logging.getLogger("ag.deeplens.v7.tool_executor")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_artifacts(state: RuntimeState, artifacts: list[dict[str, Any]]) -> None:
    if not artifacts:
        return
    state.last_artifacts.extend(artifacts)
    state.last_artifacts = state.last_artifacts[-20:]


def _record_pending_run(state: RuntimeState, run_id: str, graph_id: str, meta: dict[str, Any] | None = None) -> None:
    state.pending_runs.append(
        {
            "run_id": run_id,
            "graph_id": graph_id,
            "status": "submitted",
            "submitted_at": _utc_now_iso(),
            "meta": meta or {},
        }
    )
    state.pending_runs = state.pending_runs[-20:]
    state.active_run_id = run_id


def _update_pending_run_status(state: RuntimeState, run_id: str, status: str) -> None:
    for record in state.pending_runs:
        if record.get("run_id") == run_id:
            record["status"] = status
    if status in {"completed", "failed", "canceled"} and state.active_run_id == run_id:
        state.active_run_id = None


async def _record_tool_memory(context: Any, tool_name: str, inputs: dict[str, Any], result: ToolResult) -> None:
    await context.memory().record_tool_result(
        tool=tool_name,
        inputs=[inputs],
        outputs=[
            {
                "ok": result.ok,
                "summary": result.summary,
                "structured_output": result.structured_output,
                "artifacts": result.artifacts,
                "error_type": result.error_type,
                "error_message": result.error_message,
            }
        ],
        message=result.summary,
        tags=["ag.deeplens.v7.tool", f"ag.deeplens.v7.action:{tool_name}"],
    )


async def _exec_ag_get_latest_uploads(*, context: Any, **_: Any) -> ToolResult:
    uploads = await context.channel("ui:session").get_latest_uploads(clear=False)
    names = [u.get("name") or u.get("filename") or u.get("uri") or "upload" for u in uploads]
    return ToolResult(
        ok=True,
        tool_name="ag.get_latest_uploads",
        summary=f"Found {len(uploads)} uploaded file(s).",
        structured_output={"uploads": uploads, "names": names},
    )


async def _exec_ag_load_artifact_text_or_json(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    artifact_id = resolved_inputs.get("artifact_id")
    uri = resolved_inputs.get("uri")
    if artifact_id:
        raw_text = await context.artifacts().load_text_by_id(str(artifact_id))
    elif uri:
        raw_text = await context.artifacts().load_text(str(uri))
    else:
        return ToolResult(
            ok=False,
            tool_name="ag.load_artifact_text_or_json",
            summary="No artifact id or uri was provided.",
            error_type=ErrorType.MISSING_INPUT.value,
            error_message="Missing artifact id or uri.",
        )
    return ToolResult(
        ok=True,
        tool_name="ag.load_artifact_text_or_json",
        summary="Loaded artifact payload.",
        structured_output={"payload": load_text_or_json_payload(raw_text)},
    )


async def _exec_ag_save_text_artifact(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    payload = str(resolved_inputs.get("payload") or "")
    name = str(resolved_inputs.get("name") or "deeplens-note.txt")
    artifact = await context.artifacts().save_text(payload, name=name)
    return ToolResult(
        ok=True,
        tool_name="ag.save_text_artifact",
        summary=f"Saved text artifact `{name}`.",
        artifacts=[_artifact_to_ref(artifact)],
        structured_output={"artifact_id": artifact.artifact_id},
    )


async def _exec_ag_save_json_artifact(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    payload = resolved_inputs.get("payload") or {}
    name = str(resolved_inputs.get("name") or "deeplens-data.json")
    artifact = await context.artifacts().save_json(payload, name=name)
    return ToolResult(
        ok=True,
        tool_name="ag.save_json_artifact",
        summary=f"Saved JSON artifact `{name}`.",
        artifacts=[_artifact_to_ref(artifact)],
        structured_output={"artifact_id": artifact.artifact_id},
    )


async def _exec_ag_send_image(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    url = str(resolved_inputs.get("url") or resolved_inputs.get("uri") or "")
    title = str(resolved_inputs.get("title") or resolved_inputs.get("name") or "image")
    if not url:
        return ToolResult(
            ok=False,
            tool_name="ag.send_image",
            summary="No image uri was provided.",
            error_type=ErrorType.MISSING_INPUT.value,
            error_message="Missing image uri.",
        )
    await context.channel("ui:session").send_image(url=url, alt=title, title=title, memory_log=False)
    return ToolResult(ok=True, tool_name="ag.send_image", summary=f"Sent image `{title}` to the UI.")


async def _exec_ag_send_file(*, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    url = str(resolved_inputs.get("url") or resolved_inputs.get("uri") or "")
    filename = str(resolved_inputs.get("filename") or maybe_extract_filename(url) or "file.bin")
    if not url:
        return ToolResult(
            ok=False,
            tool_name="ag.send_file",
            summary="No file uri was provided.",
            error_type=ErrorType.MISSING_INPUT.value,
            error_message="Missing file uri.",
        )
    await context.channel("ui:session").send_file(
        url=url,
        filename=filename,
        title=str(resolved_inputs.get("title") or filename),
        memory_log=False,
    )
    return ToolResult(ok=True, tool_name="ag.send_file", summary=f"Sent file `{filename}` to the UI.")


async def _exec_dl_optimize(*, resolved_inputs: dict[str, Any], task: Any, state: RuntimeState, context: Any, **_: Any) -> ToolResult:
    source = task.lens_source or state.active_source_ref
    if resolved_inputs.get("lens_source") or source:
        try:
            source = await resolve_lens_source(
                resolved_inputs=resolved_inputs,
                task=task,
                state=state,
                context=context,
            )
        except Exception:
            logger.warning("dl.optimize: resolve_lens_source failed, using fallback", exc_info=True)
    graph_id = str(resolved_inputs.get("graph_id") or "deeplens_v7_optimize_workflow")
    graph_inputs = build_optimization_inputs(
        resolved_inputs=resolved_inputs,
        task=task,
        source=source,
        state=state,
    )
    run_id = await context.runner().spawn_run(
        graph_id,
        inputs=graph_inputs,
        tags=["ag.deeplens.v7", "workflow:optimization"],
    )
    _record_pending_run(state, run_id, graph_id, meta={"source_action": "dl.optimize"})
    return ToolResult(
        ok=True,
        tool_name="dl.optimize",
        summary=(
            f"Started background workflow `{graph_id}`.\n"
            f"Run ID: `{run_id}`.\n"
            "Use status or cancel if you want to check or stop it."
        ),
        structured_output={"graph_id": graph_id, "run_id": run_id},
        state_patch={"active_source_ref": source or state.active_source_ref},
        should_end_turn=True,
    )


async def _exec_ag_status(*, resolved_inputs: dict[str, Any], state: RuntimeState, context: Any, **_: Any) -> ToolResult:
    run_id = str(resolved_inputs.get("run_id") or state.active_run_id or "")
    if not run_id:
        return ToolResult(
            ok=False,
            tool_name="ag.status",
            summary="There is no active background run to check.",
            error_type=ErrorType.MISSING_INPUT.value,
            error_message="Missing run id.",
        )
    record, outputs = await context.runner().wait_run(
        run_id,
        timeout_s=float(resolved_inputs.get("timeout_s") or 1),
        return_outputs=True,
    )
    status = str(getattr(record, "status", "unknown"))
    _update_pending_run_status(state, run_id, status)
    return ToolResult(
        ok=True,
        tool_name="ag.status",
        summary=summarize_status(record, outputs),
        structured_output={"record": {"status": status}, "outputs": outputs or {}},
    )


async def _exec_ag_cancel(*, resolved_inputs: dict[str, Any], state: RuntimeState, context: Any, **_: Any) -> ToolResult:
    run_id = str(resolved_inputs.get("run_id") or state.active_run_id or "")
    if not run_id:
        return ToolResult(
            ok=False,
            tool_name="ag.cancel",
            summary="There is no active background run to cancel.",
            error_type=ErrorType.MISSING_INPUT.value,
            error_message="Missing run id.",
        )
    await context.runner().cancel_run(run_id)
    _update_pending_run_status(state, run_id, "cancel_requested")
    return ToolResult(
        ok=True,
        tool_name="ag.cancel",
        summary=f"Requested cancellation for run `{run_id}`.",
        structured_output={"run_id": run_id},
        should_end_turn=True,
    )


async def _exec_dl_load_lens(*, resolved_inputs: dict[str, Any], task: Any, state: RuntimeState, context: Any, **_: Any) -> ToolResult:
    try:
        source = await resolve_lens_source(
            resolved_inputs=resolved_inputs,
            task=task,
            state=state,
            context=context,
        )
    except FileNotFoundError as exc:
        return ToolResult(
            ok=False,
            tool_name="dl.load_lens",
            summary=str(exc),
            error_type=ErrorType.MISSING_INPUT.value,
            error_message=str(exc),
        )
    except Exception as exc:
        logger.error("dl.load_lens failed", exc_info=True)
        return ToolResult(
            ok=False,
            tool_name="dl.load_lens",
            summary=f"Load failed: {exc}",
            error_type=ErrorType.UNEXPECTED.value,
            error_message=str(exc),
        )
    return ToolResult(
        ok=True,
        tool_name="dl.load_lens",
        summary=f"Loaded lens source `{source['name']}`.",
        structured_output={"source": source},
        state_patch={"active_source_ref": source},
    )


async def _exec_dl_analysis(*, resolved_inputs: dict[str, Any], task: Any, state: RuntimeState, context: Any, **_: Any) -> ToolResult:
    try:
        out = await run_analysis(
            resolved_inputs=resolved_inputs,
            task=task,
            state=state,
            context=context,
        )
    except FileNotFoundError as exc:
        return ToolResult(
            ok=False,
            tool_name="dl.analysis",
            summary=str(exc),
            error_type=ErrorType.MISSING_INPUT.value,
            error_message=str(exc),
        )
    except Exception as exc:
        logger.error("dl.analysis failed", exc_info=True)
        return ToolResult(
            ok=False,
            tool_name="dl.analysis",
            summary=f"Analysis failed: {exc}",
            error_type=ErrorType.UNEXPECTED.value,
            error_message=str(exc),
        )
    return ToolResult(
        ok=True,
        tool_name="dl.analysis",
        summary=f"{out['summary']}\nArtifacts: {', '.join(summarize_artifacts(out['artifacts']))}",
        artifacts=out["artifacts"],
        structured_output={"metrics": out["metrics"]},
        state_patch={
            "active_lens_ref": out.get("active_lens_ref"),
            "active_source_ref": out.get("active_source_ref"),
            "last_analysis_bundle": {
                "summary": out["summary"],
                "metrics": out["metrics"],
                "artifacts": out["artifacts"],
            },
            "last_metrics": out["metrics"],
        },
    )


async def _exec_dl_create_lens(*, resolved_inputs: dict[str, Any], state: RuntimeState, context: Any, **_: Any) -> ToolResult:
    spec = resolved_inputs.get("design_spec") or {}
    missing = missing_design_fields(spec)
    if missing:
        return ToolResult(
            ok=False,
            tool_name="dl.create_lens",
            summary=f"Missing required design fields: {', '.join(missing)}.",
            error_type=ErrorType.MISSING_INPUT.value,
            error_message=f"Missing required design fields: {', '.join(missing)}.",
        )
    try:
        created = await create_lens_design(resolved_inputs=resolved_inputs, context=context)
    except Exception as exc:
        logger.error("dl.create_lens failed", exc_info=True)
        return ToolResult(
            ok=False,
            tool_name="dl.create_lens",
            summary=f"Lens creation failed: {exc}",
            error_type=ErrorType.UNEXPECTED.value,
            error_message=str(exc),
        )
    artifacts = await persist_output_files(result_dir=created["result_dir"], context=context, tag="design")
    active_lens_ref = next((item.get("artifact_id") for item in artifacts if str(item.get("name")).endswith(".json")), None)
    active_source_ref = next((item for item in artifacts if str(item.get("name")).endswith(".json")), {})
    return ToolResult(
        ok=True,
        tool_name="dl.create_lens",
        summary=f"Created a new DeepLens design.\nArtifacts: {', '.join(summarize_artifacts(artifacts))}",
        artifacts=artifacts,
        structured_output={"design_spec": spec},
        state_patch={
            "active_lens_ref": active_lens_ref,
            "active_source_ref": active_source_ref,
            "design_draft": spec,
        },
    )


async def _exec_dl_export_lens(*, resolved_inputs: dict[str, Any], task: Any, state: RuntimeState, context: Any, **_: Any) -> ToolResult:
    try:
        out = await export_lens(
            resolved_inputs=resolved_inputs,
            task=task,
            state=state,
            context=context,
        )
    except FileNotFoundError as exc:
        return ToolResult(
            ok=False,
            tool_name="dl.export_lens",
            summary=str(exc),
            error_type=ErrorType.MISSING_INPUT.value,
            error_message=str(exc),
        )
    except Exception as exc:
        logger.error("dl.export_lens failed", exc_info=True)
        return ToolResult(
            ok=False,
            tool_name="dl.export_lens",
            summary=f"Export failed: {exc}",
            error_type=ErrorType.UNEXPECTED.value,
            error_message=str(exc),
        )
    return ToolResult(
        ok=True,
        tool_name="dl.export_lens",
        summary=f"{out['summary']}\nArtifacts: {', '.join(summarize_artifacts(out['artifacts']))}",
        artifacts=out["artifacts"],
        state_patch={
            "active_lens_ref": out.get("active_lens_ref"),
            "active_source_ref": out.get("active_source_ref"),
        },
    )


EXECUTORS = {
    "ag.get_latest_uploads": _exec_ag_get_latest_uploads,
    "ag.load_artifact_text_or_json": _exec_ag_load_artifact_text_or_json,
    "ag.save_text_artifact": _exec_ag_save_text_artifact,
    "ag.save_json_artifact": _exec_ag_save_json_artifact,
    "ag.send_image": _exec_ag_send_image,
    "ag.send_file": _exec_ag_send_file,
    "dl.optimize": _exec_dl_optimize,
    "ag.status": _exec_ag_status,
    "ag.cancel": _exec_ag_cancel,
    "dl.load_lens": _exec_dl_load_lens,
    "dl.analysis": _exec_dl_analysis,
    "dl.create_lens": _exec_dl_create_lens,
    "dl.export_lens": _exec_dl_export_lens,
}


async def execute_tool(*, action: Any, task: Any, state: RuntimeState, context: Any) -> ToolResult:
    tool_name = action.tool_name
    executor = EXECUTORS[tool_name]
    result = await executor(resolved_inputs=action.args, task=task, state=state, context=context)
    if result.artifacts:
        _append_artifacts(state, result.artifacts)
    for key, value in result.state_patch.items():
        if value is not None and hasattr(state, key):
            setattr(state, key, value)
    await _record_tool_memory(context, tool_name, action.args, result)
    return result
