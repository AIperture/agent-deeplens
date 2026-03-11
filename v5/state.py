from __future__ import annotations

from typing import Any

from .types import ConversationState, PendingInteraction, TaskFrame


def append_trace(state: ConversationState, event: str, **payload: Any) -> None:
    state.loop_trace.append({"event": event, "payload": payload})
    state.loop_trace = state.loop_trace[-80:]


def get_active_task(state: ConversationState) -> TaskFrame | None:
    return state.get_active_task()


def set_active_task(state: ConversationState, task: TaskFrame | None) -> None:
    state.set_active_task(task)


def get_pending_interaction(state: ConversationState) -> PendingInteraction | None:
    return state.get_pending_interaction()


def set_pending_interaction(state: ConversationState, pending: PendingInteraction | None) -> None:
    state.set_pending_interaction(pending)


def increment_retry(state: ConversationState, key: str) -> int:
    state.retry_counters[key] = state.retry_counters.get(key, 0) + 1
    return state.retry_counters[key]


def reset_retry(state: ConversationState, key: str) -> None:
    state.retry_counters.pop(key, None)


def update_last_result_summary(state: ConversationState, summary: str | None) -> None:
    state.last_result_summary = summary or state.last_result_summary


def set_next_action_hints(state: ConversationState, hints: list[str] | None) -> None:
    state.next_action_hints = list(hints or [])[:6]
