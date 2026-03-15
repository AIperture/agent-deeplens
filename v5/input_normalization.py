from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .types import ConversationState, PendingInteraction, TaskFrame, TurnEnvelope


@dataclass
class PreInterpretationContext:
    active_task_summary: dict[str, Any] = field(default_factory=dict)
    pending_interaction: dict[str, Any] = field(default_factory=dict)
    recent_turns: list[dict[str, Any]] = field(default_factory=list)
    active_refs: list[str] = field(default_factory=list)
    last_result_summary: str | None = None


_EXPLICIT_COMMAND_RE = re.compile(r"^\s*(/[a-z0-9_:-]+(?:\s+[a-z0-9_:-]+)?)", re.IGNORECASE)


def _detect_attachment_kind(item: dict[str, Any]) -> str:
    name = str(item.get("name") or item.get("filename") or item.get("uri") or "").lower()
    if any(name.endswith(suffix) for suffix in (".json", ".zmx")):
        return "lens"
    if any(name.endswith(suffix) for suffix in (".png", ".jpg", ".jpeg", ".webp")):
        return "image"
    return str(item.get("kind") or "file")


def _normalize_attachments(attachments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in attachments or []:
        if not isinstance(item, dict):
            continue
        out = dict(item)
        out.setdefault("kind", _detect_attachment_kind(out))
        normalized.append(out)
    return normalized


def _extract_explicit_command(message: str) -> str | None:
    match = _EXPLICIT_COMMAND_RE.match(message or "")
    if not match:
        return None
    return match.group(1).strip().lower()


def _task_summary(task: TaskFrame | None) -> dict[str, Any]:
    if task is None:
        return {}
    return {
        "task_id": task.task_id,
        "domain_hint": task.domain_hint.value,
        "task_shape": task.task_shape.value,
        "preferred_tool": task.preferred_tool,
        "missing_fields": list(task.missing_fields),
        "status": task.status.value,
        "goal": task.user_goal,
    }


async def build_preinterpretation_context(
    *,
    state: ConversationState,
    context: Any,
) -> PreInterpretationContext:
    recent_turns: list[dict[str, Any]] = []
    try:
        recent_turns = await context.memory().recent_chat(
            limit=8,
            roles=["user", "assistant"],
            include_tags=False,
            include_ts=False,
            level="session",
            use_persistence=False,
        )
    except Exception:
        context.logger().warning("deeplens_v5: recent_chat lookup failed", exc_info=True)

    active_task = state.get_active_task()
    pending = state.get_pending_interaction()
    refs: list[str] = []
    if state.active_source_ref:
        refs.append(str(state.active_source_ref.get("artifact_id") or state.active_source_ref.get("uri") or state.active_source_ref.get("name")))
    if state.active_run_id:
        refs.append(f"run:{state.active_run_id}")
    refs.extend(
        [
            str(item.get("artifact_id") or item.get("uri") or item.get("name"))
            for item in state.last_artifacts[-4:]
            if item.get("artifact_id") or item.get("uri") or item.get("name")
        ]
    )
    return PreInterpretationContext(
        active_task_summary=_task_summary(active_task),
        pending_interaction=pending.to_dict() if pending else {},
        recent_turns=recent_turns[-8:],
        active_refs=refs[:8],
        last_result_summary=state.last_result_summary,
    )


def normalize_turn(
    *,
    message: str,
    attachments: list[dict[str, Any]] | None,
    state: ConversationState,
    user_meta: dict[str, Any] | None = None,
) -> TurnEnvelope:
    cleaned = (message or "").strip()
    normalized_attachments = _normalize_attachments(attachments)
    active_refs: list[str] = []
    if state.active_source_ref:
        active_refs.append(str(state.active_source_ref.get("artifact_id") or state.active_source_ref.get("uri") or state.active_source_ref.get("name")))
    if state.active_run_id:
        active_refs.append(f"run:{state.active_run_id}")
    return TurnEnvelope(
        raw_message=message or "",
        cleaned_message=cleaned,
        attachments=normalized_attachments,
        explicit_command=_extract_explicit_command(cleaned),
        ui_hints={},
        user_meta=dict(user_meta or {}),
        active_refs=active_refs,
        active_source_ref=dict(state.active_source_ref or {}),
        active_run_id=state.active_run_id,
    )
