# policies/repair_rules.py
from __future__ import annotations

from ._base import PolicyDecision


def apply_repair_rules(
    *,
    workflow_family: str,
    tool_name: str | None,
    error_code: str | None,
    missing_fields: list[str] | None,
) -> list[PolicyDecision]:
    decisions: list[PolicyDecision] = []
    missing = list(missing_fields or [])

    if error_code in {"missing_source", "missing_input"} and "lens_source" in missing:
        decisions.append(
            PolicyDecision(
                matched=True,
                rule_name="repair.ask_for_lens_source",
                priority=100,
                notes=["Repair should ask the user for a lens source."],
                updates={
                    "repair_action": "ask_user",
                    "ask_user_message": "Please upload a lens file or select an active lens/source first.",
                },
            )
        )

    if workflow_family == "design" and error_code == "missing_design_spec":
        decisions.append(
            PolicyDecision(
                matched=True,
                rule_name="repair.ask_for_design_specs",
                priority=95,
                notes=["Repair should ask for core design specs."],
                updates={
                    "repair_action": "ask_user",
                    "ask_user_message": "Please provide FOV, F-number, and either focal length or image height.",
                },
            )
        )

    if workflow_family == "analysis" and tool_name == "dl.analysis" and "analysis_mode" in missing:
        decisions.append(
            PolicyDecision(
                matched=True,
                rule_name="repair.ask_for_analysis_mode",
                priority=90,
                notes=["Repair should ask for analysis mode."],
                updates={
                    "repair_action": "ask_user",
                    "ask_user_message": "Please specify the analysis type: MTF, PSF, spot, or ray trace.",
                },
            )
        )

    return decisions