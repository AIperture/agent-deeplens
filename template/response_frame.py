from __future__ import annotations

from .types import PlanAgenda, ResponseFrame, ResponseOutcomeKind, TemplateState, ToolResult


def build_response_frame(
    *,
    reply: str,
    outcome_kind: ResponseOutcomeKind,
    state: TemplateState,
    agenda: PlanAgenda | None,
    last_tool_result: ToolResult | None = None,
    approval_prompt: str | None = None,
) -> ResponseFrame:
    completed: list[str] = []
    pending: list[str] = []
    agenda_status = ""
    if agenda is not None:
        agenda_status = agenda.status.value
        for action in agenda.actions:
            label = action.name or (action.capability.capability_name if action.capability else action.kind)
            if action.status.value == "completed":
                completed.append(label)
            elif action.status.value in {"pending", "blocked"}:
                pending.append(label)
    return ResponseFrame(
        outcome_kind=outcome_kind,
        reply=reply,
        agenda_status=agenda_status,
        completed_actions=completed,
        pending_actions=pending,
        missing_fields=list(state.runtime_missing_fields),
        approval_prompt=approval_prompt,
        last_tool_summary=last_tool_result.summary if last_tool_result is not None else None,
        next_action_hints=list(state.next_action_hints),
    )
