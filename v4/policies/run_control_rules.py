# policies/run_control_rules.py
from __future__ import annotations

from ._base import PolicyDecision


def apply_run_control_rules(
    *,
    message: str,
    active_run_id: str | None,
) -> list[PolicyDecision]:
    decisions: list[PolicyDecision] = []
    text = (message or "").lower()

    if any(w in text for w in ["cancel", "stop", "abort"]):
        decisions.append(
            PolicyDecision(
                matched=True,
                rule_name="run_control.cancel",
                priority=100,
                notes=["Detected run cancellation wording."],
                updates={
                    "workflow_family": "run_control",
                    "preferred_tool": "ag.cancel",
                    "run_request": {"run_id": active_run_id} if active_run_id else {},
                },
            )
        )
        return decisions

    if any(w in text for w in ["status", "progress", "running"]):
        decisions.append(
            PolicyDecision(
                matched=True,
                rule_name="run_control.status",
                priority=95,
                notes=["Detected run status wording."],
                updates={
                    "workflow_family": "run_control",
                    "preferred_tool": "ag.status",
                    "run_request": {"run_id": active_run_id} if active_run_id else {},
                },
            )
        )

    return decisions
