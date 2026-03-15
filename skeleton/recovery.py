"""Error recovery: deterministic strategies + LLM replan + escalation.

Merges v6's three files (recovery_engine, deterministic_repair, llm_replanner) into one.
All DeepLens-specific repairs removed; only generic strategies remain.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from . import policy
from .arg_resolver import build_missing_prompt
from .types import (
    AgendaAction,
    FailureKind,
    RecoveryDecision,
    RecoveryDecisionKind,
    ResponseOutcomeKind,
    Task,
    ToolResult,
)


# ---------------------------------------------------------------------------
# Failure signature
# ---------------------------------------------------------------------------

def _failure_signature(failed_action: Any, result: ToolResult) -> str:
    diagnostics = getattr(result, "diagnostics", {}) or {}
    if diagnostics.get("failure_signature"):
        return str(diagnostics["failure_signature"])
    return "|".join([
        str(getattr(failed_action, "name", None) or getattr(failed_action, "kind", "")),
        str(result.failure_kind.value if result.failure_kind is not None else "none"),
        str(result.error_code or "none"),
        str(result.summary or "")[:160],
    ])


# ---------------------------------------------------------------------------
# Deterministic recovery
# ---------------------------------------------------------------------------

def _merge_runtime_findings(task: Task, state: Any, result: ToolResult) -> None:
    """Merge newly discovered missing/invalid fields into task and state."""
    for field_name in result.missing_fields:
        if field_name not in task.missing_fields:
            task.missing_fields.append(field_name)
        if field_name not in state.runtime_missing_fields:
            state.runtime_missing_fields.append(field_name)
    if result.invalid_fields:
        task.notes.append(f"runtime_invalid_fields:{result.invalid_fields}")
        state.runtime_invalid_fields.update(result.invalid_fields)


async def _deterministic_recovery(
    *,
    task: Task,
    state: Any,
    failed_action: AgendaAction,
    result: ToolResult,
    **_: Any,
) -> RecoveryDecision | None:
    """Try generic deterministic recovery strategies."""
    _merge_runtime_findings(task, state, result)

    # Strategy 1: Missing inputs or user input needed -> ask user
    if result.needs_user_input or result.missing_fields:
        state.last_prompt_reason = "runtime_missing_fields"
        return RecoveryDecision(
            kind=RecoveryDecisionKind.ASK_USER,
            reason="Execution revealed missing required inputs.",
            ask_user_prompt=build_missing_prompt(task, state),
            task=task,
        )

    # Strategy 2: Invalid inputs that might be coercible -> retry with cleaned args
    if result.invalid_fields and result.repairable:
        patched = AgendaAction(
            action_id=failed_action.action_id,
            kind=failed_action.kind,
            name=failed_action.name,
            args=deepcopy(failed_action.args),
            rationale=f"Retry after fixing invalid fields: {list(result.invalid_fields.keys())}",
        )
        # Remove invalid fields so the arg resolver can re-resolve them from defaults
        for field_name in result.invalid_fields:
            patched.args.pop(field_name, None)
        return RecoveryDecision(
            kind=RecoveryDecisionKind.RETRY_ACTION,
            reason="Cleaned invalid fields for retry.",
            action=patched,
            task=task,
            retry_patch=deepcopy(patched.args),
        )

    # Strategy 3: Transient error under retry limit -> retry same args
    if result.failure_kind == FailureKind.TRANSIENT_ERROR:
        tool_key = failed_action.name or "tool"
        retries = state.retry_counters.get(tool_key, 0)
        if retries < policy.MAX_RETRIES_PER_TOOL:
            return RecoveryDecision(
                kind=RecoveryDecisionKind.RETRY_ACTION,
                reason=f"Retrying transient error (attempt {retries + 1}).",
                action=AgendaAction(
                    action_id=failed_action.action_id,
                    kind=failed_action.kind,
                    name=failed_action.name,
                    args=deepcopy(failed_action.args),
                    rationale="Transient error retry.",
                ),
                task=task,
            )

    return None


# ---------------------------------------------------------------------------
# LLM replan
# ---------------------------------------------------------------------------

async def _llm_replan(
    *,
    task: Task,
    state: Any,
    agenda: Any,
    failed_action: AgendaAction,
    result: ToolResult,
    context_bundle: Any,
    context: Any,
) -> RecoveryDecision | None:
    """Use LLM to generate a replacement action plan after failure."""
    try:
        from .tools.registry import get_allowed_tools

        llm = context.llm("fast")
        skills = context.skills()
        system_prompt = skills.compile_prompt(
            policy.SKILL_ID,
            "skeleton.system",
            "skeleton.loop",
            separator="\n\n",
            fallback_keys=["skeleton.system"],
        )

        failure_sig = _failure_signature(failed_action, result)
        payload = {
            "user_goal": task.user_goal,
            "failed_tool": failed_action.name,
            "failure_summary": result.summary,
            "failure_kind": result.failure_kind.value if result.failure_kind else "unknown",
            "failure_signature": failure_sig,
            "missing_fields": list(result.missing_fields),
            "invalid_fields": dict(result.invalid_fields),
            "allowed_tools": get_allowed_tools(),
            "remaining_capabilities": task.requested_capabilities,
        }

        schema = {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["retry_action", "replace_remaining_agenda", "ask_user", "escalate", "fail"]},
                "reason": {"type": "string"},
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["ask_user", "tool_call", "respond", "finish", "fail"]},
                            "name": {"type": ["string", "null"]},
                            "rationale": {"type": "string"},
                        },
                        "required": ["kind", "name", "rationale"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["decision", "reason", "actions"],
            "additionalProperties": False,
        }

        response, _usage = await llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n\n"
                        "A tool action failed. Decide how to recover: "
                        "retry_action, replace_remaining_agenda, ask_user, escalate, or fail. "
                        "If replacing, provide the new action sequence."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=schema,
            schema_name="SkeletonRecovery",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=300,
            reasoning_effort="low",
        )
        obj = json.loads(response) if isinstance(response, str) else response
        decision_str = str(obj.get("decision") or "fail")
        reason = str(obj.get("reason") or "LLM recovery decision")

        if decision_str == "ask_user":
            return RecoveryDecision(
                kind=RecoveryDecisionKind.ASK_USER,
                reason=reason,
                ask_user_prompt=reason,
                task=task,
            )

        if decision_str == "replace_remaining_agenda":
            replacement_actions: list[AgendaAction] = []
            for idx, item in enumerate(obj.get("actions", []), start=1):
                replacement_actions.append(AgendaAction(
                    action_id=f"r{idx}",
                    kind=str(item.get("kind") or "fail"),
                    name=item.get("name"),
                    rationale=str(item.get("rationale") or ""),
                ))
            if replacement_actions:
                return RecoveryDecision(
                    kind=RecoveryDecisionKind.REPLACE_REMAINING_AGENDA,
                    reason=reason,
                    replacement_actions=replacement_actions,
                    task=task,
                    replacement_reason=reason,
                )

        if decision_str == "escalate":
            return RecoveryDecision(
                kind=RecoveryDecisionKind.ESCALATE,
                reason=reason,
                task=task,
                outcome_kind=ResponseOutcomeKind.ESCALATE,
            )

        return None
    except Exception:
        if hasattr(context, "logger"):
            context.logger().warning("skeleton: LLM replan failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Main recovery orchestrator
# ---------------------------------------------------------------------------

async def recover_failed_action(
    *,
    task: Task,
    state: Any,
    agenda: Any,
    failed_action: AgendaAction,
    result: ToolResult,
    context_bundle: Any,
    context: Any,
) -> RecoveryDecision:
    """Orchestrate failure recovery: deterministic -> LLM -> escalate."""
    signature = _failure_signature(failed_action, result)
    state.recovery_attempts[signature] = state.recovery_attempts.get(signature, 0) + 1
    state.active_recovery = {
        "signature": signature,
        "tool_name": failed_action.name,
        "attempt": state.recovery_attempts[signature],
        "failure_kind": result.failure_kind.value if result.failure_kind is not None else None,
    }

    # 1. Try deterministic recovery
    deterministic = await _deterministic_recovery(
        task=task,
        state=state,
        failed_action=failed_action,
        result=result,
    )
    if deterministic is not None:
        if deterministic.kind == RecoveryDecisionKind.REPLACE_REMAINING_AGENDA:
            state.last_replan_reason = deterministic.reason
        return deterministic

    # 2. Try LLM replan if under attempt limit
    if state.recovery_attempts[signature] <= policy.MAX_RECOVERY_ATTEMPTS:
        llm_decision = await _llm_replan(
            task=task,
            state=state,
            agenda=agenda,
            failed_action=failed_action,
            result=result,
            context_bundle=context_bundle,
            context=context,
        )
        if llm_decision is not None:
            if llm_decision.kind == RecoveryDecisionKind.REPLACE_REMAINING_AGENDA:
                state.last_replan_reason = llm_decision.reason
            return llm_decision

    # 3. Escalate
    reason = result.human_escalation_reason or f"I need help after repeated failures while executing `{failed_action.name or failed_action.kind}`."
    return RecoveryDecision(
        kind=RecoveryDecisionKind.ESCALATE,
        reason=reason,
        task=task,
        outcome_kind=ResponseOutcomeKind.ESCALATE,
        escalation_diagnostics_ref=str((state.last_attempt or {}).get("attempt_id") or ""),
    )
