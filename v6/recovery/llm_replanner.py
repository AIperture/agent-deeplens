from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from ..planning.agenda_planner import _mk_action
from ..tools.tool_registry import TOOL_REGISTRY
from ..types import AgendaAction, DeepLensTask, RecoveryDecision, RecoveryDecisionKind, ResponseOutcomeKind, ToolResult


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "decision_kind": {"type": "string", "enum": ["retry_action", "replace_remaining_agenda", "ask_user", "escalate", "fail"]},
            "reason": {"type": "string"},
            "updated_task_patch_json": {"type": "string"},
            "replacement_actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["ask_user", "request_approval", "tool_call", "respond", "finish", "fail"]},
                        "name": {"type": ["string", "null"]},
                        "prompt": {"type": ["string", "null"]},
                        "rationale": {"type": "string"},
                    },
                    "required": ["kind", "name", "prompt", "rationale"],
                    "additionalProperties": False,
                },
            },
            "ask_user_prompt": {"type": ["string", "null"]},
            "escalate_reason": {"type": ["string", "null"]},
        },
        "required": ["decision_kind", "reason", "updated_task_patch_json", "replacement_actions", "ask_user_prompt", "escalate_reason"],
        "additionalProperties": False,
    }


def _apply_task_patch(task: DeepLensTask, patch: dict[str, Any]) -> DeepLensTask:
    updated = deepcopy(task)
    for key in ("design_spec", "analysis_request", "run_request", "delivery_request", "lens_source"):
        value = patch.get(key)
        if isinstance(value, dict):
            current = getattr(updated, key)
            if isinstance(current, dict):
                current.update(value)
    if isinstance(patch.get("requested_capabilities"), list):
        updated.requested_capabilities = [str(item) for item in patch["requested_capabilities"]]
    if isinstance(patch.get("notes"), list):
        updated.notes.extend(str(item) for item in patch["notes"])
    return updated


def _decode_actions(items: list[dict[str, Any]]) -> list[AgendaAction]:
    actions: list[AgendaAction] = []
    for idx, item in enumerate(items, start=1):
        name = item.get("name")
        if name is not None and name not in TOOL_REGISTRY:
            continue
        args: dict[str, Any] = {}
        prompt = item.get("prompt")
        if prompt:
            args["approval_prompt" if item.get("kind") == "request_approval" else "prompt"] = prompt
        actions.append(_mk_action(idx, str(item.get("kind") or "fail"), name, args=args, rationale=str(item.get("rationale") or "")))
    return actions


async def llm_replan_after_failure(
    *,
    task: DeepLensTask,
    state: Any,
    agenda: Any,
    failed_action: AgendaAction,
    result: ToolResult,
    context_bundle: Any,
    context: Any,
) -> RecoveryDecision | None:
    llm = context.llm("fast")
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        "aethergraph-agent-deeplens-v6",
        "deeplens.system",
        "deeplens.loop",
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )
    payload = {
        "goal": task.user_goal,
        "domain_hint": task.domain_hint.value,
        "requested_capabilities": task.requested_capabilities,
        "failed_action": {"kind": failed_action.kind, "name": failed_action.name, "args": failed_action.args},
        "tool_result": {
            "summary": result.summary,
            "failure_kind": result.failure_kind.value if result.failure_kind is not None else None,
            "missing_fields": result.missing_fields,
            "invalid_fields": result.invalid_fields,
            "repair_hints": result.repair_hints,
            "dependency_failures": result.dependency_failures,
            "diagnostics": result.diagnostics,
        },
        "working_state": context_bundle.working_state,
        "completed_actions": [action.name or action.kind for action in agenda.actions if action.status.value == "completed"],
        "allowed_tools": list(TOOL_REGISTRY.keys()),
        "policy": {
            "same_domain_only": True,
            "approval_policy_must_hold": True,
            "do_not_continue_blocked_tail": True,
        },
    }
    try:
        response, _usage = await llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n\n"
                        "You are replanning within the same DeepLens task after a failed action. "
                        "You may retry, replace the remaining agenda, ask the user, escalate, or fail. "
                        "Do not switch task/domain, invent new tools, or bypass approval gates."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=_schema(),
            schema_name="DeepLensV6FailureReplan",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=360,
            reasoning_effort="low",
        )
        obj = json.loads(response) if isinstance(response, str) else response
    except Exception:
        context.logger().warning("deeplens_v6: llm replanner failed", exc_info=True)
        return None

    try:
        task_patch = json.loads(obj.get("updated_task_patch_json") or "{}")
    except Exception:
        task_patch = {}
    patched_task = _apply_task_patch(task, task_patch if isinstance(task_patch, dict) else {})
    decision_kind = RecoveryDecisionKind(str(obj.get("decision_kind") or "fail"))
    replacement_actions = _decode_actions(list(obj.get("replacement_actions") or []))

    if decision_kind == RecoveryDecisionKind.RETRY_ACTION:
        retry_action = replacement_actions[0] if replacement_actions else None
        return RecoveryDecision(kind=decision_kind, reason=str(obj.get("reason") or "Retry the failed action with a patched input set."), action=retry_action, task=patched_task)
    if decision_kind == RecoveryDecisionKind.REPLACE_REMAINING_AGENDA:
        return RecoveryDecision(kind=decision_kind, reason=str(obj.get("reason") or "Replace the remaining agenda after the failure."), replacement_actions=replacement_actions, task=patched_task)
    if decision_kind == RecoveryDecisionKind.ASK_USER:
        return RecoveryDecision(kind=decision_kind, reason=str(obj.get("reason") or "Need a narrower clarification to continue."), ask_user_prompt=str(obj.get("ask_user_prompt") or ""), task=patched_task, outcome_kind=ResponseOutcomeKind.WAITING)
    if decision_kind == RecoveryDecisionKind.ESCALATE:
        return RecoveryDecision(kind=decision_kind, reason=str(obj.get("escalate_reason") or obj.get("reason") or "Need human assistance to proceed."), task=patched_task, outcome_kind=ResponseOutcomeKind.ESCALATE)
    return RecoveryDecision(kind=RecoveryDecisionKind.FAIL, reason=str(obj.get("reason") or "The failed action could not be recovered safely."), task=patched_task, outcome_kind=ResponseOutcomeKind.FAILED)
