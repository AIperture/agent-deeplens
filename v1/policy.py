from __future__ import annotations

from dataclasses import dataclass, field

from .types import DeepLensIntent


@dataclass
class BranchPolicy:
    intent: DeepLensIntent
    allow_direct_response: bool = True
    default_workflow_enabled: bool = False
    default_loop_enabled: bool = True

    max_steps: int = 8
    repair_budget: int = 2

    require_approval_for_execute: bool = False
    require_approval_for_write: bool = False

    required_fields: list[str] = field(default_factory=list)
    allowed_actions: list[str] = field(default_factory=list)
    success_checks: list[str] = field(default_factory=list)


POLICIES: dict[DeepLensIntent, BranchPolicy] = {
    DeepLensIntent.CHAT: BranchPolicy(
        intent=DeepLensIntent.CHAT,
        allow_direct_response=True,
        default_workflow_enabled=False,
        default_loop_enabled=False,
        max_steps=2,
        allowed_actions=["respond", "ask_user"],
    ),
    DeepLensIntent.LENS_DESIGN: BranchPolicy(
        intent=DeepLensIntent.LENS_DESIGN,
        allow_direct_response=False,
        default_workflow_enabled=True,
        default_loop_enabled=True,
        max_steps=10,
        repair_budget=2,
        require_approval_for_execute=True,
        require_approval_for_write=True,
        required_fields=["fov", "fnum"],
        allowed_actions=[
            "clarify_specs",
            "read_attachment",
            "propose_initial_design",
            "generate_lens_config",
            "run_quick_analysis",
            "revise_design",
            "save_artifact",
        ],
        success_checks=[
            "specs_complete",
            "design_generated",
            "analysis_completed",
        ],
    ),
    DeepLensIntent.SIMULATION: BranchPolicy(
        intent=DeepLensIntent.SIMULATION,
        allow_direct_response=False,
        default_workflow_enabled=True,
        default_loop_enabled=True,
        max_steps=6,
        require_approval_for_execute=False,
        require_approval_for_write=False,
        allowed_actions=[
            "read_attachment",
            "run_analysis",
            "run_targeted_eval",
            "summarize_metrics",
            "save_artifact",
        ],
        success_checks=[
            "simulation_completed",
        ],
    ),
    DeepLensIntent.OPTIMIZATION: BranchPolicy(
        intent=DeepLensIntent.OPTIMIZATION,
        allow_direct_response=False,
        default_workflow_enabled=True,
        default_loop_enabled=True,
        max_steps=10,
        repair_budget=3,
        require_approval_for_execute=True,
        require_approval_for_write=True,
        allowed_actions=[
            "read_attachment",
            "clarify_target",
            "propose_optimization_plan",
            "run_optimization_stage",
            "evaluate_progress",
            "revise_optimizer",
            "save_artifact",
        ],
        success_checks=[
            "objective_defined",
            "optimization_ran",
        ],
    ),
    DeepLensIntent.VISUALIZATION: BranchPolicy(
        intent=DeepLensIntent.VISUALIZATION,
        allow_direct_response=False,
        default_workflow_enabled=True,
        default_loop_enabled=True,
        max_steps=4,
        require_approval_for_execute=False,
        require_approval_for_write=False,
        allowed_actions=[
            "read_attachment",
            "render_plot",
            "render_report",
            "save_artifact",
        ],
        success_checks=["visualization_completed"],
    ),
    DeepLensIntent.DEBUG: BranchPolicy(
        intent=DeepLensIntent.DEBUG,
        allow_direct_response=False,
        default_workflow_enabled=False,
        default_loop_enabled=True,
        max_steps=8,
        repair_budget=3,
        require_approval_for_execute=True,
        require_approval_for_write=False,
        allowed_actions=[
            "read_attachment",
            "inspect_failure",
            "run_diagnostic",
            "propose_fix",
            "apply_fix_patch",
            "retest",
        ],
        success_checks=[
            "root_cause_identified",
            "fix_proposed_or_validated",
        ],
    ),
}


def get_policy(intent: DeepLensIntent) -> BranchPolicy:
    return POLICIES[intent]