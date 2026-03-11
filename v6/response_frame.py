from __future__ import annotations

from typing import Any

from .types import ActionAgenda, DeepLensState, ResponseFrame, ResponseOutcomeKind, ToolResult


def build_response_frame(
    *,
    reply: str,
    outcome_kind: ResponseOutcomeKind,
    state: DeepLensState,
    agenda: ActionAgenda | None,
    last_tool_result: ToolResult | None = None,
    approval_prompt: str | None = None,
) -> ResponseFrame:
    completed_actions: list[str] = []
    pending_actions: list[str] = []
    agenda_status = ""
    if agenda is not None:
        agenda_status = agenda.status.value
        for action in agenda.actions:
            label = action.name or action.kind
            if action.status.value == "completed":
                completed_actions.append(label)
            elif action.status.value in {"pending", "blocked"}:
                pending_actions.append(label)

    return ResponseFrame(
        outcome_kind=outcome_kind,
        reply=reply,
        agenda_status=agenda_status,
        completed_actions=completed_actions,
        pending_actions=pending_actions,
        missing_fields=list((state.active_intent or {}).get("missing_fields", [])) if isinstance(state.active_intent, dict) else [],
        approval_prompt=approval_prompt,
        last_tool_summary=last_tool_result.summary if last_tool_result is not None else None,
        next_action_hints=list(state.next_action_hints),
        active_run_id=state.active_run_id,
    )
