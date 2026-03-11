from __future__ import annotations

import uuid
from typing import Any

from .extraction import ExtractionBundle, extract_turn_fields
from .input_normalization import build_preinterpretation_context, normalize_turn
from .state import get_active_task, get_pending_interaction
from .types import (
    ConversationState,
    DomainHint,
    FieldSource,
    FieldValue,
    InterpreterDecision,
    PendingInteraction,
    PendingInteractionKind,
    TaskFrame,
    TaskShape,
    TaskStatus,
    TurnEnvelope,
    TurnRole,
)


TOPIC_SWITCH_HINTS = {"instead", "never mind", "actually", "forget that", "switch", "new task"}
CONTROL_HINTS = {"cancel", "stop", "abort", "status", "/mode", "help"}
DIRECT_REPLY_HINTS = {"what is", "how does", "explain", "why", "interpret"}


def _new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:10]}"


def _looks_like_topic_switch(text: str) -> bool:
    lowered = (text or "").lower()
    return any(token in lowered for token in TOPIC_SWITCH_HINTS)


def _looks_like_direct_reply(text: str) -> bool:
    lowered = (text or "").lower()
    return any(token in lowered for token in DIRECT_REPLY_HINTS)


def _looks_like_control(text: str) -> bool:
    lowered = (text or "").lower()
    return any(token in lowered for token in CONTROL_HINTS)


def _make_blank_task(message: str) -> TaskFrame:
    return TaskFrame(
        task_id=_new_task_id(),
        user_goal=message.strip(),
        domain_hint=DomainHint.UNKNOWN,
        task_shape=TaskShape.UNSUPPORTED,
        status=TaskStatus.ACTIVE,
    )


def _set_field(task: TaskFrame, name: str, value: FieldValue) -> None:
    task.field_map[name] = value
    if name in {"fov", "fnum", "foclen", "imgh"}:
        task.design_spec[name] = value.value
    elif name == "analysis_mode":
        task.analysis_request["mode"] = value.value
    elif name == "export_formats":
        task.delivery_request["formats"] = list(value.value or [])
    elif name == "run_id":
        task.run_request["run_id"] = value.value
    elif name == "approval_response":
        task.run_request["approval_response"] = value.value
    elif name == "lens_source" and isinstance(value.value, dict):
        task.lens_source = dict(value.value)
    elif name == "allows_defaults" and value.value:
        if "Allowed defaults for missing design values." not in task.assumptions:
            task.assumptions.append("Allowed defaults for missing design values.")


def _hydrate_from_state(task: TaskFrame, state: ConversationState) -> None:
    if state.design_draft:
        for key, value in state.design_draft.items():
            task.design_spec.setdefault(key, value)
            if key not in task.field_map:
                task.field_map[key] = FieldValue(
                    value=value,
                    source=FieldSource.STATE,
                    confidence=0.65,
                    inferred=True,
                    notes=["Hydrated from prior design draft."],
                )
    if state.active_source_ref and not task.lens_source:
        task.lens_source = dict(state.active_source_ref)
        task.field_map.setdefault(
            "lens_source",
            FieldValue(
                value=dict(state.active_source_ref),
                source=FieldSource.STATE,
                confidence=0.72,
                inferred=True,
                notes=["Hydrated from active source ref."],
            ),
        )
    if state.active_run_id and not task.run_request.get("run_id"):
        task.run_request["run_id"] = state.active_run_id


def _apply_extraction(task: TaskFrame, bundle: ExtractionBundle) -> None:
    for key, field in (bundle.field_map or {}).items():
        _set_field(task, key, field)
    task.notes.extend([note for note in bundle.notes if note])


def _has_lens_attachment(task: TaskFrame, envelope: TurnEnvelope) -> bool:
    """Check if task or envelope has any lens-like attachments (.json/.zmx)."""
    for att in list(task.attachments) + list(envelope.attachments):
        name = str(att.get("name") or att.get("filename") or att.get("uri") or "").lower()
        if name.endswith((".json", ".zmx")):
            return True
        if att.get("kind") == "lens":
            return True
    return False


def _detect_domain(task: TaskFrame, envelope: TurnEnvelope) -> None:
    text = envelope.cleaned_message.lower()
    explicit = envelope.explicit_command or ""
    # Analysis keywords — use "simulat" to match both "simulate" and "simulation".
    if explicit.startswith("/analysis") or any(x in text for x in ["analy", "spot", "mtf", "rms", "simulat"]):
        task.domain_hint = DomainHint.ANALYSIS
        has_source = bool(task.lens_source) or _has_lens_attachment(task, envelope)
        task.task_shape = TaskShape.SINGLE_ACTION if has_source else TaskShape.MULTI_STEP
        task.preferred_tool = "dl.analysis"
        return
    # Design keywords — include common natural language variations.
    if explicit.startswith("/design") or any(x in text for x in [
        "design", "create lens", "create a lens", "build lens", "build a lens",
        "starting lens", "make a lens", "make lens", "new lens",
        "fov", "fnum", "f/#",
    ]):
        task.domain_hint = DomainHint.DESIGN
        task.task_shape = TaskShape.MULTI_STEP
        task.preferred_tool = "dl.create_lens"
        return
    if explicit.startswith("/optimize") or any(x in text for x in ["optimize", "optimization", "improve lens", "constraint", "objective"]):
        task.domain_hint = DomainHint.OPTIMIZATION
        task.task_shape = TaskShape.MULTI_STEP
        task.preferred_tool = "ag.spawn_graph"
        return
    if any(x in text for x in ["export", "zmx", "download", "save"]):
        task.domain_hint = DomainHint.EXPORT
        task.task_shape = TaskShape.SINGLE_ACTION
        task.preferred_tool = "dl.export_lens"
        return
    if "status" in text or any(x in text for x in ["cancel", "stop", "abort"]):
        task.domain_hint = DomainHint.RUN_CONTROL
        task.task_shape = TaskShape.RUN_CONTROL
        task.preferred_tool = "ag.cancel" if any(x in text for x in ["cancel", "stop", "abort"]) else "ag.status"
        return
    # Attachment-first routing: if user has lens files but no clear domain keyword,
    # default to analysis (matches v3's _fallback_route behavior).
    if _has_lens_attachment(task, envelope):
        task.domain_hint = DomainHint.ANALYSIS
        task.task_shape = TaskShape.SINGLE_ACTION
        task.preferred_tool = "dl.analysis"
        return
    if _looks_like_direct_reply(text):
        task.domain_hint = DomainHint.CHAT
        task.task_shape = TaskShape.DIRECT_ANSWER


def _task_has_lens_file(task: TaskFrame) -> bool:
    """Check if the task has a usable lens source or lens-like attachment."""
    if task.lens_source:
        return True
    for att in task.attachments:
        name = str(att.get("name") or att.get("filename") or att.get("uri") or "").lower()
        if name.endswith((".json", ".zmx")):
            return True
        if att.get("kind") == "lens":
            return True
    return False


def _recompute_missing_fields(task: TaskFrame) -> None:
    missing: list[str] = []
    if task.domain_hint == DomainHint.DESIGN:
        if "fov" not in task.design_spec:
            missing.append("fov")
        if "fnum" not in task.design_spec:
            missing.append("fnum")
        if "foclen" not in task.design_spec and "imgh" not in task.design_spec:
            missing.append("foclen_or_imgh")
    elif task.domain_hint in {DomainHint.ANALYSIS, DomainHint.OPTIMIZATION, DomainHint.EXPORT}:
        # Check both lens_source and attachments (v3 used attachment_suggests_lens).
        if not _task_has_lens_file(task):
            missing.append("lens_source")
        if task.domain_hint == DomainHint.EXPORT and not task.delivery_request.get("formats"):
            missing.append("export_format")
    elif task.domain_hint == DomainHint.RUN_CONTROL:
        if not task.run_request.get("run_id"):
            missing.append("run_id")
    task.missing_fields = list(dict.fromkeys(missing))


def _build_control_reply(text: str, state: ConversationState) -> str | None:
    lowered = (text or "").lower()
    if lowered.startswith("/mode"):
        if "full" in lowered:
            return "DeepLens v5 uses bounded, task-local context by default; there is no separate full mode here."
        if "lite" in lowered:
            return "DeepLens v5 already uses limited context for interpretation and response composition."
    if "help" in lowered:
        return "I can help with lens design, analysis, export, optimization runs, and run status or cancellation."
    if "status" in lowered and state.active_run_id:
        return None
    if any(token in lowered for token in ("cancel", "stop", "abort")) and state.active_run_id:
        return None
    return None


def _pending_answer_matches(pending: PendingInteraction, bundle: ExtractionBundle) -> bool:
    if pending.kind == PendingInteractionKind.APPROVAL:
        return "approval_response" in bundle.field_map
    if pending.expected_fields:
        return any(field in bundle.field_map for field in pending.expected_fields)
    return bool(bundle.field_map)


async def interpret_turn(
    *,
    message: str,
    attachments: list[dict[str, Any]] | None,
    state: ConversationState,
    context: Any,
    user_meta: dict[str, Any] | None = None,
) -> InterpreterDecision:
    envelope = normalize_turn(
        message=message,
        attachments=attachments,
        state=state,
        user_meta=user_meta,
    )
    print(f"🍎 Normalized turn envelope: {envelope}")
    pre = await build_preinterpretation_context(state=state, context=context)
    active_task = get_active_task(state)
    pending = get_pending_interaction(state)
    text = envelope.cleaned_message.strip()

    extracted = await extract_turn_fields(
        envelope=envelope,
        pending=pending,
        pre_context={
            "active_task": pre.active_task_summary,
            "pending_interaction": pre.pending_interaction,
            "recent_turns": pre.recent_turns[-4:],
            "active_refs": pre.active_refs,
            "last_result_summary": pre.last_result_summary,
        },
        context=context,
    )

    if pending is not None and not _looks_like_topic_switch(text) and _pending_answer_matches(pending, extracted):
        task = TaskFrame.from_dict(active_task.to_dict()) if active_task is not None else _make_blank_task(text)
        task.attachments.extend(envelope.attachments)
        _apply_extraction(task, extracted)
        task.status = TaskStatus.ACTIVE
        _recompute_missing_fields(task)
        print(f"🍎 Merged turn fields into pending interaction {pending.interaction_id} with extracted data: {extracted}")
        return InterpreterDecision(
            turn_role=TurnRole.ANSWER_PENDING_INTERACTION,
            task=task,
            clear_pending_interaction=True,
            notes=["Merged current turn as an answer to the pending interaction."],
        )

    if _looks_like_control(text):
        reply = _build_control_reply(text, state)
        if reply is not None:
            print(f"🍎 Handling control/meta turn with reply: {reply}")
            return InterpreterDecision(
                turn_role=TurnRole.CONTROL_OR_META,
                direct_reply=reply,
                clear_pending_interaction=False,
                notes=["Handled control/meta turn directly."],
            )

    if active_task is not None and _looks_like_topic_switch(text):
        active_task.status = TaskStatus.SUPERSEDED

    if active_task is not None and not _looks_like_topic_switch(text) and not envelope.explicit_command and active_task.domain_hint != DomainHint.CHAT:
        task = TaskFrame.from_dict(active_task.to_dict())
        task.attachments.extend(envelope.attachments)
        task.user_goal = f"{task.user_goal}\nFollow-up: {text}"
        _apply_extraction(task, extracted)
        _detect_domain(task, envelope)
        _recompute_missing_fields(task)
        return InterpreterDecision(
            turn_role=TurnRole.UPDATE_OPEN_TASK,
            task=task,
            clear_pending_interaction=False,
            notes=["Patched the active task from a follow-up turn."],
        )

    task = _make_blank_task(text)
    task.attachments.extend(envelope.attachments)
    _hydrate_from_state(task, state)
    _apply_extraction(task, extracted)
    _detect_domain(task, envelope)
    _recompute_missing_fields(task)

    if task.domain_hint == DomainHint.UNKNOWN and envelope.attachments:
        task.domain_hint = DomainHint.ANALYSIS
        task.task_shape = TaskShape.SINGLE_ACTION
        task.preferred_tool = "dl.analysis"
        _recompute_missing_fields(task)

    if task.domain_hint == DomainHint.CHAT:
        # Only return a canned direct reply for pure chat with no active context.
        print(f"🍎 Handling direct reply turn with detected domain {task.domain_hint} and shape {task.task_shape}")
        return InterpreterDecision(
            turn_role=TurnRole.DIRECT_REPLY,
            direct_reply="I can help with lens design, analysis, export, optimization, and run control.",
            notes=["No executable task was detected from the current turn."],
        )

    if task.domain_hint == DomainHint.UNKNOWN:
        # Route question-like messages (interpret, explain, why) as CHAT tasks
        # through the loop so the LLM can provide a contextual answer, rather
        # than returning a canned response.
        if _looks_like_direct_reply(text):
            task.domain_hint = DomainHint.CHAT
            task.task_shape = TaskShape.DIRECT_ANSWER
            print(f"🍎 Routing question-like turn as CHAT task through loop: {text[:80]}")
            return InterpreterDecision(
                turn_role=TurnRole.NEW_TASK,
                task=task,
                replace_active_task=True,
                clear_pending_interaction=True,
                notes=["Routed question-like turn as CHAT task for LLM-based answer."],
            )
        print(f"🍎 Unable to detect a clear domain or task shape from the turn; falling back to direct reply with detected hints: {task.domain_hint}, {task.task_shape}")
        return InterpreterDecision(
            turn_role=TurnRole.DIRECT_REPLY,
            direct_reply="I can help with lens design, analysis, optimization, export, or run status/cancellation.",
            notes=["Fell back to bounded direct reply."],
        )

    print(f"🍎 Created new task from turn with detected domain {task.domain_hint} and shape {task.task_shape}")

    return InterpreterDecision(
        turn_role=TurnRole.NEW_TASK,
        task=task,
        replace_active_task=True,
        clear_pending_interaction=True,
        notes=["Created a new v5 task from the current turn."],
    )
