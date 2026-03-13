"""Agenda data operations: pure data manipulation with no domain logic.

Directly extracted from v6/planning/agenda.py — stable across all verticals.
"""
from __future__ import annotations

from .types import ActionAgenda, AgendaAction, AgendaActionStatus, AgendaStatus


def next_pending_action(agenda: ActionAgenda | None) -> AgendaAction | None:
    """Return the first pending action, or None if the agenda is done."""
    if agenda is None or agenda.status in {AgendaStatus.FAILED, AgendaStatus.CANCELED, AgendaStatus.COMPLETED}:
        return None
    for action in agenda.actions:
        if action.status == AgendaActionStatus.PENDING:
            return action
    return None


def mark_action_status(agenda: ActionAgenda, action_id: str, status: AgendaActionStatus) -> None:
    """Update an action's status and recompute the agenda-level status."""
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


def replace_pending_tail(agenda: ActionAgenda, replacement_actions: list[AgendaAction]) -> ActionAgenda:
    """Replace all pending actions with a new set (used during replanning)."""
    preserved = [action for action in agenda.actions if action.status != AgendaActionStatus.PENDING]
    for idx, action in enumerate(replacement_actions, start=1):
        if not action.action_id:
            action.action_id = f"r{idx}"
    agenda.actions = preserved + list(replacement_actions)
    agenda.status = AgendaStatus.ACTIVE if agenda.actions else AgendaStatus.FAILED
    return agenda
