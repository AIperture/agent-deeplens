from __future__ import annotations

from .types import ActionAgenda, AgendaAction, AgendaActionStatus, AgendaStatus


def next_pending_action(agenda: ActionAgenda) -> AgendaAction | None:
    for action in agenda.actions:
        if action.status == AgendaActionStatus.PENDING:
            return action
    return None


def mark_action_status(agenda: ActionAgenda, action_id: str, status: AgendaActionStatus) -> None:
    for action in agenda.actions:
        if action.action_id == action_id:
            action.status = status
            break
    if any(action.status == AgendaActionStatus.FAILED for action in agenda.actions):
        agenda.status = AgendaStatus.FAILED
    elif all(action.status in {AgendaActionStatus.COMPLETED, AgendaActionStatus.CANCELED} for action in agenda.actions):
        agenda.status = AgendaStatus.COMPLETED
    elif any(action.status == AgendaActionStatus.BLOCKED for action in agenda.actions):
        agenda.status = AgendaStatus.WAITING
    else:
        agenda.status = AgendaStatus.ACTIVE

