from __future__ import annotations

from .types import ConversationState, LoopOutcome, LoopOutcomeKind


# ---------------------------------------------------------------------
# What to implement here
# ---------------------------------------------------------------------
# - Deterministic user-facing summaries
# - Waiting / complete / escalate / failed messaging
# - Optional assumption disclosure later
#
# What should NOT be included here
# - Execution control
# - Repair logic
# - Task switching
# ---------------------------------------------------------------------


def compose_reply(*, outcome: LoopOutcome, state: ConversationState) -> str:
    del state

    if outcome.kind == LoopOutcomeKind.COMPLETE:
        return outcome.reason

    if outcome.kind == LoopOutcomeKind.WAITING:
        if outcome.pending_interaction is not None:
            return outcome.pending_interaction.prompt
        return outcome.reason

    if outcome.kind == LoopOutcomeKind.ESCALATE:
        return (
            "I need to re-evaluate the conversation before continuing. "
            f"{outcome.reason}"
        )

    return outcome.reason