from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from .extraction import attachment_suggests_lens, compute_missing_fields, resolve_task_fields
from .types import (
    ACTIVE_TASK_STATUSES,
    ContextMode,
    DEEPLENS_SKILL_ID,
    DeepLensState,
    DeepLensTask,
    DomainHint,
    RouteDecision,
    RouteResult,
    ROUTER_JSON_SCHEMA,
    TaskShape,
)


@dataclass
class SlashCommandResult:
    state: DeepLensState
    decision: RouteDecision
    reply: str | None = None


_SHORT_ANSWER_TOKENS = {
    "yes",
    "y",
    "no",
    "n",
    "both",
    "json",
    "zmx",
    "resume",
    "continue",
    "retry",
    "same",
    "use active lens",
    "use the active lens",
    "active lens",
}
_CONTINUATION_PHRASES = ("continue", "resume", "try again", "retry", "go ahead", "use active lens", "same lens")
_NEW_TASK_MARKERS = ("start over", "new task", "instead", "separate task", "different task")
_NEW_TASK_VERBS = ("analyze", "optimize", "explain", "design", "create", "build", "export")


def _default_context_mode(state: DeepLensState) -> ContextMode:
    return ContextMode.FULL if state.context_mode == "full" else ContextMode.LITE


def _message_mentions_analysis(msg: str) -> bool:
    return any(k in msg for k in ("analy", "analysis", "spot", "mtf", "rms", "evaluate", "simulat"))


def _message_mentions_design(msg: str) -> bool:
    return any(k in msg for k in ("design", "create lens", "create a lens", "build lens", "make a lens", "new lens"))


def _message_mentions_optimization(msg: str) -> bool:
    return any(k in msg for k in ("optimize", "optimization", "improve lens", "constraint", "objective", "sharper"))


def _message_mentions_export(msg: str) -> bool:
    return any(k in msg for k in ("export", "save", "download", "json", "zmx"))


def _make_task(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    task_shape: TaskShape,
    domain_hint: DomainHint,
    parsed_args: dict[str, Any] | None = None,
    preferred_tool: str | None = None,
) -> DeepLensTask:
    return DeepLensTask(
        user_goal=message,
        task_shape=task_shape,
        domain_hint=domain_hint,
        attachments=attachments,
        parsed_args=parsed_args or {},
        preferred_tool=preferred_tool,
        source_refs=[
            {
                "name": item.get("name") or item.get("filename") or item.get("uri"),
                "artifact_id": item.get("artifact_id"),
                "uri": item.get("uri"),
            }
            for item in attachments
            if isinstance(item, dict)
        ],
        delivery_request={"mode": "summary_plus_files"},
    )


def _apply_state_defaults(task: DeepLensTask, state: DeepLensState, *, continuation: bool = False) -> DeepLensTask:
    if not task.lens_source and state.active_source_ref:
        task.lens_source = dict(state.active_source_ref)
    if continuation and state.design_draft:
        merged = dict(state.design_draft)
        merged.update(task.design_spec)
        task.design_spec = merged
    if state.active_run_id and not task.run_request.get("run_id"):
        task.run_request["run_id"] = state.active_run_id
    task.missing_fields = compute_missing_fields(task)
    return task


async def _resolve_task_fields(task: DeepLensTask, state: DeepLensState, context: Any | None) -> DeepLensTask:
    await resolve_task_fields(
        task=task,
        state=state,
        message=task.user_goal,
        attachments=task.attachments,
        context=context,
    )
    task.missing_fields = compute_missing_fields(task)
    return task


def _active_task_from_state(state: DeepLensState) -> DeepLensTask | None:
    return DeepLensTask.from_dict(state.active_task if isinstance(state.active_task, dict) else None)


def _is_task_continuation_eligible(task: DeepLensTask | None) -> bool:
    return task is not None and task.task_status in ACTIVE_TASK_STATUSES


def _normalize_message(message: str) -> str:
    return " ".join((message or "").strip().lower().split())


def _strong_domain_signal(msg: str) -> DomainHint | None:
    if _message_mentions_optimization(msg):
        return DomainHint.OPTIMIZATION
    if _message_mentions_design(msg):
        return DomainHint.DESIGN
    if _message_mentions_analysis(msg):
        return DomainHint.ANALYSIS
    if _message_mentions_export(msg):
        return DomainHint.EXPORT
    return None


def _pending_field_names(state: DeepLensState, active_task: DeepLensTask | None) -> list[str]:
    pending: list[str] = []
    for field_name in list(state.runtime_missing_fields or []):
        if field_name not in pending:
            pending.append(field_name)
    for field_name in list((active_task.missing_fields if active_task is not None else []) or []):
        if field_name not in pending:
            pending.append(field_name)
    return pending


def _looks_like_short_answer(msg: str) -> bool:
    if not msg:
        return False
    if msg in _SHORT_ANSWER_TOKENS:
        return True
    tokens = msg.split()
    return len(tokens) <= 3 and all(len(token) <= 12 for token in tokens)


def _looks_like_continuation(msg: str) -> bool:
    return any(phrase in msg for phrase in _CONTINUATION_PHRASES)


def _looks_like_explicit_new_task(msg: str) -> bool:
    return any(marker in msg for marker in _NEW_TASK_MARKERS)


def _looks_like_standalone_request(msg: str) -> bool:
    if any(msg.startswith(f"{verb} ") for verb in _NEW_TASK_VERBS):
        return True
    return len(msg.split()) >= 6 and any(f" {verb} " in f" {msg} " for verb in _NEW_TASK_VERBS)


def _turn_answers_pending_fields(
    *,
    pending_fields: list[str],
    active_task: DeepLensTask,
    merged_task: DeepLensTask,
    attachments: list[dict[str, Any]],
) -> bool:
    before = set(pending_fields)
    after = set(merged_task.missing_fields)
    if before and before != after and before - after:
        return True
    if "lens_source" in before and attachment_suggests_lens(attachments):
        return True
    if "run_id" in before and merged_task.run_request.get("run_id") and not active_task.run_request.get("run_id"):
        return True
    if "export_formats" in before and merged_task.delivery_request.get("formats"):
        return True
    return False


def _coherent_continuation(
    *,
    message: str,
    active_task: DeepLensTask,
    merged_task: DeepLensTask,
    attachments: list[dict[str, Any]],
    state: DeepLensState,
) -> bool:
    msg = _normalize_message(message)
    if _looks_like_explicit_new_task(msg):
        return False
    strong_domain = _strong_domain_signal(msg)
    if strong_domain is not None and active_task.domain_hint != DomainHint.UNKNOWN and strong_domain != active_task.domain_hint:
        if _looks_like_standalone_request(msg):
            return False
    pending_fields = _pending_field_names(state, active_task)
    if _turn_answers_pending_fields(
        pending_fields=pending_fields,
        active_task=active_task,
        merged_task=merged_task,
        attachments=attachments,
    ):
        return True
    if pending_fields and (_looks_like_short_answer(msg) or _looks_like_continuation(msg)):
        return True
    if not msg and attachments:
        return True
    if strong_domain == active_task.domain_hint and not _looks_like_standalone_request(msg):
        return True
    return _looks_like_continuation(msg)


def _merge_continuation_turn(
    active_task: DeepLensTask,
    *,
    message: str,
    attachments: list[dict[str, Any]],
) -> DeepLensTask:
    merged = DeepLensTask.from_dict(asdict(active_task)) or active_task
    if message:
        merged.notes.append(f"continuation_turn:{message}")
    if attachments:
        merged.attachments.extend(attachments)
        merged.source_refs.extend(
            {
                "name": item.get("name") or item.get("filename") or item.get("uri"),
                "artifact_id": item.get("artifact_id"),
                "uri": item.get("uri"),
            }
            for item in attachments
            if isinstance(item, dict)
        )
    return merged


async def _llm_turn_role(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    state: DeepLensState,
    active_task: DeepLensTask,
    context: Any,
) -> tuple[str, str] | None:
    llm = context.llm("fast")
    payload = {
        "message": message,
        "attachments_count": len(attachments),
        "active_task": {
            "domain_hint": active_task.domain_hint.value,
            "task_shape": active_task.task_shape.value,
            "task_status": active_task.task_status,
            "missing_fields": list(active_task.missing_fields),
        },
        "pending_action": state.pending_action,
        "runtime_missing_fields": list(state.runtime_missing_fields),
        "active_recovery": state.active_recovery,
    }
    schema = {
        "type": "object",
        "properties": {
            "turn_role": {"type": "string", "enum": ["answer_pending_interaction", "continue_active_task", "new_task"]},
            "reason": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["turn_role", "reason", "confidence"],
        "additionalProperties": False,
    }
    try:
        response, _usage = await llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classify whether the latest user turn continues the current DeepLens task, "
                        "answers a pending interaction, or starts a new task. Prefer continuation only when the turn "
                        "plausibly answers missing fields or advances the same objective."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=schema,
            schema_name="DeepLensV6TurnRole",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=140,
            reasoning_effort="low",
        )
    except Exception:
        context.logger().warning("deeplens_v6: turn-role llm arbitration failed", exc_info=True)
        return None
    obj = json.loads(response) if isinstance(response, str) else response
    if float(obj.get("confidence") or 0.0) < 0.6:
        return None
    return str(obj.get("turn_role") or "new_task"), str(obj.get("reason") or "llm:turn_role")


async def _detect_turn_role(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    state: DeepLensState,
    context: Any | None,
) -> tuple[str, DeepLensTask | None, str] | None:
    active_task = _active_task_from_state(state)
    if not _is_task_continuation_eligible(active_task):
        return None

    msg = _normalize_message(message)
    pending_context = bool(state.pending_action or state.runtime_missing_fields or state.active_recovery)
    strong_domain = _strong_domain_signal(msg)
    standalone = _looks_like_standalone_request(msg)
    if _looks_like_explicit_new_task(msg):
        return ("new_task", None, "state:new_task_marker")
    if strong_domain is not None and strong_domain != active_task.domain_hint and standalone:
        return ("new_task", None, "state:new_task_domain_shift")

    merged_task = _merge_continuation_turn(active_task, message=message, attachments=attachments)
    merged_task = _apply_state_defaults(merged_task, state, continuation=True)
    await resolve_task_fields(task=merged_task, state=state, message=message, attachments=attachments, context=None)
    merged_task.missing_fields = compute_missing_fields(merged_task)

    if _coherent_continuation(
        message=message,
        active_task=active_task,
        merged_task=merged_task,
        attachments=attachments,
        state=state,
    ):
        role = "answer_pending_interaction" if pending_context else "continue_active_task"
        return (role, merged_task, f"state:{role}")

    if standalone and msg:
        return ("new_task", None, "state:new_task_standalone")

    if context is not None:
        llm_role = await _llm_turn_role(
            message=message,
            attachments=attachments,
            state=state,
            active_task=active_task,
            context=context,
        )
        if llm_role is not None:
            role, reason = llm_role
            if role == "new_task":
                return ("new_task", None, reason)
            return (role, merged_task, reason)

    narrow_prompt = (
        "Are you continuing the current lens task or starting a new request?"
        if pending_context
        else "Please confirm whether this is continuing the current task."
    )
    return ("answer_pending_interaction", None, narrow_prompt)


def apply_slash_command(
    message: str,
    attachments: list[dict[str, Any]],
    state: DeepLensState,
) -> SlashCommandResult | None:
    msg = (message or "").strip()
    msg_l = msg.lower()
    if not msg_l.startswith("/"):
        return None
    ctx_mode = _default_context_mode(state)

    if msg_l.startswith("/analysis"):
        decision: RouteDecision = {
            "task_shape": TaskShape.MULTI_STEP,
            "domain_hint": DomainHint.ANALYSIS,
            "preferred_tool": "dl.analysis",
            "context_mode": ctx_mode,
            "reason": "slash:/analysis",
            "confidence": 1.0,
            "task": _make_task(message=message, attachments=attachments, task_shape=TaskShape.MULTI_STEP, domain_hint=DomainHint.ANALYSIS, preferred_tool="dl.analysis"),
        }
        return SlashCommandResult(state=state, decision=decision)

    if msg_l.startswith("/design"):
        decision = {
            "task_shape": TaskShape.MULTI_STEP,
            "domain_hint": DomainHint.DESIGN,
            "preferred_tool": "dl.create_lens",
            "context_mode": ctx_mode,
            "reason": "slash:/design",
            "confidence": 1.0,
            "task": _make_task(message=message, attachments=attachments, task_shape=TaskShape.MULTI_STEP, domain_hint=DomainHint.DESIGN, preferred_tool="dl.create_lens"),
        }
        return SlashCommandResult(state=state, decision=decision)

    if msg_l.startswith("/optimize"):
        decision = {
            "task_shape": TaskShape.MULTI_STEP,
            "domain_hint": DomainHint.OPTIMIZATION,
            "preferred_tool": "ag.spawn_graph",
            "context_mode": ctx_mode,
            "reason": "slash:/optimize",
            "confidence": 1.0,
            "task": _make_task(message=message, attachments=attachments, task_shape=TaskShape.MULTI_STEP, domain_hint=DomainHint.OPTIMIZATION, preferred_tool="ag.spawn_graph"),
        }
        return SlashCommandResult(state=state, decision=decision)

    if msg_l.startswith("/mode"):
        if " full" in f" {msg_l}":
            state.context_mode = "full"
            return SlashCommandResult(
                state=state,
                decision={
                    "task_shape": TaskShape.DIRECT_ANSWER,
                    "domain_hint": DomainHint.CHAT,
                    "preferred_tool": None,
                    "context_mode": ContextMode.FULL,
                    "reason": "slash:/mode full",
                    "confidence": 1.0,
                    "task": _make_task(message=message, attachments=attachments, task_shape=TaskShape.DIRECT_ANSWER, domain_hint=DomainHint.CHAT),
                },
                reply="Context mode set to FULL.",
            )
        if " lite" in f" {msg_l}":
            state.context_mode = "lite"
            return SlashCommandResult(
                state=state,
                decision={
                    "task_shape": TaskShape.DIRECT_ANSWER,
                    "domain_hint": DomainHint.CHAT,
                    "preferred_tool": None,
                    "context_mode": ContextMode.LITE,
                    "reason": "slash:/mode lite",
                    "confidence": 1.0,
                    "task": _make_task(message=message, attachments=attachments, task_shape=TaskShape.DIRECT_ANSWER, domain_hint=DomainHint.CHAT),
                },
                reply="Context mode set to LITE.",
            )
    return None


def _detect_run_completed_pickup(message: str, user_meta: dict[str, Any] | None = None) -> str | None:
    if user_meta and user_meta.get("type") == "run_completed" and user_meta.get("run_id"):
        return str(user_meta["run_id"])
    match = re.search(r"results?\s+for\s+(?:completed?\s+)?run\s+([A-Za-z0-9_\-]{8,})", (message or "").lower())
    return match.group(1) if match else None


def _state_sensitive_route(message: str, state: DeepLensState, user_meta: dict[str, Any] | None = None) -> RouteDecision | None:
    msg = (message or "").strip().lower()
    pickup_run_id = _detect_run_completed_pickup(message, user_meta)
    if pickup_run_id:
        return {
            "task_shape": TaskShape.RUN_CONTROL,
            "domain_hint": DomainHint.RUN_CONTROL,
            "preferred_tool": "ag.status",
            "context_mode": _default_context_mode(state),
            "reason": "state:run_completed_pickup",
            "confidence": 1.0,
            "task": DeepLensTask(
                user_goal=message,
                task_shape=TaskShape.RUN_CONTROL,
                domain_hint=DomainHint.RUN_CONTROL,
                parsed_args={"run_id": pickup_run_id},
                preferred_tool="ag.status",
                run_request={"run_id": pickup_run_id},
            ),
        }
    if state.active_run_id and any(k in msg for k in ("status", "still running", "cancel", "stop", "abort")):
        preferred_tool = "ag.cancel" if any(k in msg for k in ("cancel", "stop", "abort")) else "ag.status"
        return {
            "task_shape": TaskShape.RUN_CONTROL,
            "domain_hint": DomainHint.RUN_CONTROL,
            "preferred_tool": preferred_tool,
            "context_mode": _default_context_mode(state),
            "reason": "state:active_run_control",
            "confidence": 0.95,
            "task": DeepLensTask(
                user_goal=message,
                task_shape=TaskShape.RUN_CONTROL,
                domain_hint=DomainHint.RUN_CONTROL,
                parsed_args={"run_id": state.active_run_id},
                preferred_tool=preferred_tool,
                run_request={"run_id": state.active_run_id},
            ),
        }
    return None


def _fallback_route(message: str, attachments: list[dict[str, Any]], state: DeepLensState) -> RouteDecision:
    msg = (message or "").lower()
    has_design = _message_mentions_design(msg)
    has_analysis = _message_mentions_analysis(msg)
    has_optimization = _message_mentions_optimization(msg)
    has_export = _message_mentions_export(msg)

    if has_design:
        return {
            "task_shape": TaskShape.MULTI_STEP,
            "domain_hint": DomainHint.DESIGN,
            "preferred_tool": "dl.create_lens",
            "context_mode": _default_context_mode(state),
            "reason": "fallback:design_or_compound",
            "confidence": 0.8,
            "task": _make_task(message=message, attachments=attachments, task_shape=TaskShape.MULTI_STEP, domain_hint=DomainHint.DESIGN, preferred_tool="dl.create_lens"),
        }
    if has_analysis or (attachment_suggests_lens(attachments) and not has_optimization):
        shape = TaskShape.MULTI_STEP if has_export or has_optimization else TaskShape.SINGLE_ACTION
        return {
            "task_shape": shape,
            "domain_hint": DomainHint.ANALYSIS,
            "preferred_tool": "dl.analysis",
            "context_mode": _default_context_mode(state),
            "reason": "fallback:analysis_or_compound",
            "confidence": 0.78,
            "task": _make_task(message=message, attachments=attachments, task_shape=shape, domain_hint=DomainHint.ANALYSIS, preferred_tool="dl.analysis"),
        }
    if has_optimization:
        return {
            "task_shape": TaskShape.MULTI_STEP,
            "domain_hint": DomainHint.OPTIMIZATION,
            "preferred_tool": "ag.spawn_graph",
            "context_mode": _default_context_mode(state),
            "reason": "fallback:optimization",
            "confidence": 0.78,
            "task": _make_task(message=message, attachments=attachments, task_shape=TaskShape.MULTI_STEP, domain_hint=DomainHint.OPTIMIZATION, preferred_tool="ag.spawn_graph"),
        }
    if has_export:
        return {
            "task_shape": TaskShape.MULTI_STEP,
            "domain_hint": DomainHint.EXPORT,
            "preferred_tool": "dl.export_lens",
            "context_mode": _default_context_mode(state),
            "reason": "fallback:export",
            "confidence": 0.72,
            "task": _make_task(message=message, attachments=attachments, task_shape=TaskShape.MULTI_STEP, domain_hint=DomainHint.EXPORT, preferred_tool="dl.export_lens"),
        }
    if any(k in msg for k in ("what is", "how does", "explain", "why", "interpret")):
        return {
            "task_shape": TaskShape.DIRECT_ANSWER,
            "domain_hint": DomainHint.CHAT,
            "preferred_tool": None,
            "context_mode": _default_context_mode(state),
            "reason": "fallback:direct_answer",
            "confidence": 0.6,
            "task": _make_task(message=message, attachments=attachments, task_shape=TaskShape.DIRECT_ANSWER, domain_hint=DomainHint.CHAT),
        }
    return {
        "task_shape": TaskShape.UNSUPPORTED,
        "domain_hint": DomainHint.UNKNOWN,
        "preferred_tool": None,
        "context_mode": _default_context_mode(state),
        "reason": "fallback:unsupported",
        "confidence": 0.5,
        "task": _make_task(message=message, attachments=attachments, task_shape=TaskShape.UNSUPPORTED, domain_hint=DomainHint.UNKNOWN),
    }


async def _llm_route(*, message: str, attachments: list[dict[str, Any]], state: DeepLensState, context: Any) -> RouteDecision:
    llm = context.llm()
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        DEEPLENS_SKILL_ID,
        "deeplens.system",
        "deeplens.router",
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )
    user_prompt = (
        "Classify the request into one task shape and one dominant domain.\n"
        "The planner will handle multi-tool composition later.\n"
        f"Message:\n{message}\n\n"
        f"State:\n- active_run_id={state.active_run_id or 'none'}\n- active_source_ref={state.active_source_ref or 'none'}\n"
        f"- attachments_count={len(attachments)}\n"
    )
    resp, _usage = await llm.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        output_format="json_schema",
        json_schema=ROUTER_JSON_SCHEMA,
        schema_name="DeepLensV6Route",
        strict_schema=True,
        validate_json=True,
        max_output_tokens=220,
        reasoning_effort="low",
    )
    print(f"🍎 LLM Router response: {resp}")
    obj = json.loads(resp) if isinstance(resp, str) else resp
    task_shape = TaskShape(str(obj.get("task_shape", "unsupported")).strip().lower())
    domain_hint = DomainHint(str(obj.get("domain_hint", "unknown")).strip().lower())
    preferred_tool = obj.get("preferred_tool")
    return {
        "task_shape": task_shape,
        "domain_hint": domain_hint,
        "preferred_tool": preferred_tool,
        "context_mode": _default_context_mode(state),
        "reason": str(obj.get("reason") or "llm_route"),
        "confidence": float(obj.get("confidence") or 0.5),
        "task": _make_task(message=message, attachments=attachments, task_shape=task_shape, domain_hint=domain_hint, preferred_tool=preferred_tool),
    }


async def route(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    state: DeepLensState,
    context: Any | None = None,
    user_meta: dict[str, Any] | None = None,
) -> RouteResult:
    slash = apply_slash_command(message, attachments, state)
    if slash is not None:
        slash.decision["task"] = await _resolve_task_fields(_apply_state_defaults(slash.decision["task"], state, continuation=False), state, context)
        return {"decision": slash.decision, "state": slash.state, "immediate_reply": slash.reply, "turn_role": "slash_command"}

    state_override = _state_sensitive_route(message, state, user_meta=user_meta)
    if state_override is not None:
        state_override["task"] = await _resolve_task_fields(_apply_state_defaults(state_override["task"], state, continuation=False), state, context)
        return {"decision": state_override, "state": state, "immediate_reply": None, "turn_role": "run_control"}

    turn_role = await _detect_turn_role(message=message, attachments=attachments, state=state, context=context)
    if turn_role is not None:
        role, continuation_task, reason = turn_role
        if role != "new_task" and continuation_task is None:
            clarification = _make_task(
                message=message or reason,
                attachments=attachments,
                task_shape=TaskShape.DIRECT_ANSWER,
                domain_hint=DomainHint.CHAT,
            )
            decision: RouteDecision = {
                "task_shape": clarification.task_shape,
                "domain_hint": clarification.domain_hint,
                "preferred_tool": clarification.preferred_tool,
                "context_mode": _default_context_mode(state),
                "reason": "state:continuation_clarification",
                "confidence": 0.7,
                "task": clarification,
            }
            return {"decision": decision, "state": state, "immediate_reply": reason, "turn_role": role}
        if role != "new_task" and continuation_task is not None:
            continuation_task = await _resolve_task_fields(
                _apply_state_defaults(continuation_task, state, continuation=True),
                state,
                context,
            )
            continuation_task.task_status = "active" if role == "continue_active_task" else "waiting"
            state.active_agenda = None
            decision: RouteDecision = {
                "task_shape": continuation_task.task_shape,
                "domain_hint": continuation_task.domain_hint,
                "preferred_tool": continuation_task.preferred_tool,
                "context_mode": _default_context_mode(state),
                "reason": reason,
                "confidence": 0.93 if role == "answer_pending_interaction" else 0.9,
                "task": continuation_task,
            }
            return {"decision": decision, "state": state, "immediate_reply": None, "turn_role": role}

    fallback = _fallback_route(message, attachments, state)
    fallback["task"] = await _resolve_task_fields(_apply_state_defaults(fallback["task"], state, continuation=False), state, context)
    if fallback["reason"] != "fallback:unsupported":
        return {"decision": fallback, "state": state, "immediate_reply": None, "turn_role": "new_task"}

    if context is not None:
        try:
            decision = await _llm_route(message=message, attachments=attachments, state=state, context=context)
            decision["task"] = await _resolve_task_fields(_apply_state_defaults(decision["task"], state, continuation=False), state, context)
            return {"decision": decision, "state": state, "immediate_reply": None, "turn_role": "new_task"}
        except Exception:
            context.logger().warning("deeplens_v6: llm router failed; using deterministic fallback")
    return {"decision": fallback, "state": state, "immediate_reply": None, "turn_role": "new_task"}
