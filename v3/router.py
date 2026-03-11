from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .extraction import (
    attachment_suggests_lens,
    extract_analysis_request,
    extract_design_spec,
    extract_run_request,
    missing_design_fields,
)
from .types import (
    ContextMode,
    DEEPLENS_SKILL_ID,
    DeepLensState,
    DeepLensTask,
    DomainHint,
    MISSING_FIELD_CODES,
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


def _missing_fields_for_domain(domain_hint: DomainHint, task: DeepLensTask) -> list[str]:
    if domain_hint == DomainHint.DESIGN:
        return missing_design_fields(task.design_spec)
    if domain_hint in {DomainHint.ANALYSIS, DomainHint.OPTIMIZATION}:
        if not attachment_suggests_lens(task.attachments):
            return ["lens_source"]
    return []


def _normalize_missing_fields(values: list[Any] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    for value in values:
        item = str(value).strip()
        if item in MISSING_FIELD_CODES and item not in out:
            out.append(item)
    return out[:5]


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
    design_spec = extract_design_spec(message)
    analysis_request = extract_analysis_request(message, attachments)
    run_request = extract_run_request(message)
    task = DeepLensTask(
        user_goal=message,
        task_shape=task_shape,
        domain_hint=domain_hint,
        attachments=attachments,
        parsed_args=parsed_args or {},
        missing_fields=missing_fields or [],
        preferred_tool=preferred_tool,
        source_refs=analysis_request.get("source_refs", []),
        lens_source={},
        design_spec=design_spec,
        analysis_request=analysis_request,
        run_request=run_request,
        delivery_request={"mode": "summary_plus_files"},
    )
    if not task.missing_fields:
        task.missing_fields = _missing_fields_for_domain(domain_hint, task)
    return task


def _apply_state_defaults(task: DeepLensTask, state: DeepLensState) -> DeepLensTask:
    if not task.lens_source and state.active_source_ref:
        task.lens_source = dict(state.active_source_ref)
    if task.domain_hint in {DomainHint.ANALYSIS, DomainHint.OPTIMIZATION} and task.missing_fields:
        if state.active_lens_ref or state.active_source_ref:
            task.missing_fields = [field for field in task.missing_fields if field != "lens_source"]
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
            "task_shape": TaskShape.SINGLE_ACTION if attachments else TaskShape.MULTI_STEP,
            "domain_hint": DomainHint.ANALYSIS,
            "preferred_tool": "dl.analysis",
            "context_mode": ctx_mode,
            "reason": "slash:/analysis",
            "confidence": 1.0,
            "task": _make_task(
                message=message,
                attachments=attachments,
                task_shape=TaskShape.SINGLE_ACTION if attachments else TaskShape.MULTI_STEP,
                domain_hint=DomainHint.ANALYSIS,
                preferred_tool="dl.analysis",
            ),
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
            "task": _make_task(
                message=message,
                attachments=attachments,
                task_shape=TaskShape.MULTI_STEP,
                domain_hint=DomainHint.DESIGN,
                preferred_tool="dl.create_lens",
            ),
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
            "task": _make_task(
                message=message,
                attachments=attachments,
                task_shape=TaskShape.MULTI_STEP,
                domain_hint=DomainHint.OPTIMIZATION,
                preferred_tool="ag.spawn_graph",
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
        reply="Unknown command. Supported now: `/design`, `/analysis`, `/optimize`, `/mode full`, `/mode lite`.",
    )


def _detect_run_completed_pickup(
    message: str,
    user_meta: dict[str, Any] | None = None,
) -> str | None:
    """Detect structured run-completion pickup messages sent by the UI toast action."""
    # Check user_meta for structured type (from RunsPollingBridge sendMessage meta)
    if user_meta and user_meta.get("type") == "run_completed" and user_meta.get("run_id"):
        return str(user_meta["run_id"])
    # Also match the text pattern "Show results for completed run <run_id>"
    m = re.search(r"results?\s+for\s+(?:completed?\s+)?run\s+([a-zA-Z0-9_-]{8,})", (message or "").lower())
    if m:
        return m.group(1)
    return None


def _state_sensitive_route(
    message: str,
    state: DeepLensState,
    user_meta: dict[str, Any] | None = None,
) -> RouteDecision | None:
    msg = (message or "").strip().lower()

    # Run-completion pickup (from UI toast "View Results" action)
    pickup_run_id = _detect_run_completed_pickup(message, user_meta)
    if pickup_run_id:
        return {
            "task_shape": TaskShape.RUN_CONTROL,
            "domain_hint": DomainHint.OPTIMIZATION,
            "preferred_tool": "ag.status",
            "context_mode": _default_context_mode(state),
            "reason": "state:run_completed_pickup",
            "confidence": 1.0,
            "task": DeepLensTask(
                user_goal=message,
                task_shape=TaskShape.RUN_CONTROL,
                domain_hint=DomainHint.OPTIMIZATION,
                parsed_args={"run_id": pickup_run_id},
                preferred_tool="ag.status",
                run_request={"run_id": pickup_run_id},
            ),
        }

    if state.active_run_id and any(k in msg for k in ("status", "still running", "cancel", "stop", "abort")):
        preferred_tool = "ag.cancel" if any(k in msg for k in ("cancel", "stop", "abort")) else "ag.status"
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
                run_request={"run_id": state.active_run_id},
            ),
        }
    return None


def _message_mentions_analysis(msg: str) -> bool:
    keywords = ("analy", "analysis", "spot", "mtf", "rms", "evaluate")
    return any(k in msg for k in keywords)


def _message_mentions_design(msg: str) -> bool:
    keywords = ("design", "create lens", "create a lens", "build lens", "starting lens")
    return any(k in msg for k in keywords)


def _message_mentions_optimization(msg: str) -> bool:
    keywords = ("optimize", "optimization", "improve lens", "constraint", "objective")
    return any(k in msg for k in keywords)


def _fallback_route(message: str, attachments: list[dict[str, Any]], state: DeepLensState) -> RouteDecision:
    """
    A deterministic fallback routing based on simple heuristics, used when no slash command or state-sensitive route is triggered.
    This is a safety net to ensure that user requests are routed somewhere reasonable, and also serves as a source of weak signals for the LLM router to learn from.
    The heuristics are intentionally simple and high-precision, to avoid misrouting. The LLM router can be used to capture more complex patterns and edge cases.
    """
    msg = (message or "").lower()
    # Run-control routing is handled by _state_sensitive_route (higher priority).
    if attachment_suggests_lens(attachments) and not _message_mentions_design(msg) and not _message_mentions_optimization(msg):
        return {
            "task_shape": TaskShape.SINGLE_ACTION,
            "domain_hint": DomainHint.ANALYSIS,
            "preferred_tool": "dl.analysis",
            "context_mode": _default_context_mode(state),
            "reason": "fallback:attachment_analysis",
            "confidence": 0.85,
            "task": _make_task(
                message=message or "Analyze the uploaded lens.",
                attachments=attachments,
                task_shape=TaskShape.SINGLE_ACTION,
                domain_hint=DomainHint.ANALYSIS,
                preferred_tool="dl.analysis",
            ),
        }
    if _message_mentions_design(msg):
        return {
            "task_shape": TaskShape.MULTI_STEP,
            "domain_hint": DomainHint.DESIGN,
            "preferred_tool": "dl.create_lens",
            "context_mode": _default_context_mode(state),
            "reason": "fallback:design",
            "confidence": 0.75,
            "task": _make_task(
                message=message,
                attachments=attachments,
                task_shape=TaskShape.MULTI_STEP,
                domain_hint=DomainHint.DESIGN,
                preferred_tool="dl.create_lens",
            ),
        }
    if _message_mentions_analysis(msg):
        return {
            "task_shape": TaskShape.SINGLE_ACTION if attachment_suggests_lens(attachments) or state.active_lens_ref else TaskShape.MULTI_STEP,
            "domain_hint": DomainHint.ANALYSIS,
            "preferred_tool": "dl.analysis",
            "context_mode": _default_context_mode(state),
            "reason": "fallback:analysis",
            "confidence": 0.75,
            "task": _make_task(
                message=message,
                attachments=attachments,
                task_shape=TaskShape.SINGLE_ACTION if attachment_suggests_lens(attachments) or state.active_lens_ref else TaskShape.MULTI_STEP,
                domain_hint=DomainHint.ANALYSIS,
                preferred_tool="dl.analysis",
            ),
        }
    if _message_mentions_optimization(msg):
        return {
            "task_shape": TaskShape.MULTI_STEP,
            "domain_hint": DomainHint.OPTIMIZATION,
            "preferred_tool": "ag.spawn_graph",
            "context_mode": _default_context_mode(state),
            "reason": "fallback:optimization",
            "confidence": 0.75,
            "task": _make_task(
                message=message,
                attachments=attachments,
                task_shape=TaskShape.MULTI_STEP,
                domain_hint=DomainHint.OPTIMIZATION,
                preferred_tool="ag.spawn_graph",
            ),
        }
    if any(k in msg for k in ("what is", "how does", "explain", "why", "interpret")):
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
        "Classify the request into one task shape and one domain hint.\n"
        "Use only these missing_fields codes when needed: "
        + ", ".join(MISSING_FIELD_CODES)
        + ".\n"
        "Keep missing_fields short codes only, not prose.\n"
        f"Message:\n{message}\n\n"
        f"State:\n"
        f"- context_mode={state.context_mode}\n"
        f"- active_run_id={state.active_run_id or 'none'}\n"
        f"- active_lens_ref={state.active_lens_ref or 'none'}\n"
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
        output_format="json_schema",
        json_schema=ROUTER_JSON_SCHEMA,
        schema_name="DeepLensV3Route",
        strict_schema=True,
        validate_json=True,
        max_output_tokens=220,
        reasoning_effort="low",
    )
    obj = json.loads(resp) if isinstance(resp, str) else resp
    task_shape = TaskShape(str(obj.get("task_shape", "unsupported")).strip().lower())
    domain_hint = DomainHint(str(obj.get("domain_hint", "unknown")).strip().lower())
    preferred_tool = obj.get("preferred_tool")
    parsed_args_raw = obj.get("parsed_args")
    if isinstance(parsed_args_raw, str) and parsed_args_raw.strip():
        try:
            parsed_args = json.loads(parsed_args_raw)
            if not isinstance(parsed_args, dict):
                parsed_args = {}
        except Exception:
            parsed_args = {}
    else:
        parsed_args = {}
    missing_fields = _normalize_missing_fields(obj.get("missing_fields") or [])
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
    user_meta: dict[str, Any] | None = None,
) -> RouteResult:
    slash = apply_slash_command(message, attachments, state)
    if slash is not None:
        slash.decision["task"] = _apply_state_defaults(slash.decision["task"], state)
        return {
            "decision": slash.decision,
            "state": slash.state,
            "immediate_reply": slash.reply,
        }
    state_override = _state_sensitive_route(message, state, user_meta=user_meta)
    if state_override is not None:
        state_override["task"] = _apply_state_defaults(state_override["task"], state)
        return {
            "decision": state_override,
            "state": state,
            "immediate_reply": None,
        }
    fallback = _fallback_route(message, attachments, state)
    fallback["task"] = _apply_state_defaults(fallback["task"], state)
    if fallback["reason"] != "fallback:unsupported":
        return {
            "decision": fallback,
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
            decision["task"] = _apply_state_defaults(decision["task"], state)
            return {"decision": decision, "state": state, "immediate_reply": None}
        except Exception:
            context.logger().warning("deeplens_v3: llm router failed; using deterministic fallback")
    return {
        "decision": fallback,
        "state": state,
        "immediate_reply": None,
    }
