# policies/state_sensitive_rules.py
from __future__ import annotations

from ._base import PolicyDecision


def apply_state_sensitive_rules(
    *,
    message: str,
    state: object,
) -> list[PolicyDecision]:
    decisions: list[PolicyDecision] = []
    text = (message or "").lower()

    active_run_id = getattr(state, "active_run_id", None)
    active_source_ref = getattr(state, "active_source_ref", {}) or {}

    if active_run_id and any(w in text for w in ["status", "progress", "running"]):
        decisions.append(
            PolicyDecision(
                matched=True,
                rule_name="state.active_run_status",
                priority=100,
                notes=["Active run in state biases request toward run status."],
                updates={
                    "workflow_family": "run_control",
                    "preferred_tool": "ag.status",
                    "run_request": {"run_id": active_run_id},
                },
            )
        )

    if active_run_id and any(w in text for w in ["cancel", "stop", "abort"]):
        decisions.append(
            PolicyDecision(
                matched=True,
                rule_name="state.active_run_cancel",
                priority=100,
                notes=["Active run in state biases request toward run cancel."],
                updates={
                    "workflow_family": "run_control",
                    "preferred_tool": "ag.cancel",
                    "run_request": {"run_id": active_run_id},
                },
            )
        )

    if active_source_ref and any(w in text for w in ["analy", "mtf", "spot", "psf", "ray trace"]):
        decisions.append(
            PolicyDecision(
                matched=True,
                rule_name="state.active_source_analysis",
                priority=70,
                notes=["Active source ref biases request toward analysis."],
                updates={
                    "workflow_family": "analysis",
                    "preferred_tool": "dl.analysis",
                    "analysis_request": {"source_ref": active_source_ref},
                },
            )
        )

    if active_source_ref and any(w in text for w in ["optimize", "improve"]):
        decisions.append(
            PolicyDecision(
                matched=True,
                rule_name="state.active_source_optimization",
                priority=65,
                notes=["Active source ref biases request toward optimization."],
                updates={
                    "workflow_family": "optimization",
                    "preferred_tool": "ag.spawn_graph",
                    "task_payload": {"lens_source": active_source_ref},
                },
            )
        )

    return decisions
