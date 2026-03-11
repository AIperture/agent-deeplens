from __future__ import annotations

from typing import Any

from ..types import RecoveryDecision, RecoveryDecisionKind, ResponseOutcomeKind
from .deterministic_repair import deterministic_recovery
from .llm_replanner import llm_replan_after_failure


def _failure_signature(failed_action: Any, result: Any) -> str:
    return "|".join(
        [
            str(failed_action.name or failed_action.kind),
            str(result.failure_kind.value if result.failure_kind is not None else "none"),
            str(result.error_code or "none"),
            str(result.summary or "")[:160],
        ]
    )


async def recover_failed_action(
    *,
    task: Any,
    state: Any,
    agenda: Any,
    failed_action: Any,
    result: Any,
    context_bundle: Any,
    context: Any,
) -> RecoveryDecision:
    signature = _failure_signature(failed_action, result)
    state.recovery_attempts[signature] = state.recovery_attempts.get(signature, 0) + 1
    state.active_recovery = {
        "signature": signature,
        "tool_name": failed_action.name,
        "attempt": state.recovery_attempts[signature],
        "failure_kind": result.failure_kind.value if result.failure_kind is not None else None,
    }

    deterministic = await deterministic_recovery(
        task=task,
        state=state,
        agenda=agenda,
        failed_action=failed_action,
        result=result,
        context_bundle=context_bundle,
        context=context,
    )
    if deterministic is not None:
        state.last_replan_reason = deterministic.reason if deterministic.kind == RecoveryDecisionKind.REPLACE_REMAINING_AGENDA else state.last_replan_reason
        return deterministic

    if state.recovery_attempts[signature] <= 2:
        llm_decision = await llm_replan_after_failure(
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

    reason = result.human_escalation_reason or f"I need human help after repeated failures while executing `{failed_action.name or failed_action.kind}`."
    return RecoveryDecision(
        kind=RecoveryDecisionKind.ESCALATE,
        reason=reason,
        task=task,
        outcome_kind=ResponseOutcomeKind.ESCALATE,
    )
