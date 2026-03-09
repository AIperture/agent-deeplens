from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aethergraph import NodeContext

from .types import ContextMode, DeepLensState, DeepLensTask

@dataclass
class PromptContextBundle:
    system_segments: list[str] = field(default_factory=list)
    working_state: dict[str, Any] = field(default_factory=dict)
    recent_messages: list[dict[str, Any]] = field(default_factory=list)
    long_term_summary: dict[str, Any] | None = None
    prompt_segments: dict[str, Any] = field(default_factory=dict)

SESSION_SUMMARY_TAG = "session"
SESSION_SUMMARY_KIND = "long_term_summary"
SESSION_MEMORY_LEVEL = "session"


async def maybe_distill_session_summary(context: NodeContext):
    mem = context.memory()
    recent_for_distill = await mem.recent_chat(
        limit=120,
        level=SESSION_MEMORY_LEVEL,
        use_persistence=True,
    )
    if len(recent_for_distill) < 80:
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

async def build_lite_context(
    *,
    task: DeepLensTask,
    state: DeepLensState,
    context: Any,
) -> PromptContextBundle:
    mem = context.memory()

    recent_chat = await mem.recent_chat(
        limit=6,
        roles=["user", "assistant"],
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
        level=SESSION_MEMORY_LEVEL,
        use_persistence=False,
    )

    return PromptContextBundle(
        working_state={
            "intent": task.intent.value,
            "parsed_args": task.parsed_args,
            "missing_fields": task.missing_fields,
            "constraints": task.constraints,
            "last_metrics": state.last_metrics,
            "pending_action": state.pending_action,
            "pending_approval": state.pending_approval,
        },
        recent_messages=recent_chat,
        long_term_summary=hydrated,
        prompt_segments=segments,
    )


async def build_full_context(
    *,
    task: DeepLensTask,
    state: DeepLensState,
    context: NodeContext,
) -> PromptContextBundle:
    mem = context.memory()

    history = await mem.chat_history_for_llm(
        limit=20,
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
        level=SESSION_MEMORY_LEVEL,
        use_persistence=False,
    )

    return PromptContextBundle(
        working_state={
            "intent": task.intent.value,
            "parsed_args": task.parsed_args,
            "missing_fields": task.missing_fields,
            "pending_specs": state.pending_specs,
            "constraints": state.constraints,
            "last_metrics": state.last_metrics,
            "last_artifacts": state.last_artifacts[-8:],
            "pending_action": state.pending_action,
            "pending_approval": state.pending_approval,
            "active_task": state.active_task,
        },
        recent_messages=history.get("messages", []),
        long_term_summary={"summary": history.get("summary")} if history.get("summary") else None,
        prompt_segments=segments,
    )


async def build_context_bundle(
    *,
    context_mode: ContextMode,
    task: DeepLensTask,
    state: DeepLensState,
    context: Any,
) -> PromptContextBundle:
    if context_mode == ContextMode.FULL:
        return await build_full_context(task=task, state=state, context=context)
    return await build_lite_context(task=task, state=state, context=context)
