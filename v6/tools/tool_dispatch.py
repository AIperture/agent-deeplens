from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..backend import (
    _artifact_to_ref,
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
from ..types import ActionAttempt, ApprovalLevel, FailureKind, PendingRun, ToolResult
from .tool_arg_refine import maybe_refine_tool_inputs_with_llm, normalize_tool_inputs
from .tool_registry import get_tool_spec

logger = logging.getLogger("ag.deeplens.v6.tool_dispatch")


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


def _failure_result(
    *,
    tool_name: str,
    summary: str,
    status: str = "failed",
    error_code: str | None = None,
    failure_kind: FailureKind = FailureKind.UNKNOWN,
    blocking: bool = False,
    repairable: bool = False,
    needs_replan: bool = False,
    needs_user_input: bool = False,
    missing_fields: list[str] | None = None,
    invalid_fields: dict[str, str] | None = None,
    repair_hints: list[str] | None = None,
    dependency_failures: list[str] | None = None,
    diagnostics: dict[str, Any] | None = None,
    human_escalation_reason: str | None = None,
) -> ToolResult:
    return ToolResult(
        ok=False,
        tool_name=tool_name,
        summary=summary,
        status=status,
        error_code=error_code,
        retryable=repairable,
        failure_kind=failure_kind,
        blocking=blocking,
        repairable=repairable,
        needs_replan=needs_replan,
        needs_user_input=needs_user_input,
        missing_fields=list(missing_fields or []),
        invalid_fields=dict(invalid_fields or {}),
        repair_hints=list(repair_hints or []),
        dependency_failures=list(dependency_failures or []),
        diagnostics=dict(diagnostics or {}),
        human_escalation_reason=human_escalation_reason,
    )


def _failure_signature(action_id: str, result: ToolResult) -> str:
    return "|".join(
        [
            action_id or "unknown_action",
            str(result.tool_name or "unknown_tool"),
            str(result.failure_kind.value if result.failure_kind is not None else "none"),
            str(result.error_code or "none"),
            str(result.summary or "")[:160],
        ]
    )


def _next_attempt_index(state: Any, action_id: str, tool_name: str) -> int:
    history = list(getattr(state, "attempt_history", []) or [])
    matches = [item for item in history if item.get("action_id") == action_id and item.get("tool_name") == tool_name]
    return len(matches) + 1


def _record_attempt_snapshot(
    state: Any,
    *,
    attempt: ActionAttempt,
) -> None:
    payload = attempt.to_dict()
    state.last_attempt = payload
    state.attempt_history.append(payload)
    state.attempt_history = state.attempt_history[-20:]


def _update_attempt_result(state: Any, result: ToolResult) -> None:
    if not isinstance(state.last_attempt, dict):
        return
    attempt = dict(state.last_attempt)
    attempt["result"] = {
        "ok": result.ok,
        "summary": result.summary,
        "status": result.status,
        "error_code": result.error_code,
        "failure_kind": result.failure_kind.value if result.failure_kind is not None else None,
        "missing_fields": list(result.missing_fields),
        "invalid_fields": dict(result.invalid_fields),
    }
    if not result.ok:
        attempt["failure_signature"] = _failure_signature(str(attempt.get("action_id") or ""), result)
    state.last_attempt = attempt
    if state.attempt_history:
        state.attempt_history[-1] = attempt


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
                "attempt_id": result.attempt_id,
                "failure_kind": result.failure_kind.value if result.failure_kind is not None else None,
                "missing_fields": result.missing_fields,
                "invalid_fields": result.invalid_fields,
                "repair_hints": result.repair_hints,
            }
        ],
        message=result.summary,
        tags=["ag.deeplens.v6.tool", f"ag.deeplens.v6.action:{tool_name}"],
    )


async def _maybe_approval(spec: Any, action: dict[str, Any], state: Any, context: Any) -> bool:
    if spec.approval_level == ApprovalLevel.NONE:
        return True
    if state.approved_action == spec.name:
        state.approved_action = None
        return True
    if spec.approval_level == ApprovalLevel.HARD:
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
) -> tuple[dict[str, Any], dict[str, str]]:
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
    refined = await maybe_refine_tool_inputs_with_llm(
        spec=spec,
        draft_inputs=args,
        task=task,
        state=state,
        context_bundle=context_bundle,
        context=context,
    )
    normalized, invalid_fields = normalize_tool_inputs(spec=spec, refined_inputs=refined)
    return normalized, invalid_fields


def _validate_inputs(tool_name: str, spec: Any, resolved_inputs: dict[str, Any], invalid_fields: dict[str, str]) -> ToolResult | None:
    if invalid_fields:
        return _failure_result(
            tool_name=tool_name,
            summary=f"`{tool_name}` received invalid inputs.",
            error_code="invalid_inputs",
            failure_kind=FailureKind.INVALID_INPUTS,
            blocking=True,
            repairable=True,
            invalid_fields=invalid_fields,
            repair_hints=["normalize_tool_inputs", "retry_action"],
        )

    if spec.name == "dl.create_lens":
        missing = missing_design_fields(dict(resolved_inputs.get("design_spec") or {}))
        if missing:
            return _failure_result(
                tool_name=tool_name,
                summary=f"Missing required design fields: {', '.join(missing)}.",
                error_code="missing_design_fields",
                failure_kind=FailureKind.MISSING_INPUTS,
                blocking=True,
                repairable=True,
                needs_user_input=True,
                missing_fields=missing,
                repair_hints=["ask_user"],
            )

    if getattr(spec, "requires_lens_input", False):
        if not (resolved_inputs.get("lens_source") or resolved_inputs.get("active_source_ref")):
            return _failure_result(
                tool_name=tool_name,
                summary=f"`{tool_name}` needs an active or provided lens source before it can run.",
                error_code="missing_lens_dependency",
                failure_kind=FailureKind.MISSING_DEPENDENCY,
                blocking=True,
                repairable=True,
                needs_replan=True,
                dependency_failures=["lens_source"],
                repair_hints=["replan_remaining_agenda", "ask_user"],
            )

    return None


async def _exec_ag_get_latest_uploads(*, tool_name: str, context: Any, **_: Any) -> ToolResult:
    uploads = await context.channel("ui:session").get_latest_uploads(clear=False)
    names = [u.get("name") or u.get("filename") or u.get("uri") or "upload" for u in uploads]
    return ToolResult(ok=True, tool_name=tool_name, summary=f"Found {len(uploads)} uploaded file(s).", data={"uploads": uploads, "names": names})


async def _exec_ag_load_artifact_text_or_json(*, tool_name: str, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    artifact_id = resolved_inputs.get("artifact_id")
    uri = resolved_inputs.get("uri")
    if artifact_id:
        raw_text = await context.artifacts().load_text_by_id(str(artifact_id))
    elif uri:
        raw_text = await context.artifacts().load_text(str(uri))
    else:
        return _failure_result(tool_name=tool_name, summary="No artifact id or uri was provided.", error_code="missing_artifact_ref", failure_kind=FailureKind.MISSING_INPUTS, repairable=True, needs_user_input=True)
    payload = load_text_or_json_payload(raw_text)
    return ToolResult(ok=True, tool_name=tool_name, summary="Loaded artifact payload.", data={"payload": payload})


async def _exec_ag_save_text_artifact(*, tool_name: str, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    payload = str(resolved_inputs.get("payload") or resolved_inputs.get("text") or "")
    name = str(resolved_inputs.get("name") or "deeplens-note.txt")
    artifact = await context.artifacts().save_text(payload, name=name)
    return ToolResult(ok=True, tool_name=tool_name, summary=f"Saved text artifact `{name}`.", artifacts=[_artifact_to_ref(artifact)], data={"artifact_id": artifact.artifact_id})


async def _exec_ag_save_json_artifact(*, tool_name: str, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    payload = resolved_inputs.get("payload") or {}
    name = str(resolved_inputs.get("name") or "deeplens-data.json")
    artifact = await context.artifacts().save_json(payload, name=name)
    return ToolResult(ok=True, tool_name=tool_name, summary=f"Saved JSON artifact `{name}`.", artifacts=[_artifact_to_ref(artifact)], data={"artifact_id": artifact.artifact_id})


async def _exec_ag_send_image(*, tool_name: str, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    url = str(resolved_inputs.get("url") or resolved_inputs.get("uri") or "")
    title = str(resolved_inputs.get("title") or resolved_inputs.get("name") or "image")
    if not url:
        return _failure_result(tool_name=tool_name, summary="No image uri was provided.", error_code="missing_image_uri", failure_kind=FailureKind.MISSING_INPUTS, repairable=True, needs_user_input=True)
    await context.channel("ui:session").send_image(url=url, alt=title, title=title, memory_log=False)
    return ToolResult(ok=True, tool_name=tool_name, summary=f"Sent image `{title}` to the UI.")


async def _exec_ag_send_file(*, tool_name: str, resolved_inputs: dict[str, Any], context: Any, **_: Any) -> ToolResult:
    url = str(resolved_inputs.get("url") or resolved_inputs.get("uri") or "")
    filename = str(resolved_inputs.get("filename") or maybe_extract_filename(url) or "file.bin")
    if not url:
        return _failure_result(tool_name=tool_name, summary="No file uri was provided.", error_code="missing_file_uri", failure_kind=FailureKind.MISSING_INPUTS, repairable=True, needs_user_input=True)
    await context.channel("ui:session").send_file(url=url, filename=filename, title=str(resolved_inputs.get("title") or filename), memory_log=False)
    return ToolResult(ok=True, tool_name=tool_name, summary=f"Sent file `{filename}` to the UI.")


GRAPH_INPUT_BUILDERS: dict[str, Any] = {
    "deeplens_v6_optimize_workflow": build_optimization_inputs,
}


async def _exec_ag_spawn_graph(*, tool_name: str, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any, **_: Any) -> ToolResult:
    source = None
    if resolved_inputs.get("lens_source") or task.lens_source or state.active_source_ref:
        try:
            source = await resolve_lens_source(resolved_inputs=resolved_inputs, task=task, state=state, context=context)
        except Exception:
            logger.warning("ag.spawn_graph: resolve_lens_source failed, using fallback", exc_info=True)
            source = task.lens_source or state.active_source_ref

    graph_id = str(resolved_inputs.get("graph_id") or "deeplens_v6_optimize_workflow")
    builder = GRAPH_INPUT_BUILDERS.get(graph_id)
    graph_inputs = resolved_inputs.get("graph_inputs") or (builder(resolved_inputs=resolved_inputs, task=task, source=source, state=state) if builder else resolved_inputs)
    run_id = await context.runner().spawn_run(
        graph_id,
        inputs=graph_inputs,
        tags=["ag.deeplens.v6", f"shape:{task.task_shape.value}", f"domain:{task.domain_hint.value}"],
    )
    _record_pending_run(state, run_id, graph_id, meta={"source_action": "ag.spawn_graph"})
    return ToolResult(
        ok=True,
        tool_name=tool_name,
        status="submitted",
        summary=f"Started background workflow `{graph_id}`.\nRun ID: `{run_id}`.\nUse status or cancel if you want to check or stop it.",
        run_id=run_id,
        data={
            "graph_id": graph_id,
            "graph_inputs": graph_inputs,
            "state_updates": {
                "active_source_ref": source or state.active_source_ref,
                "next_action_hints": ["Use status to check the optimization run, or cancel if you want to stop it."],
            },
        },
        should_end_turn=True,
    )


async def _exec_ag_status(*, tool_name: str, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    run_id = str(resolved_inputs.get("run_id") or state.active_run_id or "")
    if not run_id:
        return _failure_result(tool_name=tool_name, summary="There is no active background run to check.", error_code="missing_run_id", failure_kind=FailureKind.MISSING_INPUTS, repairable=True, needs_user_input=True, missing_fields=["run_id"])
    record, outputs = await context.runner().wait_run(run_id, timeout_s=float(resolved_inputs.get("timeout_s") or 1), return_outputs=True)
    status = str(getattr(record, "status", "unknown"))
    _update_pending_run_status(state, run_id, status)
    return ToolResult(ok=True, tool_name=tool_name, summary=summarize_status(record, outputs), data={"record": {"status": status}, "outputs": outputs or {}}, run_id=run_id)


async def _exec_ag_cancel(*, tool_name: str, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    run_id = str(resolved_inputs.get("run_id") or state.active_run_id or "")
    if not run_id:
        return _failure_result(tool_name=tool_name, summary="There is no active background run to cancel.", error_code="missing_run_id", failure_kind=FailureKind.MISSING_INPUTS, repairable=True, needs_user_input=True, missing_fields=["run_id"])
    await context.runner().cancel_run(run_id)
    _update_pending_run_status(state, run_id, "cancel_requested")
    return ToolResult(ok=True, tool_name=tool_name, status="cancel_requested", summary=f"Requested cancellation for run `{run_id}`.", run_id=run_id, should_end_turn=True)


async def _exec_dl_load_lens(*, tool_name: str, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any, **_: Any) -> ToolResult:
    try:
        source = await resolve_lens_source(resolved_inputs=resolved_inputs, task=task, state=state, context=context)
    except FileNotFoundError as exc:
        return _failure_result(tool_name=tool_name, summary=str(exc), error_code="missing_lens_source", failure_kind=FailureKind.MISSING_DEPENDENCY, blocking=True, repairable=True, needs_user_input=True, dependency_failures=["lens_source"], repair_hints=["ask_user", "replan_remaining_agenda"])
    except Exception as exc:
        logger.error("dl.load_lens failed", exc_info=True)
        return _failure_result(tool_name=tool_name, summary=str(exc), error_code="load_lens_failed", failure_kind=FailureKind.INTERNAL_TOOL_ERROR, blocking=True, needs_replan=True, diagnostics={"exception_type": type(exc).__name__})
    return ToolResult(ok=True, tool_name=tool_name, summary=f"Loaded lens source `{source['name']}`.", data={"state_updates": {"active_source_ref": source}})


async def _exec_dl_analysis(*, tool_name: str, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any, **_: Any) -> ToolResult:
    try:
        out = await run_analysis(resolved_inputs=resolved_inputs, task=task, state=state, context=context)
    except FileNotFoundError as exc:
        return _failure_result(tool_name=tool_name, summary=f"Analysis failed: {exc}", error_code="missing_lens_source", failure_kind=FailureKind.MISSING_DEPENDENCY, blocking=True, repairable=True, needs_replan=True, dependency_failures=["lens_source"], repair_hints=["replan_remaining_agenda", "ask_user"])
    except Exception as exc:
        logger.error("dl.analysis failed", exc_info=True)
        return _failure_result(tool_name=tool_name, summary=f"Analysis failed: {exc}", error_code="analysis_failed", failure_kind=FailureKind.INTERNAL_TOOL_ERROR, blocking=True, needs_replan=True, diagnostics={"exception_type": type(exc).__name__})
    artifact_names = ", ".join(summarize_artifacts(out["artifacts"]))
    return ToolResult(
        ok=True,
        tool_name=tool_name,
        summary=f"{out['summary']}\nArtifacts: {artifact_names}",
        artifacts=out["artifacts"],
        data={
            "metrics": out["metrics"],
            "state_updates": {
                "active_lens_ref": out.get("active_lens_ref"),
                "active_source_ref": out.get("active_source_ref"),
                "last_analysis_bundle": {"summary": out["summary"], "metrics": out["metrics"], "artifacts": out["artifacts"]},
                "last_metrics": out["metrics"],
                "next_action_hints": ["You can export the analyzed lens or start optimization next."],
            },
        },
    )


async def _exec_dl_create_lens(*, tool_name: str, resolved_inputs: dict[str, Any], state: Any, context: Any, **_: Any) -> ToolResult:
    spec = resolved_inputs.get("design_spec") or {}
    missing = missing_design_fields(spec)
    if missing:
        return _failure_result(tool_name=tool_name, summary=f"Missing required design fields: {', '.join(missing)}.", error_code="missing_design_fields", failure_kind=FailureKind.MISSING_INPUTS, blocking=True, repairable=True, needs_user_input=True, missing_fields=missing)
    try:
        created = await create_lens_design(resolved_inputs=resolved_inputs, context=context)
    except Exception as exc:
        logger.error("dl.create_lens failed", exc_info=True)
        failure_kind = FailureKind.INVALID_INPUTS if isinstance(exc, (TypeError, ValueError)) else FailureKind.INTERNAL_TOOL_ERROR
        return _failure_result(
            tool_name=tool_name,
            summary=f"Lens creation failed: {exc}",
            error_code="create_lens_failed",
            failure_kind=failure_kind,
            blocking=True,
            repairable=failure_kind == FailureKind.INVALID_INPUTS,
            needs_replan=failure_kind != FailureKind.INVALID_INPUTS,
            repair_hints=["normalize_tool_inputs", "retry_action"] if failure_kind == FailureKind.INVALID_INPUTS else ["replan_remaining_agenda"],
            diagnostics={"exception_type": type(exc).__name__, "design_spec": spec},
        )

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
    active_lens_ref = next((artifact.get("artifact_id") for artifact in artifacts if str(artifact.get("name")).endswith(".json")), None)
    active_source_ref = next((artifact for artifact in artifacts if str(artifact.get("name")).endswith(".json")), {})
    return ToolResult(
        ok=True,
        tool_name=tool_name,
        summary=f"Created a new DeepLens design from the provided spec.\nArtifacts: {', '.join(summarize_artifacts(artifacts))}",
        artifacts=artifacts,
        data={
            "design_spec": spec,
            "state_updates": {
                "active_lens_ref": active_lens_ref,
                "active_source_ref": active_source_ref,
                "design_draft": spec,
                "next_action_hints": ["You can analyze the new lens, export it, or start optimization."],
            },
        },
    )


async def _exec_dl_export_lens(*, tool_name: str, resolved_inputs: dict[str, Any], task: Any, state: Any, context: Any, **_: Any) -> ToolResult:
    try:
        out = await export_lens(resolved_inputs=resolved_inputs, task=task, state=state, context=context)
    except FileNotFoundError as exc:
        return _failure_result(tool_name=tool_name, summary=f"Export failed: {exc}", error_code="missing_lens_source", failure_kind=FailureKind.MISSING_DEPENDENCY, blocking=True, repairable=True, needs_replan=True, dependency_failures=["lens_source"], repair_hints=["replan_remaining_agenda", "ask_user"])
    except Exception as exc:
        logger.error("dl.export_lens failed", exc_info=True)
        return _failure_result(tool_name=tool_name, summary=f"Export failed: {exc}", error_code="export_failed", failure_kind=FailureKind.INTERNAL_TOOL_ERROR, blocking=True, needs_replan=True, diagnostics={"exception_type": type(exc).__name__})
    return ToolResult(
        ok=True,
        tool_name=tool_name,
        summary=f"{out['summary']}\nArtifacts: {', '.join(summarize_artifacts(out['artifacts']))}",
        artifacts=out["artifacts"],
        data={
            "state_updates": {
                "active_lens_ref": out.get("active_lens_ref"),
                "active_source_ref": out.get("active_source_ref"),
                "next_action_hints": ["You can optimize the exported lens or start a new design."],
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
    if isinstance(updates, dict):
        for key, value in updates.items():
            if value is not None and hasattr(state, key):
                setattr(state, key, value)
    state.last_tool_result = {
        "attempt_id": result.attempt_id,
        "tool_name": result.tool_name,
        "ok": result.ok,
        "status": result.status,
        "summary": result.summary,
        "failure_kind": result.failure_kind.value if result.failure_kind is not None else None,
        "error_code": result.error_code,
        "missing_fields": list(result.missing_fields),
        "invalid_fields": dict(result.invalid_fields),
        "repair_hints": list(result.repair_hints),
    }
    _update_attempt_result(state, result)
    if result.missing_fields:
        for field_name in result.missing_fields:
            if field_name not in state.runtime_missing_fields:
                state.runtime_missing_fields.append(field_name)
        state.last_prompt_reason = "runtime_missing_fields"
    if result.invalid_fields:
        state.runtime_invalid_fields.update(result.invalid_fields)
        state.last_prompt_reason = "runtime_invalid_fields"
    if result.ok:
        state.runtime_missing_fields = []
        state.runtime_invalid_fields = {}
        state.last_prompt_reason = None
    if not result.ok:
        state.failure_history.append(
            {
                "attempt_id": result.attempt_id,
                "tool_name": result.tool_name,
                "status": result.status,
                "summary": result.summary,
                "failure_kind": result.failure_kind.value if result.failure_kind is not None else None,
                "error_code": result.error_code,
            }
        )
        state.failure_history = state.failure_history[-20:]


async def dispatch_tool_action(
    *,
    action: dict[str, Any],
    task: Any,
    state: Any,
    context_bundle: Any,
    context: Any,
) -> ToolResult:
    tool_name = action.get("name") or ""
    action_id = str(action.get("action_id") or "")
    spec = get_tool_spec(tool_name)

    approved = await _maybe_approval(spec, action, state, context)
    if not approved:
        state.approved_action = None
        return ToolResult(ok=True, tool_name=tool_name, status="canceled", summary=f"Skipped `{tool_name}` because approval was not granted.", should_end_turn=True)

    resolved_inputs, invalid_fields = await _resolve_inputs(
        spec=spec,
        action=action,
        task=task,
        state=state,
        context_bundle=context_bundle,
        context=context,
    )
    attempt = ActionAttempt(
        attempt_id=f"{action_id or tool_name}:{_next_attempt_index(state, action_id or tool_name, tool_name)}",
        action_id=action_id or tool_name,
        tool_name=tool_name,
        resolved_inputs=resolved_inputs,
        invalid_fields=invalid_fields,
        attempt_index=_next_attempt_index(state, action_id or tool_name, tool_name),
    )
    _record_attempt_snapshot(state, attempt=attempt)
    validation_failure = _validate_inputs(tool_name, spec, resolved_inputs, invalid_fields)
    if validation_failure is not None:
        validation_failure.attempt_id = attempt.attempt_id
        validation_failure.diagnostics.setdefault("failure_signature", _failure_signature(attempt.action_id, validation_failure))
        _apply_state_updates(state, validation_failure)
        state.approved_action = None
        await _record_tool_memory(context, tool_name, resolved_inputs, validation_failure)
        return validation_failure

    try:
        result = await EXECUTOR_MAP[spec.executor_key](
            tool_name=tool_name,
            resolved_inputs=resolved_inputs,
            task=task,
            state=state,
            context_bundle=context_bundle,
            context=context,
        )
    except Exception as exc:
        logger.error("Unhandled error in tool executor %s", tool_name, exc_info=True)
        result = _failure_result(
            tool_name=tool_name,
            summary=f"`{tool_name}` failed: {exc}",
            error_code="unhandled_executor_error",
            failure_kind=FailureKind.INTERNAL_TOOL_ERROR,
            blocking=True,
            needs_replan=True,
            diagnostics={"exception_type": type(exc).__name__},
        )
    result.attempt_id = attempt.attempt_id
    if not result.ok:
        result.diagnostics.setdefault("failure_signature", _failure_signature(attempt.action_id, result))
    if result.artifacts:
        _append_artifacts(state, result.artifacts)
    _apply_state_updates(state, result)
    state.approved_action = None
    await _record_tool_memory(context, tool_name, resolved_inputs, result)
    return result
