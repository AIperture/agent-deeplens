from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .extraction import attachment_suggests_lens, compute_missing_fields, resolve_task_fields
from .types import (
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


def _apply_state_defaults(task: DeepLensTask, state: DeepLensState) -> DeepLensTask:
    if not task.lens_source and state.active_source_ref:
        task.lens_source = dict(state.active_source_ref)
    if state.design_draft:
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
        slash.decision["task"] = await _resolve_task_fields(_apply_state_defaults(slash.decision["task"], state), state, context)
        return {"decision": slash.decision, "state": slash.state, "immediate_reply": slash.reply}

    state_override = _state_sensitive_route(message, state, user_meta=user_meta)
    if state_override is not None:
        state_override["task"] = await _resolve_task_fields(_apply_state_defaults(state_override["task"], state), state, context)
        return {"decision": state_override, "state": state, "immediate_reply": None}

    fallback = _fallback_route(message, attachments, state)
    fallback["task"] = await _resolve_task_fields(_apply_state_defaults(fallback["task"], state), state, context)
    if fallback["reason"] != "fallback:unsupported":
        return {"decision": fallback, "state": state, "immediate_reply": None}

    if context is not None:
        try:
            decision = await _llm_route(message=message, attachments=attachments, state=state, context=context)
            decision["task"] = await _resolve_task_fields(_apply_state_defaults(decision["task"], state), state, context)
            return {"decision": decision, "state": state, "immediate_reply": None}
        except Exception:
            context.logger().warning("deeplens_v6: llm router failed; using deterministic fallback")
    return {"decision": fallback, "state": state, "immediate_reply": None}
