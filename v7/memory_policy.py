from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aethergraph import NodeContext

from .types import ContextMode, DeepLensTask, RuntimeState, SESSION_MEMORY_LEVEL


SESSION_SUMMARY_TAG = "session"
SESSION_SUMMARY_KIND = "long_term_summary"


@dataclass
class PromptContextBundle:
    working_state: dict[str, Any] = field(default_factory=dict)
    recent_messages: list[dict[str, Any]] = field(default_factory=list)
    long_term_summary: dict[str, Any] | None = None
    prompt_segments: dict[str, Any] = field(default_factory=dict)


async def maybe_distill_session_summary(context: NodeContext) -> None:
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


def _working_state(task: DeepLensTask, state: RuntimeState, artifact_limit: int) -> dict[str, Any]:
    return {
        "goal": task.user_goal,
        "requested_capabilities": task.requested_capabilities,
        "design_spec": task.design_spec,
        "analysis_request": task.analysis_request,
        "run_request": task.run_request,
        "delivery_request": task.delivery_request,
        "lens_source": task.lens_source,
        "response_request": task.response_request,
        "notes": task.notes[-8:],
        "active_run_id": state.active_run_id,
        "active_lens_ref": state.active_lens_ref,
        "active_source_ref": state.active_source_ref,
        "last_metrics": state.last_metrics,
        "last_analysis_bundle": state.last_analysis_bundle,
        "last_artifacts": state.last_artifacts[-artifact_limit:],
        "pending_runs": state.pending_runs[-5:],
        "design_draft": state.design_draft,
    }


async def build_context_bundle(
    *,
    context_mode: ContextMode,
    task: DeepLensTask,
    state: RuntimeState,
    context: NodeContext,
) -> PromptContextBundle:
    mem = context.memory()
    if context_mode == ContextMode.FULL:
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

    recent = await mem.recent_chat(
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
        recent_messages=recent,
        long_term_summary=hydrated,
        prompt_segments=segments,
    )
