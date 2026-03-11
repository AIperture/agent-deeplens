from __future__ import annotations

import uuid
from typing import Any

from .input_normalization import normalize_turn 
from .extraction import extract_analysis_fields, extract_design_fields
from .state import get_active_task, get_pending_interaction
from .types import (
    ConversationState,
    DomainHint,
    FieldSource,
    FieldValue,
    InterpreterDecision,
    PendingInteractionKind,
    TaskFrame,
    TaskShape,
    TaskStatus,
    TurnEnvelope,
    TurnRole,
)


# ---------------------------------------------------------------------
# What to implement here
# ---------------------------------------------------------------------
# - Turn-role classification:
#     * answer pending interaction
#     * update open task
#     * new task
#     * control/meta
#     * direct reply
# - Lightweight cross-turn continuation judgment
# - Task create / patch / supersede
#
# What should NOT be included here
# - Tool dispatch
# - Tool validation
# - Repair loops
# - Multi-step execution logic
# - Controller branching from v4
# ---------------------------------------------------------------------


TOPIC_SWITCH_HINTS = {"instead", "never mind", "actually", "forget that", "switch"}


def _new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:10]}"


def _looks_like_topic_switch(text: str) -> bool:
    lowered = (text or "").lower()
    return any(x in lowered for x in TOPIC_SWITCH_HINTS)


def _looks_like_short_answer(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if len(stripped.split()) <= 6:
        return True
    if any(ch.isdigit() for ch in stripped) and len(stripped.split()) <= 14:
        return True
    return False


def _make_blank_task(message: str) -> TaskFrame:
    return TaskFrame(
        task_id=_new_task_id(),
        user_goal=message.strip(),
        domain_hint=DomainHint.UNKNOWN,
        task_shape=TaskShape.UNSUPPORTED,
        status=TaskStatus.ACTIVE,
    )


def _set_field(
    task: TaskFrame,
    name: str,
    value: Any,
    *,
    source: FieldSource,
    confidence: float,
    inferred: bool = False,
    confirmed: bool = False,
    note: str | None = None,
) -> None:
    if value is None:
        return
    task.field_map[name] = FieldValue(
        value=value,
        source=source,
        confidence=confidence,
        inferred=inferred,
        confirmed=confirmed,
        notes=[note] if note else [],
    )


def _hydrate_from_state(task: TaskFrame, state: ConversationState) -> None:
    if state.design_draft:
        for k, v in state.design_draft.items():
            task.design_spec.setdefault(k, v)
            if k not in task.field_map:
                _set_field(
                    task,
                    k,
                    v,
                    source=FieldSource.STATE,
                    confidence=0.65,
                    inferred=True,
                    note="Hydrated from design_draft.",
                )
    if state.active_source_ref and not task.lens_source:
        task.lens_source = dict(state.active_source_ref)
        _set_field(
            task,
            "lens_source",
            dict(state.active_source_ref),
            source=FieldSource.STATE,
            confidence=0.72,
            inferred=True,
            note="Hydrated from active source ref.",
        )


def _detect_domain(task: TaskFrame, envelope: TurnEnvelope) -> None:
    text = envelope.cleaned_message.lower()
    if any(x in text for x in ["analy", "spot", "mtf", "rms", "simulate"]):
        task.domain_hint = DomainHint.ANALYSIS
        task.task_shape = TaskShape.SINGLE_ACTION
        task.preferred_tool = "dl.analysis"
        return
    if any(x in text for x in ["design", "lens", "fov", "fnum", "f/#"]):
        task.domain_hint = DomainHint.DESIGN
        task.task_shape = TaskShape.MULTI_STEP
        task.preferred_tool = "dl.create_lens"
        return
    if any(x in text for x in ["optimize", "optimization"]):
        task.domain_hint = DomainHint.OPTIMIZATION
        task.task_shape = TaskShape.MULTI_STEP
        task.preferred_tool = "ag.spawn_graph"
        return
    if any(x in text for x in ["export", "zmx", "json", "download", "save"]):
        task.domain_hint = DomainHint.EXPORT
        task.task_shape = TaskShape.SINGLE_ACTION
        task.preferred_tool = "dl.export_lens"
        return
    if any(x in text for x in ["status", "cancel", "stop", "abort", "run"]):
        task.domain_hint = DomainHint.RUN_CONTROL
        task.task_shape = TaskShape.RUN_CONTROL
        task.preferred_tool = "ag.status"
        return


def _apply_extraction(task: TaskFrame, envelope: TurnEnvelope, followup: bool = False) -> None:
    # Parse broadly, commit narrowly. This function is allowed to extract
    # fields that may not all be used immediately.
    design_bundle = extract_design_fields(envelope)
    analysis_bundle = extract_analysis_fields(envelope)

    source = FieldSource.USER_FOLLOWUP if followup else FieldSource.USER_TEXT

    for key, fv in (design_bundle.field_map or {}).items():
        task.field_map[key] = fv
        task.field_map[key].source = source
        task.design_spec.update(design_bundle.design_spec)

    for key, fv in (analysis_bundle.field_map or {}).items():
        task.field_map[key] = fv
        task.field_map[key].source = source

    task.analysis_request.update(analysis_bundle.analysis_request)
    task.run_request.update(analysis_bundle.run_request)
    task.delivery_request.update(analysis_bundle.delivery_request)
    task.notes.extend(list(design_bundle.notes or []))
    task.notes.extend(list(analysis_bundle.notes or []))

    # Minimal source handling
    if envelope.active_source_ref and not task.lens_source:
        task.lens_source = dict(envelope.active_source_ref)
    if envelope.attachments:
        task.source_refs.extend(envelope.attachments)


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
        if not task.lens_source:
            missing.append("lens_source")

    elif task.domain_hint == DomainHint.RUN_CONTROL:
        if not task.run_request.get("run_id"):
            missing.append("run_id")

    task.missing_fields = list(dict.fromkeys(missing))


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

    active_task = get_active_task(state)
    pending = get_pending_interaction(state)
    text = envelope.cleaned_message.strip()

    # 1) Answer pending interaction
    if pending is not None and _looks_like_short_answer(text) and not _looks_like_topic_switch(text):
        task = active_task or _make_blank_task(text)
        _apply_extraction(task, envelope, followup=True)
        _recompute_missing_fields(task)
        task.status = TaskStatus.ACTIVE
        return InterpreterDecision(
            turn_role=TurnRole.ANSWER_PENDING_INTERACTION,
            task=task,
            clear_pending_interaction=True,
            notes=["Merged turn as answer to pending interaction."],
        )

    # 2) Topic switch / explicit supersession
    if pending is not None and _looks_like_topic_switch(text):
        task = _make_blank_task(text)
        _hydrate_from_state(task, state)
        _detect_domain(task, envelope)
        _apply_extraction(task, envelope, followup=False)
        _recompute_missing_fields(task)
        return InterpreterDecision(
            turn_role=TurnRole.NEW_TASK,
            task=task,
            replace_active_task=True,
            clear_pending_interaction=True,
            notes=["Detected topic switch while pending interaction was open."],
        )

    # 3) Update open task
    if active_task is not None and not _looks_like_topic_switch(text):
        task = TaskFrame.from_dict(active_task.to_dict())
        task.user_goal = f"{task.user_goal}\nFollow-up: {text}"
        _apply_extraction(task, envelope, followup=True)
        _recompute_missing_fields(task)
        return InterpreterDecision(
            turn_role=TurnRole.UPDATE_OPEN_TASK,
            task=task,
            notes=["Patched active task from follow-up turn."],
        )

    # 4) New task
    task = _make_blank_task(text)
    _hydrate_from_state(task, state)
    _detect_domain(task, envelope)
    _apply_extraction(task, envelope, followup=False)
    _recompute_missing_fields(task)

    # 5) Direct reply fallback
    if task.domain_hint == DomainHint.UNKNOWN:
        return InterpreterDecision(
            turn_role=TurnRole.DIRECT_REPLY,
            direct_reply="I can help with lens design, analysis, optimization, export, or run control.",
            notes=["No supported task domain detected."],
        )

    return InterpreterDecision(
        turn_role=TurnRole.NEW_TASK,
        task=task,
        replace_active_task=True,
        clear_pending_interaction=True,
        notes=["Created new task from current turn."],
    )