# policies/approval_rules.py
from __future__ import annotations

from ._base import PolicyDecision


def apply_approval_rules(
    *,
    workflow_family: str,
    preferred_tool: str | None,
    execution_args: dict,
) -> list[PolicyDecision]:
    decisions: list[PolicyDecision] = []

    if workflow_family == "optimization":
        decisions.append(
            PolicyDecision(
                matched=True,
                rule_name="approval.optimization_soft",
                priority=50,
                notes=["Optimization may be expensive; soft approval recommended."],
                updates={
                    "approval_required": "soft",
                },
            )
        )

    if preferred_tool == "dl.export_lens" and execution_args.get("overwrite") is True:
        decisions.append(
            PolicyDecision(
                matched=True,
                rule_name="approval.export_overwrite_hard",
                priority=90,
                notes=["Overwriting exported output should require hard approval."],
                updates={
                    "approval_required": "hard",
                },
            )
        )

    return decisions
