"""Turn role detection + task routing.

Same cascade as v6: slash commands -> turn role detection -> heuristic fallback -> LLM fallback.
All domain keyword detection uses policy.CAPABILITY_PATTERNS instead of hardcoded optics patterns.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any

from . import policy
from .types import (
    ACTIVE_TASK_STATUSES,
    AgentState,
    ContextMode,
    RouteDecision,
    RouteResult,
    Task,
    TaskShape,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_context_mode(state: AgentState) -> ContextMode:
    return ContextMode.FULL if state.context_mode == "full" else ContextMode.LITE


def _normalize_message(message: str) -> str:
    return " ".join((message or "").strip().lower().split())


def _make_task(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    task_shape: TaskShape,
    domain_hint: str = "general",
    preferred_tool: str | None = None,
) -> Task:
    return Task(
        user_goal=message,
        task_shape=task_shape,
        domain_hint=domain_hint,
        attachments=attachments,
        preferred_tool=preferred_tool,
    )


def _active_task_from_state(state: AgentState) -> Task | None:
    return Task.from_dict(state.active_task if isinstance(state.active_task, dict) else None)


def _is_task_continuation_eligible(task: Task | None) -> bool:
    return task is not None and task.task_status in ACTIVE_TASK_STATUSES


# ---------------------------------------------------------------------------
# Capability detection from message
# ---------------------------------------------------------------------------

def _detect_capabilities(msg: str) -> list[str]:
    """Scan message for capability keywords using policy.CAPABILITY_PATTERNS."""
    lowered = (msg or "").lower()
    found: list[tuple[int, str]] = []
    for capability, patterns in policy.CAPABILITY_PATTERNS:
        for pattern in patterns:
            idx = lowered.find(pattern)
            if idx >= 0:
                found.append((idx, capability))
                break
    found.sort(key=lambda item: item[0])
    seen: set[str] = set()
    result: list[str] = []
    for _idx, cap in found:
        if cap not in seen:
            result.append(cap)
            seen.add(cap)
    return result


def _infer_task_shape(capabilities: list[str]) -> TaskShape:
    if not capabilities:
        return TaskShape.UNSUPPORTED
    if len(capabilities) == 1:
        return TaskShape.SINGLE_ACTION
    return TaskShape.MULTI_STEP


# ---------------------------------------------------------------------------
# Turn role detection
# ---------------------------------------------------------------------------

def _looks_like_short_answer(msg: str) -> bool:
    if not msg:
        return False
    if msg in policy.SHORT_ANSWER_TOKENS:
        return True
    tokens = msg.split()
    return len(tokens) <= 3 and all(len(token) <= 12 for token in tokens)


def _looks_like_continuation(msg: str) -> bool:
    return any(phrase in msg for phrase in policy.CONTINUATION_PHRASES)


def _looks_like_explicit_new_task(msg: str) -> bool:
    return any(marker in msg for marker in policy.NEW_TASK_MARKERS)


def _looks_like_standalone_request(msg: str) -> bool:
    if any(msg.startswith(f"{verb} ") for verb in policy.NEW_TASK_VERBS):
        return True
    return len(msg.split()) >= 6 and any(f" {verb} " in f" {msg} " for verb in policy.NEW_TASK_VERBS)


def _merge_continuation_turn(active_task: Task, *, message: str, attachments: list[dict[str, Any]]) -> Task:
    merged = Task.from_dict(asdict(active_task)) or active_task
    if message:
        merged.notes.append(f"continuation_turn:{message}")
    if attachments:
        merged.attachments.extend(attachments)
    return merged


async def _llm_turn_role(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    state: AgentState,
    active_task: Task,
    context: Any,
) -> tuple[str, str] | None:
    """LLM arbitration for turn role classification."""
    llm = context.llm("fast")
    payload = {
        "message": message,
        "attachments_count": len(attachments),
        "active_task": {
            "domain_hint": active_task.domain_hint,
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
                        "Classify whether the latest user turn continues the current task, "
                        "answers a pending interaction, or starts a new task."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=schema,
            schema_name="SkeletonTurnRole",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=140,
            reasoning_effort="low",
        )
    except Exception:
        if hasattr(context, "logger"):
            context.logger().warning("skeleton: turn-role llm arbitration failed", exc_info=True)
        return None
    obj = json.loads(response) if isinstance(response, str) else response
    if float(obj.get("confidence") or 0.0) < 0.6:
        return None
    return str(obj.get("turn_role") or "new_task"), str(obj.get("reason") or "llm:turn_role")


async def _detect_turn_role(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    state: AgentState,
    context: Any | None,
) -> tuple[str, Task | None, str] | None:
    active_task = _active_task_from_state(state)
    if not _is_task_continuation_eligible(active_task):
        return None

    msg = _normalize_message(message)
    pending_context = bool(state.pending_action or state.runtime_missing_fields or state.active_recovery)

    if _looks_like_explicit_new_task(msg):
        return ("new_task", None, "state:new_task_marker")

    merged_task = _merge_continuation_turn(active_task, message=message, attachments=attachments)

    # Check if the turn is answering pending fields or continuing
    if pending_context and (_looks_like_short_answer(msg) or _looks_like_continuation(msg)):
        return ("answer_pending_interaction", merged_task, "state:answer_pending_interaction")

    if _looks_like_continuation(msg):
        return ("continue_active_task", merged_task, "state:continuation")

    if _looks_like_standalone_request(msg) and msg:
        new_caps = _detect_capabilities(msg)
        old_caps = set(active_task.requested_capabilities or [])
        if new_caps and not set(new_caps).intersection(old_caps):
            return ("new_task", None, "state:new_task_domain_shift")

    if not msg and attachments:
        return ("answer_pending_interaction" if pending_context else "continue_active_task", merged_task, "state:attachment_continuation")

    # LLM fallback
    if context is not None:
        llm_role = await _llm_turn_role(
            message=message, attachments=attachments,
            state=state, active_task=active_task, context=context,
        )
        if llm_role is not None:
            role, reason = llm_role
            if role == "new_task":
                return ("new_task", None, reason)
            return (role, merged_task, reason)

    return None


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

def apply_slash_command(message: str, attachments: list[dict[str, Any]], state: AgentState) -> dict[str, Any] | None:
    msg = (message or "").strip()
    if not msg.startswith("/"):
        return None

    # Check registered slash commands from policy
    for cmd in policy.SLASH_COMMANDS:
        cmd_name = cmd.get("name", "")
        if msg.lower().startswith(cmd_name.lower()):
            # Generic slash command handling — vertical agents override policy.SLASH_COMMANDS
            return None  # Not implemented in skeleton; verticals add their own

    return None


# ---------------------------------------------------------------------------
# Heuristic fallback routing
# ---------------------------------------------------------------------------

def _fallback_route(message: str, attachments: list[dict[str, Any]], state: AgentState) -> RouteDecision:
    msg = (message or "").lower()
    capabilities = _detect_capabilities(msg)

    if capabilities:
        task_shape = _infer_task_shape(capabilities)
        preferred_tool = policy.CAPABILITY_TOOL_MAP.get(capabilities[0])
        return RouteDecision({
            "task_shape": task_shape,
            "domain_hint": "general",
            "preferred_tool": preferred_tool,
            "context_mode": _default_context_mode(state),
            "reason": f"fallback:detected_capabilities:{','.join(capabilities)}",
            "confidence": 0.8,
            "task": _make_task(message=message, attachments=attachments, task_shape=task_shape, preferred_tool=preferred_tool),
        })

    # Direct answer patterns
    if any(k in msg for k in ("what is", "how does", "explain", "why", "help")):
        return RouteDecision({
            "task_shape": TaskShape.DIRECT_ANSWER,
            "domain_hint": "general",
            "preferred_tool": None,
            "context_mode": _default_context_mode(state),
            "reason": "fallback:direct_answer",
            "confidence": 0.6,
            "task": _make_task(message=message, attachments=attachments, task_shape=TaskShape.DIRECT_ANSWER),
        })

    return RouteDecision({
        "task_shape": TaskShape.UNSUPPORTED,
        "domain_hint": "general",
        "preferred_tool": None,
        "context_mode": _default_context_mode(state),
        "reason": "fallback:unsupported",
        "confidence": 0.5,
        "task": _make_task(message=message, attachments=attachments, task_shape=TaskShape.UNSUPPORTED),
    })


# ---------------------------------------------------------------------------
# LLM routing fallback
# ---------------------------------------------------------------------------

async def _llm_route(*, message: str, attachments: list[dict[str, Any]], state: AgentState, context: Any) -> RouteDecision:
    llm = context.llm()
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        policy.SKILL_ID,
        "skeleton.system",
        "skeleton.router",
        separator="\n\n",
        fallback_keys=["skeleton.system"],
    )
    tool_names = list(policy.CAPABILITY_TOOL_MAP.values())
    schema = {
        "type": "object",
        "properties": {
            "task_shape": {"type": "string", "enum": [x.value for x in TaskShape]},
            "domain_hint": {"type": "string"},
            "preferred_tool": {"type": ["string", "null"]},
            "reason": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["task_shape", "domain_hint", "preferred_tool", "reason", "confidence"],
        "additionalProperties": False,
    }
    user_prompt = (
        "Classify the request into one task shape and domain.\n"
        f"Available tools: {', '.join(tool_names)}\n"
        f"Message:\n{message}\n"
        f"Attachments count: {len(attachments)}\n"
    )
    resp, _usage = await llm.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        output_format="json_schema",
        json_schema=schema,
        schema_name="SkeletonRoute",
        strict_schema=True,
        validate_json=True,
        max_output_tokens=220,
        reasoning_effort="low",
    )
    obj = json.loads(resp) if isinstance(resp, str) else resp
    task_shape = TaskShape(str(obj.get("task_shape", "unsupported")).strip().lower())
    preferred_tool = obj.get("preferred_tool")
    return RouteDecision({
        "task_shape": task_shape,
        "domain_hint": str(obj.get("domain_hint") or "general"),
        "preferred_tool": preferred_tool,
        "context_mode": _default_context_mode(state),
        "reason": str(obj.get("reason") or "llm_route"),
        "confidence": float(obj.get("confidence") or 0.5),
        "task": _make_task(message=message, attachments=attachments, task_shape=task_shape, preferred_tool=preferred_tool),
    })


# ---------------------------------------------------------------------------
# Main router
# ---------------------------------------------------------------------------

async def route(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    state: AgentState,
    context: Any | None = None,
    user_meta: dict[str, Any] | None = None,
) -> RouteResult:
    """Route a user message to the appropriate task shape and domain."""

    # 1. Slash commands
    slash = apply_slash_command(message, attachments, state)
    if slash is not None:
        return RouteResult({"decision": slash, "state": state, "immediate_reply": slash.get("reply"), "turn_role": "slash_command"})

    # 2. Turn role detection (continuation vs new task)
    turn_role = await _detect_turn_role(message=message, attachments=attachments, state=state, context=context)
    if turn_role is not None:
        role, continuation_task, reason = turn_role
        if role != "new_task" and continuation_task is None:
            clarification = _make_task(message=message or reason, attachments=attachments, task_shape=TaskShape.DIRECT_ANSWER)
            decision = RouteDecision({
                "task_shape": clarification.task_shape,
                "domain_hint": clarification.domain_hint,
                "preferred_tool": clarification.preferred_tool,
                "context_mode": _default_context_mode(state),
                "reason": "state:continuation_clarification",
                "confidence": 0.7,
                "task": clarification,
            })
            return RouteResult({"decision": decision, "state": state, "immediate_reply": reason, "turn_role": role})

        if role != "new_task" and continuation_task is not None:
            continuation_task.task_status = "active" if role == "continue_active_task" else "waiting"
            state.active_agenda = None
            decision = RouteDecision({
                "task_shape": continuation_task.task_shape,
                "domain_hint": continuation_task.domain_hint,
                "preferred_tool": continuation_task.preferred_tool,
                "context_mode": _default_context_mode(state),
                "reason": reason,
                "confidence": 0.93 if role == "answer_pending_interaction" else 0.9,
                "task": continuation_task,
            })
            return RouteResult({"decision": decision, "state": state, "immediate_reply": None, "turn_role": role})

    # 3. Heuristic fallback
    fallback = _fallback_route(message, attachments, state)
    if fallback.get("reason") != "fallback:unsupported":
        return RouteResult({"decision": fallback, "state": state, "immediate_reply": None, "turn_role": "new_task"})

    # 4. LLM fallback
    if context is not None:
        try:
            decision = await _llm_route(message=message, attachments=attachments, state=state, context=context)
            return RouteResult({"decision": decision, "state": state, "immediate_reply": None, "turn_role": "new_task"})
        except Exception:
            if hasattr(context, "logger"):
                context.logger().warning("skeleton: llm router failed; using deterministic fallback")

    return RouteResult({"decision": fallback, "state": state, "immediate_reply": None, "turn_role": "new_task"})
