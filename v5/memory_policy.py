from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .types import ConversationState, TaskFrame


SESSION_SUMMARY_TAG = "session"
SESSION_SUMMARY_KIND = "long_term_summary"
SESSION_MEMORY_LEVEL = "session"


@dataclass
class PromptContextBundle:
    working_state: dict[str, Any] = field(default_factory=dict)
    recent_messages: list[dict[str, Any]] = field(default_factory=list)
    long_term_summary: dict[str, Any] | None = None
    prompt_segments: dict[str, Any] = field(default_factory=dict)


def _working_state(task: TaskFrame, state: ConversationState, artifact_limit: int) -> dict[str, Any]:
    return {
        "task_shape": task.task_shape.value,
        "domain_hint": task.domain_hint.value,
        "preferred_tool": task.preferred_tool,
        "task_notes": task.notes[-8:],
        "lens_source": task.lens_source,
        "design_spec": task.design_spec,
        "analysis_request": task.analysis_request,
        "run_request": task.run_request,
        "delivery_request": task.delivery_request,
        "source_refs": task.source_refs[-4:],
        "active_artifact_refs": task.active_artifact_refs[-8:],
        "missing_fields": task.missing_fields,
        "active_run_id": state.active_run_id,
        "active_lens_ref": state.active_lens_ref,
        "active_source_ref": state.active_source_ref,
        "last_metrics": state.last_metrics,
        "last_analysis_bundle": state.last_analysis_bundle,
        "design_draft": state.design_draft,
        "requested_next_step": state.requested_next_step,
        "last_artifacts": state.last_artifacts[-artifact_limit:],
        "pending_runs": state.pending_runs[-5:],
        "pending_action": state.pending_action,
        "pending_approval": state.pending_approval,
        "retry_counters": state.retry_counters,
        "current_loop_trace_tail": state.loop_trace[-6:],
        "previous_loop_summaries": state.loop_history[-3:],
    }


async def build_context_bundle(
    *,
    task: TaskFrame,
    state: ConversationState,
    context: Any,
) -> PromptContextBundle:
    """Build a lite context bundle with recent messages and working state."""
    mem = context.memory()
    recent_chat: list[dict[str, Any]] = []
    try:
        recent_chat = await mem.recent_chat(
            limit=6,
            roles=["user", "assistant"],
            include_tags=False,
            include_ts=False,
            level=SESSION_MEMORY_LEVEL,
            use_persistence=False,
        )
    except Exception:
        context.logger().warning("deeplens_v5: recent_chat lookup failed", exc_info=True)

    long_term_summary = None
    try:
        long_term_summary = await mem.soft_hydrate_last_summary(
            summary_tag=SESSION_SUMMARY_TAG,
            summary_kind=SESSION_SUMMARY_KIND,
            level=SESSION_MEMORY_LEVEL,
        )
    except Exception:
        context.logger().warning("deeplens_v5: soft_hydrate_last_summary failed", exc_info=True)

    segments: dict[str, Any] = {}
    try:
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
    except Exception:
        context.logger().warning("deeplens_v5: build_prompt_segments failed", exc_info=True)

    return PromptContextBundle(
        working_state=_working_state(task, state, artifact_limit=4),
        recent_messages=recent_chat,
        long_term_summary=long_term_summary,
        prompt_segments=segments,
    )
