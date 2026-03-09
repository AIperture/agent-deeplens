from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

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


def _make_task(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    task_shape: TaskShape,
    domain_hint: DomainHint,
    parsed_args: dict[str, Any] | None = None,
    missing_fields: list[str] | None = None,
    preferred_tool: str | None = None,
) -> DeepLensTask:
    return DeepLensTask(
        user_goal=message,
        task_shape=task_shape,
        domain_hint=domain_hint,
        attachments=attachments,
        parsed_args=parsed_args or {},
        missing_fields=missing_fields or [],
        preferred_tool=preferred_tool,
    )


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

    if msg_l.startswith("/simulate"):
        decision: RouteDecision = {
            "task_shape": TaskShape.SINGLE_ACTION,
            "domain_hint": DomainHint.SIMULATION,
            "preferred_tool": "dl.simulate_standard",
            "context_mode": ctx_mode,
            "reason": "slash:/simulate",
            "confidence": 1.0,
            "task": _make_task(
                message=message,
                attachments=attachments,
                task_shape=TaskShape.SINGLE_ACTION,
                domain_hint=DomainHint.SIMULATION,
                preferred_tool="dl.simulate_standard",
            ),
        }
        return SlashCommandResult(state=state, decision=decision)

    if msg_l.startswith("/optimize"):
        decision = {
            "task_shape": TaskShape.MULTI_STEP,
            "domain_hint": DomainHint.OPTIMIZATION,
            "preferred_tool": "dl.submit_optimization",
            "context_mode": ctx_mode,
            "reason": "slash:/optimize",
            "confidence": 1.0,
            "task": _make_task(
                message=message,
                attachments=attachments,
                task_shape=TaskShape.MULTI_STEP,
                domain_hint=DomainHint.OPTIMIZATION,
                preferred_tool="dl.submit_optimization",
            ),
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
                    "task": _make_task(
                        message=message,
                        attachments=attachments,
                        task_shape=TaskShape.DIRECT_ANSWER,
                        domain_hint=DomainHint.CHAT,
                    ),
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
                    "task": _make_task(
                        message=message,
                        attachments=attachments,
                        task_shape=TaskShape.DIRECT_ANSWER,
                        domain_hint=DomainHint.CHAT,
                    ),
                },
                reply="Context mode set to LITE.",
            )

    return SlashCommandResult(
        state=state,
        decision={
            "task_shape": TaskShape.UNSUPPORTED,
            "domain_hint": DomainHint.UNKNOWN,
            "preferred_tool": None,
            "context_mode": ctx_mode,
            "reason": "slash:unknown",
            "confidence": 1.0,
            "task": _make_task(
                message=message,
                attachments=attachments,
                task_shape=TaskShape.UNSUPPORTED,
                domain_hint=DomainHint.UNKNOWN,
            ),
        },
        reply="Unknown command. Supported now: `/simulate`, `/optimize`, `/mode full`, `/mode lite`.",
    )


def _state_sensitive_route(message: str, state: DeepLensState) -> RouteDecision | None:
    msg = (message or "").strip().lower()
    if state.active_run_id and any(k in msg for k in ("status", "still running", "cancel", "stop run")):
        preferred_tool = "ag.cancel_run" if any(k in msg for k in ("cancel", "stop run")) else "dl.check_run_status"
        return {
            "task_shape": TaskShape.RUN_CONTROL,
            "domain_hint": DomainHint.OPTIMIZATION,
            "preferred_tool": preferred_tool,
            "context_mode": _default_context_mode(state),
            "reason": "state:active_run_control",
            "confidence": 0.95,
            "task": DeepLensTask(
                user_goal=message,
                task_shape=TaskShape.RUN_CONTROL,
                domain_hint=DomainHint.OPTIMIZATION,
                parsed_args={"run_id": state.active_run_id},
                preferred_tool=preferred_tool,
            ),
        }
    return None


def _fallback_route(message: str, attachments: list[dict[str, Any]], state: DeepLensState) -> RouteDecision:
    msg = (message or "").lower()
    if any(k in msg for k in ("cancel", "status", "still running", "stop run")) and state.active_run_id:
        preferred_tool = "ag.cancel_run" if any(k in msg for k in ("cancel", "stop run")) else "dl.check_run_status"
        return {
            "task_shape": TaskShape.RUN_CONTROL,
            "domain_hint": DomainHint.OPTIMIZATION,
            "preferred_tool": preferred_tool,
            "context_mode": _default_context_mode(state),
            "reason": "fallback:run_control",
            "confidence": 0.8,
            "task": _make_task(
                message=message,
                attachments=attachments,
                task_shape=TaskShape.RUN_CONTROL,
                domain_hint=DomainHint.OPTIMIZATION,
                preferred_tool=preferred_tool,
                parsed_args={"run_id": state.active_run_id},
            ),
        }
    if any(k in msg for k in ("simulate", "mtf", "psf", "spot", "ray trace")):
        return {
            "task_shape": TaskShape.SINGLE_ACTION,
            "domain_hint": DomainHint.SIMULATION,
            "preferred_tool": "dl.simulate_standard",
            "context_mode": _default_context_mode(state),
            "reason": "fallback:simulation",
            "confidence": 0.7,
            "task": _make_task(
                message=message,
                attachments=attachments,
                task_shape=TaskShape.SINGLE_ACTION,
                domain_hint=DomainHint.SIMULATION,
                preferred_tool="dl.simulate_standard",
            ),
        }
    if any(k in msg for k in ("optimize", "objective", "constraint", "improve")):
        return {
            "task_shape": TaskShape.MULTI_STEP,
            "domain_hint": DomainHint.OPTIMIZATION,
            "preferred_tool": "dl.submit_optimization",
            "context_mode": _default_context_mode(state),
            "reason": "fallback:optimization",
            "confidence": 0.7,
            "task": _make_task(
                message=message,
                attachments=attachments,
                task_shape=TaskShape.MULTI_STEP,
                domain_hint=DomainHint.OPTIMIZATION,
                preferred_tool="dl.submit_optimization",
            ),
        }
    if any(k in msg for k in ("what is", "how does", "explain", "why")):
        return {
            "task_shape": TaskShape.DIRECT_ANSWER,
            "domain_hint": DomainHint.CHAT,
            "preferred_tool": None,
            "context_mode": _default_context_mode(state),
            "reason": "fallback:direct_answer",
            "confidence": 0.6,
            "task": _make_task(
                message=message,
                attachments=attachments,
                task_shape=TaskShape.DIRECT_ANSWER,
                domain_hint=DomainHint.CHAT,
            ),
        }
    return {
        "task_shape": TaskShape.UNSUPPORTED,
        "domain_hint": DomainHint.UNKNOWN,
        "preferred_tool": None,
        "context_mode": _default_context_mode(state),
        "reason": "fallback:unsupported",
        "confidence": 0.5,
        "task": _make_task(
            message=message,
            attachments=attachments,
            task_shape=TaskShape.UNSUPPORTED,
            domain_hint=DomainHint.UNKNOWN,
        ),
    }


async def _llm_route(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    state: DeepLensState,
    context: Any,
) -> RouteDecision:
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
        "Classify the user request into one task shape.\n\n"
        "Valid task shapes:\n"
        "- direct_answer\n"
        "- single_action\n"
        "- multi_step\n"
        "- run_control\n"
        "- unsupported\n\n"
        "Valid domain hints:\n"
        "- chat\n"
        "- simulation\n"
        "- optimization\n"
        "- debug\n"
        "- unknown\n\n"
        "Prefer:\n"
        "- direct_answer for explanatory questions\n"
        "- single_action for one obvious action like standard simulation\n"
        "- multi_step for optimization or iterative requests\n"
        "- run_control for status/cancel on an active run\n"
        "- unsupported if outside the supported surface\n\n"
        f"Message:\n{message}\n\n"
        f"State:\n"
        f"- context_mode={state.context_mode}\n"
        f"- active_run_id={state.active_run_id or 'none'}\n"
        f"- attachments_count={len(attachments)}\n\n"
        "Return strict JSON with fields:\n"
        "- task_shape\n"
        "- domain_hint\n"
        "- preferred_tool\n"
        "- reason\n"
        "- confidence\n"
        "- parsed_args\n"
        "- missing_fields\n"
    )
    resp, _usage = await llm.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        output_format="json",
        json_schema=ROUTER_JSON_SCHEMA,
        schema_name="DeepLensRoute",
        strict_schema=True,
        validate_json=True,
        max_output_tokens=320,
    )
    obj = json.loads(resp) if isinstance(resp, str) else resp
    task_shape = TaskShape(str(obj.get("task_shape", "unsupported")).strip().lower())
    domain_hint = DomainHint(str(obj.get("domain_hint", "unknown")).strip().lower())
    preferred_tool = obj.get("preferred_tool")
    parsed_args = obj.get("parsed_args") or {}
    missing_fields = obj.get("missing_fields") or []
    return {
        "task_shape": task_shape,
        "domain_hint": domain_hint,
        "preferred_tool": preferred_tool,
        "context_mode": _default_context_mode(state),
        "reason": str(obj.get("reason") or "llm_route"),
        "confidence": float(obj.get("confidence") or 0.5),
        "task": _make_task(
            message=message,
            attachments=attachments,
            task_shape=task_shape,
            domain_hint=domain_hint,
            preferred_tool=preferred_tool,
            parsed_args=parsed_args,
            missing_fields=missing_fields,
        ),
    }


async def route(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    state: DeepLensState,
    context: Any | None = None,
) -> RouteResult:
    slash = apply_slash_command(message, attachments, state)
    if slash is not None:
        return {
            "decision": slash.decision,
            "state": slash.state,
            "immediate_reply": slash.reply,
        }
    state_override = _state_sensitive_route(message, state)
    if state_override is not None:
        return {
            "decision": state_override,
            "state": state,
            "immediate_reply": None,
        }
    if context is not None:
        try:
            decision = await _llm_route(
                message=message,
                attachments=attachments,
                state=state,
                context=context,
            )
            return {"decision": decision, "state": state, "immediate_reply": None}
        except Exception:
            context.logger().exception("deeplens_v2: llm router failed; falling back")
    return {
        "decision": _fallback_route(message, attachments, state),
        "state": state,
        "immediate_reply": None,
    }
