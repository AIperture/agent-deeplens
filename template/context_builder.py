from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import policy
from .types import ContextMode, TemplateState, TemplateTask


@dataclass
class PromptContextBundle:
    recent_messages: list[dict[str, Any]] = field(default_factory=list)
    working_state: dict[str, Any] = field(default_factory=dict)
    candidate_field_values: dict[str, Any] = field(default_factory=dict)
    prior_tool_outputs: dict[str, Any] = field(default_factory=dict)
    recent_failures: list[dict[str, Any]] = field(default_factory=list)
    current_agenda_summary: list[dict[str, Any]] = field(default_factory=list)


def _candidate_field_values(task: TemplateTask, state: TemplateState) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (task.parsed_args or {}).items():
        out[key] = {"value": value, "source": "task.parsed_args"}
    for key, resolution in (task.field_map or {}).items():
        out[key] = {"value": resolution.value, "source": resolution.source.value}
    for key, value in (state.memory_bag or {}).items():
        out.setdefault(key, {"value": value, "source": "state.memory_bag"})
    return out


async def build_context_bundle(
    *,
    context_mode: ContextMode,
    task: TemplateTask,
    state: TemplateState,
    context: Any,
) -> PromptContextBundle:
    limit = policy.FULL_CHAT_LIMIT if context_mode == ContextMode.FULL else policy.LITE_CHAT_LIMIT
    recent = await context.memory().recent_chat(
        limit=limit,
        roles=["user", "assistant"],
        include_tags=False,
        include_ts=False,
        level=policy.SESSION_MEMORY_LEVEL,
        use_persistence=False,
    )
    agenda_summary: list[dict[str, Any]] = []
    active_agenda = state.active_agenda if isinstance(state.active_agenda, dict) else {}
    for action in list(active_agenda.get("actions") or [])[-6:]:
        agenda_summary.append({
            "action_id": action.get("action_id"),
            "kind": action.get("kind"),
            "name": action.get("name"),
            "status": action.get("status"),
        })
    return PromptContextBundle(
        recent_messages=recent,
        working_state={
            "task": task.to_dict(),
            "state_memory_bag": dict(state.memory_bag),
            "runtime_missing_fields": list(state.runtime_missing_fields),
            "runtime_invalid_fields": dict(state.runtime_invalid_fields),
        },
        candidate_field_values=_candidate_field_values(task, state),
        prior_tool_outputs=dict(state.prior_tool_outputs),
        recent_failures=list(state.recent_failures[-4:]),
        current_agenda_summary=agenda_summary,
    )
