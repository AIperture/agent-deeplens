"""Agenda building: deterministic capability-to-tool mapping with LLM fallback.

Driven entirely by policy.CAPABILITY_TOOL_MAP and policy.SEQUENCING_RULES.
"""
from __future__ import annotations

import json
from typing import Any

from . import policy
from .arg_resolver import build_missing_prompt, compute_missing_fields
from .tools.registry import get_allowed_tools
from .types import (
    ActionAgenda,
    AgendaAction,
    AgendaStatus,
    AgentState,
    IntentFrame,
    Task,
)


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------

def _capability_positions(text: str) -> list[tuple[int, str]]:
    """Scan user text for capability keywords, returning (position, capability) sorted by position."""
    lowered = (text or "").lower()
    found: list[tuple[int, str]] = []
    for capability, patterns in policy.CAPABILITY_PATTERNS:
        for pattern in patterns:
            idx = lowered.find(pattern)
            if idx >= 0:
                found.append((idx, capability))
                break
    found.sort(key=lambda item: item[0])
    dedup: list[tuple[int, str]] = []
    seen: set[str] = set()
    for idx, capability in found:
        if capability not in seen:
            dedup.append((idx, capability))
            seen.add(capability)
    return dedup


def build_intent_frame(task: Task, state: AgentState) -> IntentFrame:
    """Build an intent frame from the task, detecting capabilities and sequencing."""
    positions = _capability_positions(task.user_goal)
    capabilities = [capability for _idx, capability in positions]

    if not capabilities:
        # Fallback: try to infer from domain_hint
        for domain, caps in policy.DOMAIN_CAPABILITY_MAP.items():
            if task.domain_hint == domain and caps:
                capabilities = caps[:1]
                break

    # Apply sequencing rules
    sequencing_hints: list[str] = []
    cap_set = set(capabilities)
    for first, second in policy.SEQUENCING_RULES:
        if first in cap_set and second in cap_set:
            sequencing_hints.append(f"{first}_before_{second}")

    task.requested_capabilities = capabilities
    task.sequencing_hints = sequencing_hints

    # Compute missing fields across all tools we plan to use
    all_missing: list[str] = []
    for cap in capabilities:
        tool_name = policy.CAPABILITY_TOOL_MAP.get(cap)
        if tool_name:
            for field_name in compute_missing_fields(task, tool_name):
                if field_name not in all_missing:
                    all_missing.append(field_name)
    task.missing_fields = all_missing

    summary = " -> ".join(capabilities) if capabilities else task.domain_hint
    return IntentFrame(
        user_goal=task.user_goal,
        dominant_domain=task.domain_hint,
        requested_capabilities=capabilities,
        sequencing_hints=sequencing_hints,
        missing_fields=list(task.missing_fields),
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Action building helpers
# ---------------------------------------------------------------------------

def _mk_action(idx: int, kind: str, name: str | None = None, args: dict[str, Any] | None = None, rationale: str = "") -> AgendaAction:
    return AgendaAction(
        action_id=f"a{idx}",
        kind=kind,
        name=name,
        args=dict(args or {}),
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Deterministic actions
# ---------------------------------------------------------------------------

def _deterministic_actions(task: Task, intent: IntentFrame) -> list[AgendaAction]:
    """Map capabilities to tool_call actions using policy.CAPABILITY_TOOL_MAP."""
    actions: list[AgendaAction] = []
    idx = 1

    # If there are missing required fields, ask user first
    if task.missing_fields:
        actions.append(
            _mk_action(
                idx,
                "ask_user",
                args={"prompt": build_missing_prompt(task, AgentState()), "expected_fields": task.missing_fields},
                rationale="Gather unresolved required fields before execution.",
            )
        )
        idx += 1

    capabilities = list(intent.requested_capabilities or [])
    if not capabilities:
        if task.task_shape.value == "direct_answer":
            return [_mk_action(1, "respond", args={"text": f"I can help with: {', '.join(policy.CAPABILITY_TOOL_MAP.keys())}."}, rationale="Direct answer.")]
        return []

    # Order capabilities by sequencing rules
    ordered = _apply_sequencing(capabilities)

    for capability in ordered:
        tool_name = policy.CAPABILITY_TOOL_MAP.get(capability)
        if not tool_name:
            continue

        # Insert approval action if required
        if capability in policy.APPROVAL_REQUIRED:
            actions.append(_mk_action(
                idx,
                "request_approval",
                tool_name,
                args={"approval_prompt": f"I'm ready to run `{tool_name}`. Approve?"},
                rationale=f"{capability} requires approval.",
            ))
            idx += 1

        actions.append(_mk_action(idx, "tool_call", tool_name, rationale=f"Execute {capability} capability."))
        idx += 1

    if actions and actions[-1].kind == "tool_call":
        actions.append(_mk_action(idx, "finish", rationale="Agenda complete."))

    return actions


def _apply_sequencing(capabilities: list[str]) -> list[str]:
    """Reorder capabilities based on policy.SEQUENCING_RULES."""
    ordered = list(capabilities)
    changed = True
    while changed:
        changed = False
        for first, second in policy.SEQUENCING_RULES:
            if first in ordered and second in ordered:
                fi = ordered.index(first)
                si = ordered.index(second)
                if fi > si:
                    ordered.remove(first)
                    ordered.insert(si, first)
                    changed = True
    return ordered


# ---------------------------------------------------------------------------
# LLM fallback
# ---------------------------------------------------------------------------

def _needs_llm_fallback(task: Task, intent: IntentFrame, actions: list[AgendaAction]) -> bool:
    goal = (task.user_goal or "").lower()
    if not actions:
        return True
    if any(token in goal for token in (" if ", " unless ", " compare ", " branch ", " choose best ", " depending on ")):
        return True
    return False


def _action_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["ask_user", "request_approval", "tool_call", "respond", "finish", "fail"]},
                        "name": {"type": ["string", "null"]},
                        "rationale": {"type": "string"},
                        "prompt": {"type": ["string", "null"]},
                    },
                    "required": ["kind", "name", "rationale", "prompt"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["actions"],
        "additionalProperties": False,
    }


async def _llm_actions(task: Task, intent: IntentFrame, context_bundle: Any, context: Any) -> list[AgendaAction]:
    llm = context.llm("fast")
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        policy.SKILL_ID,
        "skeleton.system",
        "skeleton.loop",
        separator="\n\n",
        fallback_keys=["skeleton.system"],
    )
    payload = {
        "user_goal": task.user_goal,
        "dominant_domain": task.domain_hint,
        "requested_capabilities": intent.requested_capabilities,
        "missing_fields": task.missing_fields,
        "allowed_tools": get_allowed_tools(),
    }
    response, _usage = await llm.chat(
        messages=[
            {
                "role": "system",
                "content": (
                    f"{system_prompt}\n\n"
                    "Plan a short action agenda. "
                    "Use only the allowed tools and prefer ask_user before expensive work when inputs are missing."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        output_format="json_schema",
        json_schema=_action_schema(),
        schema_name="SkeletonAgenda",
        strict_schema=True,
        validate_json=True,
        max_output_tokens=260,
        reasoning_effort="low",
    )
    obj = json.loads(response) if isinstance(response, str) else response
    actions: list[AgendaAction] = []
    for idx, item in enumerate(obj.get("actions", []), start=1):
        args: dict[str, Any] = {}
        if item.get("prompt"):
            key = "approval_prompt" if item.get("kind") == "request_approval" else "prompt"
            args[key] = item["prompt"]
        actions.append(_mk_action(idx, str(item["kind"]), item.get("name"), args=args, rationale=str(item.get("rationale") or "")))
    return actions


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def build_action_agenda(
    *,
    task: Task,
    state: AgentState,
    context_bundle: Any,
    context: Any,
) -> ActionAgenda:
    """Build an action agenda for the given task."""
    intent = build_intent_frame(task, state)
    deterministic = _deterministic_actions(task, intent)
    actions = deterministic

    if _needs_llm_fallback(task, intent, deterministic):
        try:
            llm_actions = await _llm_actions(task, intent, context_bundle, context)
            if llm_actions:
                actions = llm_actions
        except Exception:
            if hasattr(context, "logger"):
                context.logger().warning("skeleton: agenda planner llm fallback failed", exc_info=True)

    agenda = ActionAgenda(
        goal=task.user_goal,
        actions=actions,
        status=AgendaStatus.ACTIVE if actions else AgendaStatus.FAILED,
        resumable=True,
        next_action_hints=list(state.next_action_hints),
        metadata={"intent": intent.to_dict()},
    )
    state.active_intent = intent.to_dict()
    return agenda
