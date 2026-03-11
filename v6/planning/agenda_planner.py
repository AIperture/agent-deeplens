from __future__ import annotations

import json
from typing import Any

from ..extraction import build_missing_prompt, compute_missing_fields
from ..types import (
    DEEPLENS_SKILL_ID,
    ActionAgenda,
    AgendaAction,
    AgendaStatus,
    DeepLensState,
    DeepLensTask,
    DomainHint,
    IntentFrame,
)


_CAPABILITY_PATTERNS: list[tuple[str, list[str]]] = [
    ("design", ["design", "create lens", "create a lens", "build lens", "build a lens", "make a lens", "new lens"]),
    ("analysis", ["analy", "simulate", "simulation", "spot", "mtf", "rms", "evaluate"]),
    ("export", ["export", "save", "download", "zmx", "json"]),
    ("optimization", ["optimize", "optimization", "improve lens", "sharper", "objective", "constraint"]),
    ("run_control", ["status", "cancel", "stop", "abort"]),
]


def _capability_positions(text: str) -> list[tuple[int, str]]:
    lowered = (text or "").lower()
    found: list[tuple[int, str]] = []
    for capability, patterns in _CAPABILITY_PATTERNS:
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


def build_intent_frame(task: DeepLensTask, state: DeepLensState) -> IntentFrame:
    positions = _capability_positions(task.user_goal)
    capabilities = [capability for _idx, capability in positions]
    if not capabilities:
        fallback = {
            DomainHint.DESIGN: ["design"],
            DomainHint.ANALYSIS: ["analysis"],
            DomainHint.EXPORT: ["export"],
            DomainHint.OPTIMIZATION: ["optimization"],
            DomainHint.RUN_CONTROL: ["run_control"],
        }.get(task.domain_hint, [])
        capabilities = fallback

    sequencing_hints: list[str] = []
    if "design" in capabilities and "analysis" in capabilities:
        sequencing_hints.append("design_before_analysis")
    if "analysis" in capabilities and "optimization" in capabilities:
        sequencing_hints.append("analysis_before_optimization")
    if "design" in capabilities and "optimization" in capabilities:
        sequencing_hints.append("design_before_optimization")

    task.requested_capabilities = capabilities
    task.sequencing_hints = sequencing_hints
    task.missing_fields = compute_missing_fields(task)

    summary = " -> ".join(capabilities) if capabilities else task.domain_hint.value
    return IntentFrame(
        user_goal=task.user_goal,
        dominant_domain=task.domain_hint,
        requested_capabilities=capabilities,
        sequencing_hints=sequencing_hints,
        missing_fields=list(task.missing_fields),
        active_refs={
            "active_source_ref": state.active_source_ref,
            "active_run_id": state.active_run_id,
        },
        summary=summary,
    )


def _mk_action(idx: int, kind: str, name: str | None = None, args: dict[str, Any] | None = None, rationale: str = "") -> AgendaAction:
    return AgendaAction(
        action_id=f"a{idx}",
        kind=kind,
        name=name,
        args=dict(args or {}),
        rationale=rationale,
    )


def _deterministic_actions(task: DeepLensTask, intent: IntentFrame) -> list[AgendaAction]:
    actions: list[AgendaAction] = []
    idx = 1
    missing = compute_missing_fields(task)
    if missing:
        actions.append(
            _mk_action(
                idx,
                "ask_user",
                args={"prompt": build_missing_prompt(task), "expected_fields": missing},
                rationale="Gather unresolved required fields before execution.",
            )
        )
        idx += 1

    capabilities = list(intent.requested_capabilities or [])
    if not capabilities:
        if task.task_shape.value == "direct_answer":
            return [_mk_action(1, "respond", args={"text": "I can help with lens design, analysis, export, optimization, and run control."}, rationale="Direct answer requested.")]
        return []

    for capability in capabilities:
        if capability == "design":
            actions.append(_mk_action(idx, "tool_call", "dl.create_lens", rationale="Create a starting lens from the requested design spec."))
            idx += 1
        elif capability == "analysis":
            actions.append(_mk_action(idx, "tool_call", "dl.analysis", rationale="Run analysis on the active or newly created lens."))
            idx += 1
        elif capability == "export":
            actions.append(_mk_action(idx, "tool_call", "dl.export_lens", rationale="Export the active lens in the requested formats."))
            idx += 1
        elif capability == "optimization":
            actions.append(_mk_action(idx, "request_approval", "ag.spawn_graph", args={"approval_prompt": "I’m ready to submit the background optimization run. Approve?"}, rationale="Background optimization needs approval."))
            idx += 1
            actions.append(_mk_action(idx, "tool_call", "ag.spawn_graph", rationale="Submit the optimization workflow as a background run."))
            idx += 1
        elif capability == "run_control":
            tool_name = task.preferred_tool if task.preferred_tool in {"ag.status", "ag.cancel"} else "ag.status"
            actions.append(_mk_action(idx, "tool_call", tool_name, rationale="Run-control tool is explicit."))
            idx += 1

    if actions and (actions[-1].kind != "tool_call" or actions[-1].name not in {"ag.spawn_graph"}):
        actions.append(_mk_action(idx, "finish", rationale="Agenda complete."))
    return actions


def _needs_llm_fallback(task: DeepLensTask, intent: IntentFrame, actions: list[AgendaAction]) -> bool:
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


async def _llm_actions(task: DeepLensTask, intent: IntentFrame, context_bundle: Any, context: Any) -> list[AgendaAction]:
    llm = context.llm("fast")
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        DEEPLENS_SKILL_ID,
        "deeplens.system",
        "deeplens.loop",
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )
    payload = {
        "user_goal": task.user_goal,
        "dominant_domain": task.domain_hint.value,
        "requested_capabilities": intent.requested_capabilities,
        "missing_fields": task.missing_fields,
        "active_source_ref": context_bundle.working_state.get("active_source_ref"),
        "allowed_tools": ["dl.create_lens", "dl.analysis", "dl.export_lens", "ag.spawn_graph", "ag.status", "ag.cancel"],
    }
    response, _usage = await llm.chat(
        messages=[
            {
                "role": "system",
                "content": (
                    f"{system_prompt}\n\n"
                    "Plan a short DeepLens action agenda. "
                    "Use only the allowed tools and prefer ask_user before expensive work when inputs are missing."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        output_format="json_schema",
        json_schema=_action_schema(),
        schema_name="DeepLensV6Agenda",
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


async def build_action_agenda(
    *,
    task: DeepLensTask,
    state: DeepLensState,
    context_bundle: Any,
    context: Any,
) -> ActionAgenda:
    intent = build_intent_frame(task, state)
    deterministic = _deterministic_actions(task, intent)
    actions = deterministic
    if _needs_llm_fallback(task, intent, deterministic):
        try:
            llm_actions = await _llm_actions(task, intent, context_bundle, context)
            if llm_actions:
                actions = llm_actions
        except Exception:
            context.logger().warning("deeplens_v6: agenda planner llm fallback failed", exc_info=True)

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
