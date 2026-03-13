from __future__ import annotations

from .types import ResponseFrame, ResponseOutcomeKind


async def compose_reply(*, frame: ResponseFrame, context: object | None = None) -> str:
    del context
    if frame.outcome_kind == ResponseOutcomeKind.WAITING:
        return frame.reply
    if frame.outcome_kind == ResponseOutcomeKind.ESCALATE:
        return f"I need to pause here. {frame.reply}"
    return frame.reply
