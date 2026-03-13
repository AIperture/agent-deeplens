from __future__ import annotations

import json
from typing import Any

from . import policy
from .types import AgendaAction, AgendaStatus, CapabilityIntent, PlanAgenda, TemplateState, TemplateTask


def _capability_to_intent(capability_name: str, task: TemplateTask) -> CapabilityIntent:
    meta = policy.CAPABILITY_CATALOG[capability_name]
    return CapabilityIntent(
        capability_name=capability_name,
        goal=task.user_goal,
        rationale=f"Requested capability `{capability_name}` inferred from the user goal.",
        required_fields=list(meta["required_fields"]),
        optional_fields=list(meta["optional_fields"]),
        candidate_tools=list(meta["candidate_tools"]),
        expected_output=str(meta["expected_output"]),
        planner_args={k: v for k, v in task.parsed_args.items() if k in set(meta["required_fields"]) | set(meta["optional_fields"])},
    )


def _deterministic_capabilities(task: TemplateTask) -> list[str]:
    requested = [cap for cap in task.requested_capabilities if cap in policy.CAPABILITY_CATALOG]
    if requested:
        return list(dict.fromkeys(requested))
    if task.task_shape.value == "direct_answer":
        return ["inspect_context", "summarize_result"]
    return []


def _mk_action(idx: int, kind: str, *, capability: CapabilityIntent | None = None, name: str | None = None, args: dict[str, Any] | None = None, rationale: str = "") -> AgendaAction:
    return AgendaAction(
        action_id=f"t{idx}",
        kind=kind,
        capability=capability,
        name=name,
        args=dict(args or {}),
        rationale=rationale,
    )


def _deterministic_plan(task: TemplateTask) -> list[AgendaAction]:
    capabilities = _deterministic_capabilities(task)
    actions: list[AgendaAction] = []
    for idx, capability_name in enumerate(capabilities, start=1):
        intent = _capability_to_intent(capability_name, task)
        actions.append(_mk_action(idx, "bind_and_execute", capability=intent, rationale=intent.rationale))
    if actions:
        actions.append(_mk_action(len(actions) + 1, "finish", rationale="Agenda complete."))
    elif task.task_shape.value == "unsupported":
        actions.append(_mk_action(1, "fail", args={"text": policy.UNSUPPORTED_MESSAGE}, rationale="Unsupported input."))
    else:
        actions.append(_mk_action(1, "respond", args={"text": "I need a clearer capability request to continue."}, rationale="Clarify"))
    return actions


async def _llm_plan(task: TemplateTask, context_bundle: Any, context: Any) -> list[AgendaAction] | None:
    try:
        llm = context.llm("fast")
        schema = {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["bind_and_execute", "ask_user", "request_approval", "respond", "finish", "fail"]},
                            "capability_name": {"type": ["string", "null"], "enum": [*policy.CAPABILITY_CATALOG.keys(), None]},
                            "rationale": {"type": "string"},
                        },
                        "required": ["kind", "capability_name", "rationale"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["actions"],
            "additionalProperties": False,
        }
        payload = {
            "user_goal": task.user_goal,
            "parsed_args": task.parsed_args,
            "available_capabilities": list(policy.CAPABILITY_CATALOG.keys()),
            "working_state": context_bundle.working_state if context_bundle else {},
        }
        response, _usage = await llm.chat(
            messages=[
                {"role": "system", "content": "Plan a short capability agenda. Use only allowed capability names and bind_and_execute for capabilities."},
                {"role": "user", "content": json.dumps(payload)},
            ],
            output_format="json_schema",
            json_schema=schema,
            schema_name="TemplateAgenda",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=220,
        )
        obj = json.loads(response) if isinstance(response, str) else response
        actions: list[AgendaAction] = []
        for idx, item in enumerate(obj.get("actions") or [], start=1):
            kind = str(item.get("kind") or "respond")
            capability_name = item.get("capability_name")
            intent = _capability_to_intent(str(capability_name), task) if capability_name else None
            actions.append(_mk_action(idx, kind, capability=intent, rationale=str(item.get("rationale") or "")))
        return actions or None
    except Exception:
        return None


async def build_plan_agenda(
    *,
    task: TemplateTask,
    state: TemplateState,
    context_bundle: Any,
    context: Any,
) -> PlanAgenda:
    actions = _deterministic_plan(task)
    if context is not None and (" if " in task.user_goal.lower() or "compare" in task.user_goal.lower()):
        llm_actions = await _llm_plan(task, context_bundle, context)
        if llm_actions:
            actions = llm_actions
            if actions[-1].kind not in {"finish", "fail", "respond"}:
                actions.append(_mk_action(len(actions) + 1, "finish", rationale="LLM plan complete."))
    print(f"🍎 Built plan agenda with {len(actions)} actions for task: '{task.user_goal}'")
    return PlanAgenda(
        goal=task.user_goal,
        actions=actions,
        status=AgendaStatus.ACTIVE if actions else AgendaStatus.FAILED,
        resumable=True,
        next_action_hints=list(state.next_action_hints),
        metadata={"requested_capabilities": list(task.requested_capabilities)},
    )
