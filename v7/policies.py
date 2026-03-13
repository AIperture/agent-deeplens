from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UiPolicy:
    running_phase: str | None = None
    success_phase: str | None = None
    failure_text: str | None = None


@dataclass
class ToolPolicy:
    policy_id: str
    requires_approval: bool = False
    approval_prompt: str | None = None
    ui: UiPolicy = field(default_factory=UiPolicy)


TOOL_POLICIES: dict[str, ToolPolicy] = {
    "default_tool": ToolPolicy(
        policy_id="default_tool",
        ui=UiPolicy(
            running_phase="tool",
            success_phase="tool",
            failure_text="The tool failed before completing the step.",
        ),
    ),
    "approval_required": ToolPolicy(
        policy_id="approval_required",
        requires_approval=True,
        approval_prompt="I’m ready to submit the background optimization run. Approve?",
        ui=UiPolicy(
            running_phase="tool",
            success_phase="tool",
            failure_text="The approved tool failed before completing the step.",
        ),
    ),
    "soft_approval": ToolPolicy(
        policy_id="soft_approval",
        requires_approval=True,
        approval_prompt="I’m ready to request cancellation for the active run. Approve?",
        ui=UiPolicy(
            running_phase="tool",
            success_phase="tool",
            failure_text="The cancellation request failed.",
        ),
    ),
}


def get_tool_policy(policy_id: str) -> ToolPolicy:
    if policy_id not in TOOL_POLICIES:
        return TOOL_POLICIES["default_tool"]
    return TOOL_POLICIES[policy_id]
