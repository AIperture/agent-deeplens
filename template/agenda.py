from __future__ import annotations

from .types import AgendaAction, AgendaActionStatus, AgendaStatus, PlanAgenda


def next_pending_action(agenda: PlanAgenda | None) -> AgendaAction | None:
    if agenda is None or agenda.status in {AgendaStatus.COMPLETED, AgendaStatus.CANCELED, AgendaStatus.FAILED}:
        return None
    for action in agenda.actions:
        if action.status == AgendaActionStatus.PENDING:
            return action
    return None


def mark_action_status(agenda: PlanAgenda, action_id: str, status: AgendaActionStatus) -> None:
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


def replace_pending_tail(agenda: PlanAgenda, replacement_actions: list[AgendaAction]) -> PlanAgenda:
    preserved = [action for action in agenda.actions if action.status != AgendaActionStatus.PENDING]
    agenda.actions = preserved + list(replacement_actions)
    agenda.status = AgendaStatus.ACTIVE if agenda.actions else AgendaStatus.FAILED
    return agenda
