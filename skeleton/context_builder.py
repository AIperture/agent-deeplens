"""Context/prompt bundle building.

Generic version of v6's context/memory_policy.py. Working state includes
task.domain_data and state.domain_state wholesale instead of manually
plucking domain-specific fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import policy
from .types import AgentState, ContextMode, Task


SESSION_SUMMARY_TAG = "session"
SESSION_SUMMARY_KIND = "long_term_summary"
SESSION_MEMORY_LEVEL = "session"


@dataclass
class PromptContextBundle:
    system_segments: list[str] = field(default_factory=list)
    working_state: dict[str, Any] = field(default_factory=dict)
    recent_messages: list[dict[str, Any]] = field(default_factory=list)
    long_term_summary: dict[str, Any] | None = None
    prompt_segments: dict[str, Any] = field(default_factory=dict)


async def maybe_distill_session_summary(context: Any) -> None:
    """Distill session summary when chat history is long enough."""
    mem = context.memory()
    recent = await mem.recent_chat(
        limit=120,
        level=SESSION_MEMORY_LEVEL,
        use_persistence=True,
    )
    if len(recent) < 80:
        return
    await mem.distill_long_term(
        level=SESSION_MEMORY_LEVEL,
        summary_tag=SESSION_SUMMARY_TAG,
        summary_kind=SESSION_SUMMARY_KIND,
        include_kinds=["chat.turn"],
        include_tags=["chat"],
        max_events=200,
        use_llm=False,
    )


def _working_state(task: Task, state: AgentState) -> dict[str, Any]:
    """Build working state dict for LLM context. Domain-agnostic + domain bags."""
    return {
        # Task fields
        "user_goal": task.user_goal,
        "task_shape": task.task_shape.value,
        "domain_hint": task.domain_hint,
        "preferred_tool": task.preferred_tool,
        "task_notes": task.notes[-8:],
        "requested_capabilities": task.requested_capabilities,
        "sequencing_hints": task.sequencing_hints,
        "intent_summary": task.intent_summary,
        "parsed_args": task.parsed_args,
        "missing_fields": task.missing_fields,
        "domain_data": task.domain_data,
        # State fields
        "active_intent": state.active_intent,
        "active_agenda": state.active_agenda,
        "next_action_hints": state.next_action_hints,
        "pending_action": state.pending_action,
        "pending_approval": state.pending_approval,
        "retry_counters": state.retry_counters,
        "recovery_attempts": state.recovery_attempts,
        "active_recovery": state.active_recovery,
        "last_replan_reason": state.last_replan_reason,
        "last_tool_result": state.last_tool_result,
        "last_attempt": state.last_attempt,
        "attempt_history_tail": state.attempt_history[-4:],
        "runtime_missing_fields": state.runtime_missing_fields,
        "runtime_invalid_fields": state.runtime_invalid_fields,
        "last_prompt_reason": state.last_prompt_reason,
        "failure_history_tail": state.failure_history[-4:],
        "current_loop_trace_tail": state.loop_trace[-6:],
        "previous_loop_summaries": state.loop_history[-3:],
        "domain_state": state.domain_state,
    }


async def build_lite_context(
    *,
    task: Task,
    state: AgentState,
    context: Any,
) -> PromptContextBundle:
    mem = context.memory()
    recent_chat = await mem.recent_chat(
        limit=policy.LITE_CHAT_LIMIT,
        roles=["user", "assistant"],
        include_tags=False,
        include_ts=False,
        level=SESSION_MEMORY_LEVEL,
        use_persistence=False,
    )
    hydrated = await mem.soft_hydrate_last_summary(
        summary_tag=SESSION_SUMMARY_TAG,
        summary_kind=SESSION_SUMMARY_KIND,
        level=SESSION_MEMORY_LEVEL,
    )
    segments = await mem.build_prompt_segments(
        recent_chat_limit=4,
        include_long_term=True,
        summary_tag=SESSION_SUMMARY_TAG,
        summary_kind=SESSION_SUMMARY_KIND,
        include_recent_tools=True,
        tool_limit=4,
        recent_chat_include_tags=False,
        recent_chat_include_ts=False,
        level=SESSION_MEMORY_LEVEL,
        use_persistence=False,
    )
    return PromptContextBundle(
        working_state=_working_state(task, state),
        recent_messages=recent_chat,
        long_term_summary=hydrated,
        prompt_segments=segments,
    )


async def build_full_context(
    *,
    task: Task,
    state: AgentState,
    context: Any,
) -> PromptContextBundle:
    mem = context.memory()
    history = await mem.chat_history_for_llm(
        limit=policy.FULL_CHAT_LIMIT,
        include_system_summary=True,
        summary_tag=SESSION_SUMMARY_TAG,
        summary_kind=SESSION_SUMMARY_KIND,
        level=SESSION_MEMORY_LEVEL,
        use_persistence=False,
    )
    segments = await mem.build_prompt_segments(
        recent_chat_limit=10,
        include_long_term=True,
        summary_tag=SESSION_SUMMARY_TAG,
        summary_kind=SESSION_SUMMARY_KIND,
        include_recent_tools=True,
        tool_limit=8,
        recent_chat_include_tags=False,
        recent_chat_include_ts=False,
        level=SESSION_MEMORY_LEVEL,
        use_persistence=False,
    )
    return PromptContextBundle(
        working_state=_working_state(task, state),
        recent_messages=history.get("messages", []),
        long_term_summary={"summary": history.get("summary")} if history.get("summary") else None,
        prompt_segments=segments,
    )


async def build_context_bundle(
    *,
    context_mode: ContextMode,
    task: Task,
    state: AgentState,
    context: Any,
) -> PromptContextBundle:
    if context_mode == ContextMode.FULL:
        return await build_full_context(task=task, state=state, context=context)
    return await build_lite_context(task=task, state=state, context=context)
