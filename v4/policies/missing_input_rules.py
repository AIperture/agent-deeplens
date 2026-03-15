# policies/missing_input_rules.py
from __future__ import annotations

from ._base import PolicyDecision


def apply_missing_input_rules(
    *,
    workflow_family: str,
    task_payload: dict,
    design_spec: dict,
    analysis_request: dict,
    delivery_request: dict,
    missing_fields: list[str] | None = None,
) -> list[PolicyDecision]:
    decisions: list[PolicyDecision] = []
    missing = list(missing_fields or [])

    if workflow_family == "design":
        if "fov" not in design_spec and "fov" not in missing:
            missing.append("fov")
        if "fnum" not in design_spec and "fnum" not in missing:
            missing.append("fnum")
        if "foclen" not in design_spec and "imgh" not in design_spec and "foclen_or_imgh" not in missing:
            missing.append("foclen_or_imgh")

        if missing:
            decisions.append(
                PolicyDecision(
                    matched=True,
                    rule_name="missing.design_core_specs",
                    priority=100,
                    notes=["Design workflow requires core specs."],
                    updates={"missing_fields": missing},
                )
            )

    if workflow_family in {"analysis", "optimization", "export"}:
        has_source = bool(
            analysis_request.get("source_ref")
            or analysis_request.get("attachment")
            or delivery_request.get("source_ref")
            or task_payload.get("source_ref")
        )
        if not has_source and "lens_source" not in missing:
            missing.append("lens_source")

        if missing:
            decisions.append(
                PolicyDecision(
                    matched=True,
                    rule_name="missing.lens_source",
                    priority=100,
                    notes=["Workflow requires a lens source."],
                    updates={"missing_fields": missing},
                )
            )

    return decisions