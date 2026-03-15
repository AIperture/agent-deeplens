# policies/post_tool_transition_rules.py
from __future__ import annotations

from ._base import PolicyDecision


def apply_post_tool_transition_rules(
    *,
    tool_name: str | None,
    outcome_type: str | None,
    result_data: dict,
    artifacts: list[dict],
    run_id: str | None,
) -> list[PolicyDecision]:
    decisions: list[PolicyDecision] = []

    if tool_name == "ag.spawn_graph" and outcome_type == "run_submitted":
        decisions.append(
            PolicyDecision(
                matched=True,
                rule_name="post_tool.optimize_to_run_control",
                priority=100,
                notes=["Optimization submission transitions into run-control context."],
                updates={
                    "workflow_family": "run_control",
                    "active_run_id": run_id,
                },
            )
        )

    if tool_name == "dl.analysis" and outcome_type == "success":
        decisions.append(
            PolicyDecision(
                matched=True,
                rule_name="post_tool.analysis_success",
                priority=70,
                notes=["Successful analysis suggests export or explanation next."],
                updates={
                    "recommended_next_actions": ["explain_result", "export_result"],
                },
            )
        )

    if artifacts:
        decisions.append(
            PolicyDecision(
                matched=True,
                rule_name="post_tool.artifacts_created",
                priority=40,
                notes=["Artifacts were created by the tool."],
                updates={
                    "artifacts_created": artifacts,
                },
            )
        )

    return decisions
