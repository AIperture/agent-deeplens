from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aethergraph import NodeContext

from ..types import ContextMode, DeepLensState, DeepLensTask


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


async def maybe_distill_session_summary(context: NodeContext) -> None:
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


def _working_state(task: DeepLensTask, state: DeepLensState, artifact_limit: int) -> dict[str, Any]:
    return {
        "task_shape": task.task_shape.value,
        "domain_hint": task.domain_hint.value,
        "preferred_tool": task.preferred_tool,
        "task_notes": task.notes[-8:],
        "requested_capabilities": task.requested_capabilities,
        "sequencing_hints": task.sequencing_hints,
        "intent_summary": task.intent_summary,
        "lens_source": task.lens_source,
        "design_spec": task.design_spec,
        "analysis_request": task.analysis_request,
        "run_request": task.run_request,
        "delivery_request": task.delivery_request,
        "source_refs": task.source_refs[-4:],
        "active_artifact_refs": task.active_artifact_refs[-8:],
        "parsed_args": task.parsed_args,
        "missing_fields": task.missing_fields,
        "field_map": {
            key: {
                "value": value.value,
                "source": value.source.value,
                "confidence": value.confidence,
            }
            for key, value in task.field_map.items()
        },
        "active_intent": state.active_intent,
        "active_agenda": state.active_agenda,
        "active_run_id": state.active_run_id,
        "active_lens_ref": state.active_lens_ref,
        "active_source_ref": state.active_source_ref,
        "last_metrics": state.last_metrics,
        "last_analysis_bundle": state.last_analysis_bundle,
        "design_draft": state.design_draft,
        "requested_next_step": state.requested_next_step,
        "next_action_hints": state.next_action_hints,
        "last_artifacts": state.last_artifacts[-artifact_limit:],
        "pending_runs": state.pending_runs[-5:],
        "pending_action": state.pending_action,
        "pending_approval": state.pending_approval,
        "retry_counters": state.retry_counters,
        "recovery_attempts": state.recovery_attempts,
        "active_recovery": state.active_recovery,
        "last_replan_reason": state.last_replan_reason,
        "last_tool_result": state.last_tool_result,
        "failure_history_tail": state.failure_history[-4:],
        "current_loop_trace_tail": state.loop_trace[-6:],
        "previous_loop_summaries": state.loop_history[-3:],
    }


async def build_lite_context(
    *,
    task: DeepLensTask,
    state: DeepLensState,
    context: NodeContext,
) -> PromptContextBundle:
    mem = context.memory()
    recent_chat = await mem.recent_chat(
        limit=6,
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
        working_state=_working_state(task, state, artifact_limit=4),
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
        recent_chat_include_tags=False,
        recent_chat_include_ts=False,
        level=SESSION_MEMORY_LEVEL,
        use_persistence=False,
    )
    return PromptContextBundle(
        working_state=_working_state(task, state, artifact_limit=8),
        recent_messages=history.get("messages", []),
        long_term_summary={"summary": history.get("summary")} if history.get("summary") else None,
        prompt_segments=segments,
    )


async def build_context_bundle(
    *,
    context_mode: ContextMode,
    task: DeepLensTask,
    state: DeepLensState,
    context: NodeContext,
) -> PromptContextBundle:
    if context_mode == ContextMode.FULL:
        return await build_full_context(task=task, state=state, context=context)
    return await build_lite_context(task=task, state=state, context=context)
