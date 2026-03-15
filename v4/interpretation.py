from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .extraction import extract_analysis_fields, extract_design_fields
from .input_normalization import normalize_turn
from .types import (
    ApprovalLevel,
    ContextPolicy,
    ControllerKind,
    DeepLensState,
    DomainHint,
    ExecutionRequest,
    FieldSource,
    FieldValue,
    InterpretationResult,
    INTERPRETATION_JSON_SCHEMA,
    IntentType,
    OutputType,
    RiskLevel,
    TaskFrame,
    TaskShape,
    ToolExecutionStyle,
    TurnEnvelope,
    WorkflowFamily,
)

RUN_STATUS_WORDS = {"status", "progress", "running", "run"}
RUN_CANCEL_WORDS = {"cancel", "stop", "abort", "terminate"}
EXPORT_WORDS = {"export", "save", "download", "zmx", "json"}
ANALYSIS_WORDS = {"analyze", "analysis", "mtf", "spot", "rms", "evaluate"}
DESIGN_WORDS = {"design", "lens", "fov", "f/#", "fnum", "focal"}
OPTIMIZE_WORDS = {"optimize", "optimization", "improve", "constraint", "objective"}

DEFAULT_HINT_WORDS = {
    "default",
    "defaults",
    "reasonable",
    "sensible",
    "typical",
    "starter",
    "choose for me",
    "pick for me",
    "best guess",
    "assume",
    "you decide",
    "up to you",
}
EXPLICIT_NO_GUESS_WORDS = {
    "do not assume",
    "don't assume",
    "no default",
    "no defaults",
    "ask me first",
}


@dataclass
class DeterministicSignals:
    matched_rules: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    confidence_boost: float = 0.0
    domain_hint: DomainHint | None = None
    intent: IntentType | None = None
    task_shape: TaskShape | None = None
    workflow_family: WorkflowFamily | None = None
    preferred_tool: str | None = None
    context_policy: ContextPolicy | None = None
    expected_output: OutputType | None = None
    risk_level: RiskLevel | None = None
    approval_required: ApprovalLevel | None = None
    immediate_reply: str | None = None
    missing_fields: list[str] = field(default_factory=list)
    analysis_request: dict[str, Any] = field(default_factory=dict)
    design_spec: dict[str, Any] = field(default_factory=dict)
    run_request: dict[str, Any] = field(default_factory=dict)
    delivery_request: dict[str, Any] = field(default_factory=dict)
    task_payload: dict[str, Any] = field(default_factory=dict)


def _message_lower(envelope: TurnEnvelope) -> str:
    return envelope.cleaned_message.lower().strip()


def _contains_any(text: str, words: set[str]) -> bool:
    return any(word in text for word in words)


def _wants_agent_defaults(envelope: TurnEnvelope) -> bool:
    text = _message_lower(envelope)
    if _contains_any(text, EXPLICIT_NO_GUESS_WORDS):
        return False
    return _contains_any(text, DEFAULT_HINT_WORDS)


def _set_field_value(
    task: TaskFrame,
    name: str,
    value: Any,
    *,
    source: FieldSource,
    confidence: float,
    inferred: bool = False,
    confirmed: bool = False,
    note: str | None = None,
    overwrite: bool = False,
) -> None:
    if value is None:
        return
    existing = task.field_map.get(name)
    if existing and existing.value not in (None, "", []) and not overwrite:
        return
    task.field_map[name] = FieldValue(
        value=value,
        source=source,
        confidence=confidence,
        inferred=inferred,
        confirmed=confirmed,
        notes=[note] if note else [],
    )


def _hydrate_from_state(task: TaskFrame, state: DeepLensState) -> None:
    if state.design_draft:
        for k, v in state.design_draft.items():
            task.design_spec.setdefault(k, v)
            _set_field_value(
                task,
                k,
                v,
                source=FieldSource.STATE,
                confidence=0.72,
                inferred=True,
                note="Hydrated from prior design draft.",
            )

    if state.active_source_ref and not task.lens_source:
        task.lens_source = dict(state.active_source_ref)
        _set_field_value(
            task,
            "lens_source",
            dict(state.active_source_ref),
            source=FieldSource.STATE,
            confidence=0.78,
            inferred=True,
            note="Inherited from active source in state.",
        )


def _build_base_task_frame(envelope: TurnEnvelope) -> TaskFrame:
    return TaskFrame(
        user_goal=envelope.cleaned_message or envelope.raw_message,
        domain_hint=DomainHint.UNKNOWN,
        intent=IntentType.UNKNOWN,
        task_shape=TaskShape.UNSUPPORTED,
        workflow_family=WorkflowFamily.UNKNOWN,
        context_policy=ContextPolicy.MINIMAL,
        expected_output=OutputType.UNKNOWN,
        confidence=0.05,
        risk_level=RiskLevel.LOW,
        approval_required=ApprovalLevel.NONE,
    )


def _rule_explicit_command(envelope: TurnEnvelope, state: DeepLensState) -> DeterministicSignals:
    del state
    sig = DeterministicSignals()
    cmd = (envelope.explicit_command or "").strip().lower()
    if not cmd:
        return sig

    sig.matched_rules.append("explicit_command")
    sig.confidence_boost += 0.45

    if cmd == "/analysis":
        sig.domain_hint = DomainHint.ANALYSIS
        sig.intent = IntentType.ANALYZE
        sig.task_shape = TaskShape.SINGLE_ACTION
        sig.workflow_family = WorkflowFamily.ANALYSIS
        sig.preferred_tool = "dl.analysis"
        sig.context_policy = ContextPolicy.ARTIFACT_CENTRIC
        sig.expected_output = OutputType.SUMMARY
        if envelope.active_source_ref:
            sig.analysis_request["source_ref"] = envelope.active_source_ref
        elif envelope.attachments:
            sig.analysis_request["attachments"] = envelope.attachments
        else:
            sig.missing_fields.append("lens_source")
        return sig

    if cmd == "/design":
        sig.domain_hint = DomainHint.DESIGN
        sig.intent = IntentType.DESIGN
        sig.task_shape = TaskShape.MULTI_STEP
        sig.workflow_family = WorkflowFamily.DESIGN
        sig.preferred_tool = "dl.create_lens"
        sig.context_policy = ContextPolicy.TASK_LOCAL
        sig.expected_output = OutputType.SUMMARY
        sig.missing_fields.extend(["fov", "fnum", "foclen_or_imgh"])
        return sig

    if cmd == "/optimize":
        sig.domain_hint = DomainHint.OPTIMIZATION
        sig.intent = IntentType.OPTIMIZE
        sig.task_shape = TaskShape.MULTI_STEP
        sig.workflow_family = WorkflowFamily.OPTIMIZATION
        sig.preferred_tool = "ag.spawn_graph"
        sig.context_policy = ContextPolicy.TASK_LOCAL
        sig.expected_output = OutputType.SUMMARY
        sig.approval_required = ApprovalLevel.HARD
        if envelope.active_source_ref:
            sig.task_payload["lens_source"] = envelope.active_source_ref
        else:
            sig.missing_fields.append("lens_source")
        return sig

    if cmd.startswith("/mode"):
        sig.domain_hint = DomainHint.CHAT
        sig.intent = IntentType.CONTINUE
        sig.task_shape = TaskShape.DIRECT_ANSWER
        sig.workflow_family = WorkflowFamily.DIRECT
        sig.expected_output = OutputType.TEXT
        if cmd == "/mode full":
            sig.context_policy = ContextPolicy.FULL
            sig.immediate_reply = "Context mode set to FULL."
        elif cmd == "/mode lite":
            sig.context_policy = ContextPolicy.TASK_LOCAL
            sig.immediate_reply = "Context mode set to LITE."
        else:
            sig.immediate_reply = "Supported mode commands are `/mode full` and `/mode lite`."
        return sig

    sig.domain_hint = DomainHint.CHAT
    sig.intent = IntentType.ASK
    sig.task_shape = TaskShape.DIRECT_ANSWER
    sig.workflow_family = WorkflowFamily.DIRECT
    sig.expected_output = OutputType.TEXT
    sig.immediate_reply = (
        "Unknown command. Supported now: `/design`, `/analysis`, `/optimize`, `/mode full`, `/mode lite`."
    )
    return sig


def _rule_run_control(envelope: TurnEnvelope, state: DeepLensState) -> DeterministicSignals:
    sig = DeterministicSignals()
    text = _message_lower(envelope)

    if envelope.ui_hints.get("run_completed_event") and envelope.active_run_id:
        sig.matched_rules.append("run_completed_event")
        sig.confidence_boost += 0.40
        sig.domain_hint = DomainHint.RUN_CONTROL
        sig.intent = IntentType.CONTINUE
        sig.task_shape = TaskShape.RUN_CONTROL
        sig.workflow_family = WorkflowFamily.RUN_CONTROL
        sig.preferred_tool = "ag.status"
        sig.context_policy = ContextPolicy.MINIMAL
        sig.expected_output = OutputType.TEXT
        sig.run_request["run_id"] = envelope.active_run_id
        return sig

    if _contains_any(text, RUN_CANCEL_WORDS):
        sig.matched_rules.append("run_cancel")
        sig.confidence_boost += 0.40
        sig.domain_hint = DomainHint.RUN_CONTROL
        sig.intent = IntentType.CONTROL_RUN
        sig.task_shape = TaskShape.RUN_CONTROL
        sig.workflow_family = WorkflowFamily.RUN_CONTROL
        sig.preferred_tool = "ag.cancel"
        sig.context_policy = ContextPolicy.MINIMAL
        sig.expected_output = OutputType.TEXT
        sig.approval_required = ApprovalLevel.SOFT
        run_id = envelope.active_run_id or state.active_run_id
        if run_id:
            sig.run_request["run_id"] = run_id
        else:
            sig.missing_fields.append("run_id")
        return sig

    if _contains_any(text, RUN_STATUS_WORDS) and (envelope.active_run_id or state.pending_runs or state.active_run_id):
        sig.matched_rules.append("run_status")
        sig.confidence_boost += 0.35
        sig.domain_hint = DomainHint.RUN_CONTROL
        sig.intent = IntentType.CONTROL_RUN
        sig.task_shape = TaskShape.RUN_CONTROL
        sig.workflow_family = WorkflowFamily.RUN_CONTROL
        sig.preferred_tool = "ag.status"
        sig.context_policy = ContextPolicy.MINIMAL
        sig.expected_output = OutputType.TEXT
        run_id = envelope.active_run_id or state.active_run_id
        if not run_id and state.pending_runs:
            run_id = state.pending_runs[-1].get("run_id")
        if run_id:
            sig.run_request["run_id"] = run_id
        else:
            sig.missing_fields.append("run_id")
        return sig

    return sig


def _rule_export(envelope: TurnEnvelope, state: DeepLensState) -> DeterministicSignals:
    sig = DeterministicSignals()
    text = _message_lower(envelope)
    if not (_contains_any(text, EXPORT_WORDS) or envelope.ui_hints.get("mentions_export")):
        return sig

    sig.matched_rules.append("export")
    sig.confidence_boost += 0.30
    sig.domain_hint = DomainHint.EXPORT
    sig.intent = IntentType.EXPORT
    sig.task_shape = TaskShape.SINGLE_ACTION
    sig.workflow_family = WorkflowFamily.EXPORT
    sig.preferred_tool = "dl.export_lens"
    sig.context_policy = ContextPolicy.ARTIFACT_CENTRIC
    sig.expected_output = OutputType.ARTIFACT

    if state.last_artifacts:
        sig.delivery_request["source_artifact"] = state.last_artifacts[-1]
    elif envelope.active_source_ref:
        sig.delivery_request["source_ref"] = envelope.active_source_ref
    else:
        sig.missing_fields.append("lens_source")

    formats: list[str] = []
    if "json" in text:
        formats.append("json")
    if "zmx" in text:
        formats.append("zmx")
    sig.delivery_request["formats"] = formats or ["json", "zmx"]
    return sig


def _rule_analysis(envelope: TurnEnvelope, state: DeepLensState) -> DeterministicSignals:
    del state
    sig = DeterministicSignals()
    text = _message_lower(envelope)
    if not (envelope.ui_hints.get("mentions_analysis") or _contains_any(text, ANALYSIS_WORDS)):
        return sig

    sig.matched_rules.append("analysis")
    sig.confidence_boost += 0.28
    sig.domain_hint = DomainHint.ANALYSIS
    sig.intent = IntentType.ANALYZE
    sig.task_shape = TaskShape.SINGLE_ACTION
    sig.workflow_family = WorkflowFamily.ANALYSIS
    sig.preferred_tool = "dl.analysis"
    sig.context_policy = ContextPolicy.ARTIFACT_CENTRIC
    sig.expected_output = OutputType.SUMMARY

    if "mtf" in text:
        sig.analysis_request["mode"] = "mtf"
    elif "spot" in text:
        sig.analysis_request["mode"] = "spot"
    elif "rms" in text:
        sig.analysis_request["mode"] = "rms"
    else:
        sig.analysis_request["mode"] = "full"

    if envelope.active_source_ref:
        sig.analysis_request["source_ref"] = envelope.active_source_ref
    else:
        lens_attachments = [a for a in envelope.attachments if a.get("kind") == "lens"]
        if lens_attachments:
            sig.analysis_request["attachments"] = lens_attachments
        else:
            sig.missing_fields.append("lens_source")

    return sig


def _rule_optimization(envelope: TurnEnvelope, state: DeepLensState) -> DeterministicSignals:
    del state
    sig = DeterministicSignals()
    text = _message_lower(envelope)
    if not (_contains_any(text, OPTIMIZE_WORDS) or envelope.ui_hints.get("mentions_optimize")):
        return sig

    sig.matched_rules.append("optimization")
    sig.confidence_boost += 0.25
    sig.domain_hint = DomainHint.OPTIMIZATION
    sig.intent = IntentType.OPTIMIZE
    sig.task_shape = TaskShape.MULTI_STEP
    sig.workflow_family = WorkflowFamily.OPTIMIZATION
    sig.preferred_tool = "ag.spawn_graph"
    sig.context_policy = ContextPolicy.TASK_LOCAL
    sig.expected_output = OutputType.SUMMARY
    sig.approval_required = ApprovalLevel.HARD

    if envelope.active_source_ref:
        sig.run_request["lens_source"] = envelope.active_source_ref
    else:
        sig.missing_fields.append("lens_source")

    return sig


def _rule_design(envelope: TurnEnvelope, state: DeepLensState) -> DeterministicSignals:
    del state
    sig = DeterministicSignals()
    text = _message_lower(envelope)
    if not (_contains_any(text, DESIGN_WORDS) or envelope.ui_hints.get("mentions_design")):
        return sig
    if "analysis" in text or "analy" in text:
        return sig

    sig.matched_rules.append("design")
    sig.confidence_boost += 0.22
    sig.domain_hint = DomainHint.DESIGN
    sig.intent = IntentType.DESIGN
    sig.task_shape = TaskShape.MULTI_STEP
    sig.workflow_family = WorkflowFamily.DESIGN
    sig.preferred_tool = "dl.create_lens"
    sig.context_policy = ContextPolicy.TASK_LOCAL
    sig.expected_output = OutputType.SUMMARY
    sig.missing_fields.extend(["fov", "fnum", "foclen_or_imgh"])

    if _wants_agent_defaults(envelope):
        sig.notes.append("User allowed tentative defaults / assumptions for design.")
        sig.confidence_boost += 0.08

    return sig


def _rule_direct_chat_fallback(envelope: TurnEnvelope, state: DeepLensState) -> DeterministicSignals:
    del envelope
    del state
    return DeterministicSignals(
        matched_rules=["direct_chat_fallback"],
        notes=["No stronger task rule matched; using direct chat fallback."],
        confidence_boost=0.10,
        domain_hint=DomainHint.CHAT,
        intent=IntentType.ASK,
        task_shape=TaskShape.DIRECT_ANSWER,
        workflow_family=WorkflowFamily.DIRECT,
        context_policy=ContextPolicy.MINIMAL,
        expected_output=OutputType.TEXT,
    )


def _merge_signals(task: TaskFrame, sig: DeterministicSignals) -> None:
    if sig.domain_hint is not None:
        task.domain_hint = sig.domain_hint
    if sig.intent is not None:
        task.intent = sig.intent
    if sig.task_shape is not None:
        task.task_shape = sig.task_shape
    if sig.workflow_family is not None:
        task.workflow_family = sig.workflow_family
    if sig.preferred_tool is not None:
        task.preferred_tool = sig.preferred_tool
    if sig.context_policy is not None:
        task.context_policy = sig.context_policy
    if sig.expected_output is not None:
        task.expected_output = sig.expected_output
    if sig.risk_level is not None:
        task.risk_level = sig.risk_level
    if sig.approval_required is not None:
        task.approval_required = sig.approval_required

    task.confidence = min(1.0, max(task.confidence, task.confidence + sig.confidence_boost))
    task.notes.extend(sig.notes)

    for field_name in sig.missing_fields:
        if field_name not in task.missing_fields:
            task.missing_fields.append(field_name)

    task.task_payload.update(sig.task_payload)
    task.design_spec.update(sig.design_spec)
    task.analysis_request.update(sig.analysis_request)
    task.run_request.update(sig.run_request)
    task.delivery_request.update(sig.delivery_request)


def _attach_envelope_refs(task: TaskFrame, envelope: TurnEnvelope) -> None:
    task.active_artifact_refs = list(envelope.active_refs)
    if envelope.active_source_ref:
        task.lens_source = dict(envelope.active_source_ref)
        _set_field_value(
            task,
            "lens_source",
            envelope.active_source_ref,
            source=FieldSource.STATE,
            confidence=0.75,
            inferred=True,
            note="Inherited from active_source_ref in state.",
        )
    for att in envelope.attachments:
        if att.get("kind") == "lens":
            task.source_refs.append(att)


def _apply_extraction_bundle(task: TaskFrame, bundle: Any) -> None:
    for key, value in bundle.field_map.items():
        if key not in task.field_map:
            task.field_map[key] = value
    task.design_spec.update(bundle.design_spec)
    if bundle.analysis_request:
        analysis_request = dict(bundle.analysis_request)
        if "analysis_mode" in analysis_request and "mode" not in analysis_request:
            analysis_request["mode"] = analysis_request["analysis_mode"]
        task.analysis_request.update(analysis_request)
    task.run_request.update(bundle.run_request)
    task.delivery_request.update(bundle.delivery_request)
    task.notes.extend(bundle.notes)
    for item in bundle.missing_fields:
        if item not in task.missing_fields:
            task.missing_fields.append(item)


def _needs_llm_interpretation(task: TaskFrame, envelope: TurnEnvelope) -> bool:
    if envelope.explicit_command and envelope.explicit_command.startswith("/mode"):
        return False

    if task.workflow_family == WorkflowFamily.RUN_CONTROL:
        return False

    if task.task_shape == TaskShape.DIRECT_ANSWER:
        return False

    if task.workflow_family == WorkflowFamily.DESIGN:
        wants_defaults = _wants_agent_defaults(envelope)
        has_core = all(k in task.design_spec for k in ("fov", "fnum")) and (
            "foclen" in task.design_spec or "imgh" in task.design_spec
        )
        if has_core and not wants_defaults:
            return False
        if wants_defaults:
            return True

    if task.workflow_family == WorkflowFamily.ANALYSIS:
        has_mode = bool(task.analysis_request.get("mode"))
        has_source = bool(task.lens_source or task.analysis_request.get("source_ref") or task.analysis_request.get("attachment"))
        if has_mode and has_source:
            return False

    return task.confidence < 0.60 or bool(task.missing_fields)


def _build_llm_interpretation_messages(*, envelope: TurnEnvelope, task: TaskFrame) -> list[dict[str, Any]]:
    attachment_summaries = [
        {"name": a.get("name"), "kind": a.get("kind"), "ext": a.get("ext"), "mime_type": a.get("mime_type")}
        for a in envelope.attachments
    ]
    current_task_view = {
        "user_goal": task.user_goal,
        "domain_hint": task.domain_hint.value,
        "intent": task.intent.value,
        "task_shape": task.task_shape.value,
        "workflow_family": task.workflow_family.value,
        "preferred_tool": task.preferred_tool,
        "context_policy": task.context_policy.value,
        "expected_output": task.expected_output.value,
        "confidence": task.confidence,
        "design_spec": task.design_spec,
        "analysis_request": task.analysis_request,
        "run_request": task.run_request,
        "delivery_request": task.delivery_request,
        "missing_fields": task.missing_fields,
        "notes": task.notes,
    }
    system_text = (
        "You are a bounded interpretation helper for a DeepLens workflow agent.\n"
        "Your job is to improve a partially filled task hypothesis conservatively.\n"
        "If the user explicitly allows assumptions/defaults for a lens design request, you may propose tentative "
        "design values in design_spec. These are unconfirmed inferred values, not guaranteed truths.\n"
        "Do not invent lens_source or run_id.\n"
        "Return JSON only.\n"
    )
    user_text = json.dumps(
        {
            "message": envelope.cleaned_message,
            "attachments": attachment_summaries,
            "active_source_ref_present": bool(envelope.active_source_ref),
            "active_run_id": envelope.active_run_id,
            "user_allows_defaults": _wants_agent_defaults(envelope),
            "deterministic_task": current_task_view,
        },
        ensure_ascii=False,
    )
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


async def _maybe_llm_interpret(*, envelope: TurnEnvelope, task: TaskFrame, context: Any) -> dict[str, Any] | None:
    if not _needs_llm_interpretation(task, envelope):
        print("🍎 Skipping LLM interpretation due to high confidence and no missing fields.")
        return None
    logger = context.logger() if context is not None else None
    try:
        print("🍎 Invoking LLM for interpretation with current task frame:", task)
        llm = context.llm(profile="fast")
        resp_text, _usage = await llm.chat(
            messages=_build_llm_interpretation_messages(envelope=envelope, task=task),
            output_format="json_schema",
            json_schema=INTERPRETATION_JSON_SCHEMA,
            schema_name="deeplens_interpretation",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=700,
        )
        print("🍎 Raw LLM interpretation response:", resp_text)
    except Exception as e:
        if logger is not None:
            logger.error(f"LLM interpretation failed: {e}")
        return None
    try:
        out = json.loads(resp_text) if isinstance(resp_text, str) else resp_text
        print("🍎 Parsed LLM interpretation output:", out)
        return out
    except Exception as e:
        if logger is not None:
            logger.error(f"Failed to parse LLM interpretation output: {e}")
        return None


def _safe_enum(enum_cls: Any, raw_value: Any, default: Any) -> Any:
    try:
        return enum_cls(raw_value)
    except Exception:
        return default


def _mark_llm_design_fields(task: TaskFrame, llm_design_spec: dict[str, Any]) -> None:
    for key, value in llm_design_spec.items():
        _set_field_value(
            task,
            key,
            value,
            source=FieldSource.LLM,
            confidence=min(0.82, max(0.55, task.confidence)),
            inferred=True,
            confirmed=False,
            note="Tentatively inferred by LLM interpretation.",
            overwrite=False,
        )


def _merge_llm_interpretation(task: TaskFrame, llm_result: dict[str, Any]) -> None:
    llm_conf = float(llm_result.get("confidence", 0.0) or 0.0)

    if task.confidence < 0.55 or task.workflow_family == WorkflowFamily.UNKNOWN:
        task.domain_hint = _safe_enum(DomainHint, llm_result.get("domain_hint"), task.domain_hint)
        task.intent = _safe_enum(IntentType, llm_result.get("intent"), task.intent)
        task.task_shape = _safe_enum(TaskShape, llm_result.get("task_shape"), task.task_shape)
        task.workflow_family = _safe_enum(WorkflowFamily, llm_result.get("workflow_family"), task.workflow_family)
        task.context_policy = _safe_enum(ContextPolicy, llm_result.get("context_policy"), task.context_policy)
        task.expected_output = _safe_enum(OutputType, llm_result.get("expected_output"), task.expected_output)

    if llm_result.get("preferred_tool") and (not task.preferred_tool or task.confidence < 0.55):
        task.preferred_tool = llm_result["preferred_tool"]

    task.confidence = max(task.confidence, min(0.82, llm_conf))

    llm_design_spec = dict(llm_result.get("design_spec", {}) or {})
    if llm_design_spec:
        for k, v in llm_design_spec.items():
            task.design_spec.setdefault(k, v)
        _mark_llm_design_fields(task, llm_design_spec)

    analysis_request = dict(llm_result.get("analysis_request", {}) or {})
    if "analysis_mode" in analysis_request and "mode" not in analysis_request:
        analysis_request["mode"] = analysis_request["analysis_mode"]
    task.analysis_request.update(analysis_request)

    task.run_request.update(llm_result.get("run_request", {}) or {})
    task.delivery_request.update(llm_result.get("delivery_request", {}) or {})

    for note in llm_result.get("notes", []):
        task.notes.append(f"LLM: {note}")

    reason = llm_result.get("reason")
    if reason:
        task.notes.append(f"LLM reason: {reason}")

    for field_name in llm_result.get("missing_fields", []):
        if field_name not in task.missing_fields:
            task.missing_fields.append(field_name)


def _finalize_missing_fields(task: TaskFrame) -> None:
    task.missing_fields = list(dict.fromkeys([item for item in task.missing_fields if item]))

    has_lens_source = bool(
        task.lens_source
        or task.analysis_request.get("source_ref")
        or task.analysis_request.get("attachment")
        or task.delivery_request.get("source_ref")
        or ("lens_source" in task.field_map and task.field_map["lens_source"].value not in (None, "", []))
    )
    if has_lens_source:
        task.missing_fields = [x for x in task.missing_fields if x != "lens_source"]

    if "fov" in task.design_spec:
        task.missing_fields = [x for x in task.missing_fields if x != "fov"]
    if "fnum" in task.design_spec:
        task.missing_fields = [x for x in task.missing_fields if x != "fnum"]
    if "foclen" in task.design_spec or "imgh" in task.design_spec:
        task.missing_fields = [x for x in task.missing_fields if x != "foclen_or_imgh"]

    if task.run_request.get("run_id"):
        task.missing_fields = [x for x in task.missing_fields if x != "run_id"]

    if task.delivery_request.get("formats") or task.delivery_request.get("export_format"):
        task.missing_fields = [x for x in task.missing_fields if x != "export_format"]


def _derive_execution_request(task: TaskFrame) -> ExecutionRequest | None:
    if task.task_shape == TaskShape.DIRECT_ANSWER:
        return None

    if task.workflow_family == WorkflowFamily.RUN_CONTROL:
        return ExecutionRequest(
            controller_kind=ControllerKind.RUN_CONTROL,
            selected_tool=task.preferred_tool,
            normalized_args={"run_id": task.run_request.get("run_id"), "timeout_s": task.run_request.get("timeout_s", 1)},
            execution_style=ToolExecutionStyle.INLINE,
            user_visible_summary="Handling run-control request.",
        )

    if task.workflow_family == WorkflowFamily.ANALYSIS:
        return ExecutionRequest(
            controller_kind=ControllerKind.WORKFLOW,
            selected_tool=task.preferred_tool,
            normalized_args={
                "analysis_request": task.analysis_request,
                "mode": task.analysis_request.get("mode", "full"),
                "lens_source": task.lens_source or task.analysis_request.get("source_ref") or task.analysis_request.get("attachment"),
                "design_spec": task.design_spec,
                "use_stub": bool(task.run_request.get("use_stub")),
            },
            execution_style=ToolExecutionStyle.INLINE,
            user_visible_summary="Executing analysis request.",
        )

    if task.workflow_family == WorkflowFamily.DESIGN:
        return ExecutionRequest(
            controller_kind=ControllerKind.WORKFLOW,
            selected_tool=task.preferred_tool,
            normalized_args={"design_spec": task.design_spec, "analysis_request": task.analysis_request},
            execution_style=ToolExecutionStyle.INLINE,
            user_visible_summary="Creating lens design.",
        )

    if task.workflow_family == WorkflowFamily.EXPORT:
        formats = task.delivery_request.get("formats") or (
            [task.delivery_request["export_format"]] if task.delivery_request.get("export_format") else ["json", "zmx"]
        )
        return ExecutionRequest(
            controller_kind=ControllerKind.WORKFLOW,
            selected_tool=task.preferred_tool,
            normalized_args={
                "delivery_request": task.delivery_request,
                "formats": formats,
                "lens_source": task.lens_source or task.delivery_request.get("source_ref"),
            },
            execution_style=ToolExecutionStyle.INLINE,
            user_visible_summary="Exporting lens artifacts.",
        )

    if task.workflow_family == WorkflowFamily.OPTIMIZATION:
        return ExecutionRequest(
            controller_kind=ControllerKind.WORKFLOW,
            selected_tool=task.preferred_tool,
            normalized_args={
                "run_request": task.run_request,
                "lens_source": task.lens_source or task.run_request.get("lens_source") or task.task_payload.get("lens_source"),
            },
            execution_style=ToolExecutionStyle.SPAWN,
            execution_constraints={"approval_prompt": "I'm ready to submit the background optimization run. Approve?"},
            user_visible_summary="Preparing optimization workflow.",
        )

    return ExecutionRequest(
        controller_kind=ControllerKind.DIRECT,
        selected_tool=task.preferred_tool,
        normalized_args=dict(task.task_payload),
        execution_style=ToolExecutionStyle.INLINE,
        user_visible_summary="Executing direct action.",
    )


def _build_immediate_reply(task: TaskFrame) -> str | None:
    if task.task_shape != TaskShape.DIRECT_ANSWER:
        return None
    if task.intent == IntentType.ASK:
        return "I can help with analysis, design, export, optimization status, or cancellation."
    if task.intent == IntentType.CONTINUE:
        return "Ready for the next DeepLens action."
    return "I interpreted the request, but no executable action was selected."


async def interpret_turn_from_envelope(
    *,
    envelope: TurnEnvelope,
    state: DeepLensState,
    context: Any | None = None,
) -> InterpretationResult:
    task = _build_base_task_frame(envelope)
    _attach_envelope_refs(task, envelope)
    _hydrate_from_state(task, state)

    ordered_rules = [
        _rule_explicit_command,
        _rule_run_control,
        _rule_export,
        _rule_analysis,
        _rule_optimization,
        _rule_design,
        _rule_direct_chat_fallback,
    ]

    first_sig: DeterministicSignals | None = None
    for rule in ordered_rules:
        sig = rule(envelope, state)
        if sig.matched_rules:
            first_sig = sig
            break

    if first_sig is None:
        first_sig = _rule_direct_chat_fallback(envelope, state)

    _merge_signals(task, first_sig)

    if task.workflow_family == WorkflowFamily.DESIGN:
        _apply_extraction_bundle(task, extract_design_fields(envelope))
    elif task.workflow_family in {WorkflowFamily.ANALYSIS, WorkflowFamily.OPTIMIZATION}:
        _apply_extraction_bundle(task, extract_analysis_fields(envelope))

    if context is not None:
        llm_result = await _maybe_llm_interpret(envelope=envelope, task=task, context=context)
        if llm_result:
            _merge_llm_interpretation(task, llm_result)

    _finalize_missing_fields(task)

    if task.workflow_family == WorkflowFamily.DESIGN and _wants_agent_defaults(envelope) and task.missing_fields:
        task.notes.append("User allowed defaults, but the task still remains under-specified after LLM interpretation.")

    immediate_reply = first_sig.immediate_reply
    execution = None

    if immediate_reply is None:
        if task.task_shape == TaskShape.DIRECT_ANSWER:
            immediate_reply = _build_immediate_reply(task)
        else:
            execution = _derive_execution_request(task)

    return InterpretationResult(envelope=envelope, task_frame=task, execution=execution, immediate_reply=immediate_reply)


async def interpret_turn(
    *,
    message: str,
    attachments: list[dict[str, Any]] | None,
    state: DeepLensState,
    user_meta: dict[str, Any] | None = None,
    context: Any | None = None,
) -> InterpretationResult:
    envelope = normalize_turn(message=message, attachments=attachments, state=state, user_meta=user_meta)
    return await interpret_turn_from_envelope(envelope=envelope, state=state, context=context)
