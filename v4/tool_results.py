from __future__ import annotations

from typing import Any

from .types import OutcomeType, ToolResult


# ----------------------------
# helper constructors
# ----------------------------

def success_result(
    *,
    tool_name: str,
    summary: str,
    data: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    run_id: str | None = None,
    state_updates: dict[str, Any] | None = None,
    recommended_next_actions: list[str] | None = None,
    should_end_turn: bool = False,
    user_visible_summary: str | None = None,
    cost_class: str | None = None,
    raw_status: str | None = None,
) -> ToolResult:
    return ToolResult(
        ok=True,
        summary=summary,
        status="completed",
        outcome_type=OutcomeType.SUCCESS,
        data=data or {},
        artifacts=artifacts or [],
        warnings=warnings or [],
        run_id=run_id,
        should_end_turn=should_end_turn,
        needs_input=False,
        missing_fields=[],
        state_updates=state_updates or {},
        recommended_next_actions=recommended_next_actions or [],
        user_visible_summary=user_visible_summary or summary,
        cost_class=cost_class,
        tool_name=tool_name,
        raw_status=raw_status,
    )


def needs_input_result(
    *,
    tool_name: str,
    summary: str,
    missing_fields: list[str] | None = None,
    data: dict[str, Any] | None = None,
    recommended_next_actions: list[str] | None = None,
    user_visible_summary: str | None = None,
    raw_status: str | None = None,
    error_code: str = "missing_input",
) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=summary,
        status="needs_input",
        outcome_type=OutcomeType.NEEDS_INPUT,
        data=data or {},
        artifacts=[],
        warnings=[],
        error_code=error_code,
        retryable=False,
        should_end_turn=True,
        needs_input=True,
        missing_fields=missing_fields or [],
        state_updates={},
        recommended_next_actions=recommended_next_actions or [],
        user_visible_summary=user_visible_summary or summary,
        tool_name=tool_name,
        raw_status=raw_status,
    )


def run_submitted_result(
    *,
    tool_name: str,
    summary: str,
    run_id: str,
    data: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    state_updates: dict[str, Any] | None = None,
    user_visible_summary: str | None = None,
    raw_status: str | None = None,
) -> ToolResult:
    return ToolResult(
        ok=True,
        summary=summary,
        status="submitted",
        outcome_type=OutcomeType.RUN_SUBMITTED,
        data=data or {},
        artifacts=artifacts or [],
        warnings=[],
        run_id=run_id,
        retryable=False,
        should_end_turn=True,
        needs_input=False,
        missing_fields=[],
        state_updates=state_updates or {},
        recommended_next_actions=["check_run_status"],
        user_visible_summary=user_visible_summary or summary,
        tool_name=tool_name,
        raw_status=raw_status,
    )


def run_updated_result(
    *,
    tool_name: str,
    summary: str,
    run_id: str | None = None,
    data: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    state_updates: dict[str, Any] | None = None,
    user_visible_summary: str | None = None,
    raw_status: str | None = None,
) -> ToolResult:
    return ToolResult(
        ok=True,
        summary=summary,
        status="updated",
        outcome_type=OutcomeType.RUN_UPDATED,
        data=data or {},
        artifacts=artifacts or [],
        warnings=[],
        run_id=run_id,
        retryable=False,
        should_end_turn=True,
        needs_input=False,
        missing_fields=[],
        state_updates=state_updates or {},
        recommended_next_actions=[],
        user_visible_summary=user_visible_summary or summary,
        tool_name=tool_name,
        raw_status=raw_status,
    )


def failure_result(
    *,
    tool_name: str,
    summary: str,
    error_code: str | None = None,
    retryable: bool = False,
    data: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    missing_fields: list[str] | None = None,
    state_updates: dict[str, Any] | None = None,
    recommended_next_actions: list[str] | None = None,
    user_visible_summary: str | None = None,
    raw_status: str | None = None,
) -> ToolResult:
    outcome = OutcomeType.NEEDS_INPUT if (missing_fields or []) else OutcomeType.FAILED
    needs_input = bool(missing_fields)

    return ToolResult(
        ok=False,
        summary=summary,
        status="failed" if not needs_input else "needs_input",
        outcome_type=outcome,
        data=data or {},
        artifacts=[],
        warnings=warnings or [],
        error_code=error_code,
        retryable=retryable,
        should_end_turn=needs_input,
        needs_input=needs_input,
        missing_fields=missing_fields or [],
        state_updates=state_updates or {},
        recommended_next_actions=recommended_next_actions or [],
        user_visible_summary=user_visible_summary or summary,
        tool_name=tool_name,
        raw_status=raw_status,
    )


def normalize_tool_result(
    *,
    tool_name: str,
    raw_result: Any = None,
    error: Exception | None = None,
) -> ToolResult:
    """
    Convert raw tool/controller outputs into a canonical ToolResult.

    Accepted inputs:
    - already-normalized ToolResult
    - dict-like tool payload
    - exception
    - plain text / fallback payload
    """
    if isinstance(raw_result, ToolResult):
        if not raw_result.tool_name:
            raw_result.tool_name = tool_name
        return raw_result

    if error is not None:
        return _normalize_exception(tool_name=tool_name, error=error)

    if isinstance(raw_result, dict):
        return _normalize_dict_result(tool_name=tool_name, payload=raw_result)

    if isinstance(raw_result, str):
        return success_result(
            tool_name=tool_name,
            summary=raw_result,
            user_visible_summary=raw_result,
            raw_status="string_result",
        )

    if raw_result is None:
        return failure_result(
            tool_name=tool_name,
            summary="Tool returned no result.",
            error_code="empty_result",
            raw_status="none",
        )

    return success_result(
        tool_name=tool_name,
        summary=f"Tool returned {type(raw_result).__name__}.",
        data={"value": raw_result},
        user_visible_summary="The operation completed.",
        raw_status=type(raw_result).__name__,
    )


def _normalize_dict_result(*, tool_name: str, payload: dict[str, Any]) -> ToolResult:
    """
    Normalize common dict-style payloads from tools or controller wrappers.
    """
    ok = payload.get("ok")
    status = str(payload.get("status", "") or "").lower()
    error_code = payload.get("error_code")
    summary = payload.get("summary") or payload.get("message") or payload.get("text") or ""
    data = dict(payload.get("data", {}) or {})
    artifacts = list(payload.get("artifacts", []) or [])
    warnings = list(payload.get("warnings", []) or [])
    run_id = payload.get("run_id")
    missing_fields = list(payload.get("missing_fields", []) or [])
    recommended_next_actions = list(payload.get("recommended_next_actions", []) or [])
    state_updates = dict(payload.get("state_updates", {}) or {})
    user_visible_summary = payload.get("user_visible_summary")
    raw_status = status or payload.get("raw_status")

    # Explicit canonical shape from tool/controller.
    if isinstance(ok, bool):
        if ok:
            if status in {"submitted", "queued", "running"} and run_id:
                return run_submitted_result(
                    tool_name=tool_name,
                    summary=summary or "Run submitted.",
                    run_id=run_id,
                    data=data,
                    artifacts=artifacts,
                    state_updates=state_updates,
                    user_visible_summary=user_visible_summary,
                    raw_status=raw_status,
                )
            if status in {"updated", "cancelled", "completed_with_update"}:
                return run_updated_result(
                    tool_name=tool_name,
                    summary=summary or "Run updated.",
                    run_id=run_id,
                    data=data,
                    artifacts=artifacts,
                    state_updates=state_updates,
                    user_visible_summary=user_visible_summary,
                    raw_status=raw_status,
                )
            return success_result(
                tool_name=tool_name,
                summary=summary or "Operation completed.",
                data=data,
                artifacts=artifacts,
                warnings=warnings,
                run_id=run_id,
                state_updates=state_updates,
                recommended_next_actions=recommended_next_actions,
                user_visible_summary=user_visible_summary,
                raw_status=raw_status,
            )

        if missing_fields or status in {"needs_input", "missing_input"}:
            return needs_input_result(
                tool_name=tool_name,
                summary=summary or "More input is required.",
                missing_fields=missing_fields,
                data=data,
                recommended_next_actions=recommended_next_actions,
                user_visible_summary=user_visible_summary,
                raw_status=raw_status,
                error_code=error_code or "missing_input",
            )

        return failure_result(
            tool_name=tool_name,
            summary=summary or "Operation failed.",
            error_code=error_code,
            retryable=bool(payload.get("retryable", False)),
            data=data,
            warnings=warnings,
            missing_fields=missing_fields,
            state_updates=state_updates,
            recommended_next_actions=recommended_next_actions,
            user_visible_summary=user_visible_summary,
            raw_status=raw_status,
        )

    # Heuristic shape: AG run-style / controller payload.
    if run_id and status in {"submitted", "queued", "running"}:
        return run_submitted_result(
            tool_name=tool_name,
            summary=summary or "Run submitted.",
            run_id=run_id,
            data=data,
            artifacts=artifacts,
            state_updates=state_updates,
            user_visible_summary=user_visible_summary,
            raw_status=raw_status,
        )

    if missing_fields:
        return needs_input_result(
            tool_name=tool_name,
            summary=summary or "More input is required.",
            missing_fields=missing_fields,
            data=data,
            recommended_next_actions=recommended_next_actions,
            user_visible_summary=user_visible_summary,
            raw_status=raw_status,
            error_code=error_code or "missing_input",
        )

    if error_code:
        return failure_result(
            tool_name=tool_name,
            summary=summary or "Operation failed.",
            error_code=error_code,
            retryable=bool(payload.get("retryable", False)),
            data=data,
            warnings=warnings,
            missing_fields=missing_fields,
            state_updates=state_updates,
            recommended_next_actions=recommended_next_actions,
            user_visible_summary=user_visible_summary,
            raw_status=raw_status,
        )

    return success_result(
        tool_name=tool_name,
        summary=summary or "Operation completed.",
        data=data,
        artifacts=artifacts,
        warnings=warnings,
        run_id=run_id,
        state_updates=state_updates,
        recommended_next_actions=recommended_next_actions,
        user_visible_summary=user_visible_summary,
        raw_status=raw_status,
    )


def _normalize_exception(*, tool_name: str, error: Exception) -> ToolResult:
    text = str(error).strip() or error.__class__.__name__
    text_lower = text.lower()

    if "missing" in text_lower and "source" in text_lower:
        return needs_input_result(
            tool_name=tool_name,
            summary=text,
            missing_fields=["lens_source"],
            raw_status=error.__class__.__name__,
            error_code="missing_source",
        )

    if "missing" in text_lower and ("fov" in text_lower or "f-number" in text_lower or "fnum" in text_lower):
        missing = []
        if "fov" in text_lower:
            missing.append("fov")
        if "f-number" in text_lower or "fnum" in text_lower:
            missing.append("fnum")
        return needs_input_result(
            tool_name=tool_name,
            summary=text,
            missing_fields=missing,
            raw_status=error.__class__.__name__,
            error_code="missing_design_spec",
        )

    return failure_result(
        tool_name=tool_name,
        summary=text,
        error_code="exception",
        retryable=False,
        raw_status=error.__class__.__name__,
    )


def apply_tool_result_to_state(*, result: ToolResult, state: Any) -> None:
    """
    Apply standardized state updates from a ToolResult onto DeepLensState.

    Keep this narrow and explicit.
    """
    updates = result.state_updates or {}

    if "active_run_id" in updates:
        state.active_run_id = updates["active_run_id"]

    if "pending_runs" in updates:
        state.pending_runs = list(updates["pending_runs"] or [])

    if "active_source_ref" in updates:
        state.active_source_ref = dict(updates["active_source_ref"] or {})

    if "active_lens_ref" in updates:
        state.active_lens_ref = updates["active_lens_ref"]

    if "last_metrics" in updates:
        state.last_metrics = dict(updates["last_metrics"] or {})

    if "last_analysis_bundle" in updates:
        state.last_analysis_bundle = dict(updates["last_analysis_bundle"] or {})

    if "design_draft" in updates:
        state.design_draft = dict(updates["design_draft"] or {})

    if "requested_next_step" in updates:
        state.requested_next_step = updates["requested_next_step"]

    if result.artifacts:
        state.last_artifacts.extend(result.artifacts)
        state.last_artifacts = state.last_artifacts[-20:]

    if result.run_id and result.outcome_type in {OutcomeType.RUN_SUBMITTED, OutcomeType.RUN_UPDATED}:
        state.active_run_id = result.run_id
