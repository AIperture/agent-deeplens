from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..response_compose import ResponseBundle, compose_reply
from ..types import DeepLensState, InterpretationResult


async def run_direct_controller(
    *,
    interpretation: InterpretationResult,
    state: DeepLensState,
    context_bundle: Any,
    context: Any,
) -> dict[str, Any]:
    del context_bundle
    del context
    state.active_task_frame = asdict(interpretation.task_frame)
    state.active_execution = asdict(interpretation.execution) if interpretation.execution else None
    reply = interpretation.immediate_reply or compose_reply(
        ResponseBundle(
            task_frame=interpretation.task_frame,
            execution=interpretation.execution,
            tool_result=None,
            repair_result=None,
            state=state,
        )
    )
    return {
        "reply": reply,
        "state": state,
        "task_frame": interpretation.task_frame,
        "execution": interpretation.execution,
        "tool_result": None,
        "repair_result": None,
    }
