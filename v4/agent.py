from __future__ import annotations

from dataclasses import asdict
from typing import Any

from aethergraph import NodeContext, graph_fn

from .context_policy import build_context_bundle
from .controller_select import select_controller
from .controllers.direct_controller import run_direct_controller
from .controllers.workflow_controller import run_workflow_controller
from .interpretation import interpret_turn
from .types import ContextPolicy, ControllerKind, DeepLensState, TaskShape, load_state, save_state


def _roll_loop_history(state: DeepLensState) -> None:
    active_task_like = state.active_task_frame or state.active_execution
    if not active_task_like and not state.loop_trace:
        return
    archived = {
        "task_frame": state.active_task_frame,
        "execution": state.active_execution,
        "trace_tail": state.loop_trace[-8:],
        "pending_runs_tail": state.pending_runs[-3:],
    }
    state.loop_history.append(archived)
    state.loop_history = state.loop_history[-6:]
    state.loop_trace = []
    state.retry_counters = {}
    state.pending_action = None
    state.pending_approval = None
    state.active_plan = None


def _mode_command_policy(command: str | None) -> ContextPolicy | None:
    if command == "/mode full":
        return ContextPolicy.FULL
    if command == "/mode lite":
        return ContextPolicy.TASK_LOCAL
    return None


async def _dispatch_controller(
    *,
    controller_kind: ControllerKind,
    interpretation: Any,
    state: DeepLensState,
    context_bundle: Any,
    context: NodeContext,
) -> dict[str, Any]:
    if controller_kind == ControllerKind.DIRECT:
        return await run_direct_controller(
            interpretation=interpretation,
            state=state,
            context_bundle=context_bundle,
            context=context,
        )
    return await run_workflow_controller(
        interpretation=interpretation,
        state=state,
        context_bundle=context_bundle,
        context=context,
    )


@graph_fn(
    name="deeplens_agent",
    inputs=["message", "attachments", "session_id", "user_meta"],
    outputs=["reply"],
    as_agent={
        "id": "deeplens_agent",
        "title": "DeepLens Assistant",
        "short_description": "Workflow-first DeepLens agent for design, analysis, and background optimization.",
        "description": (
            "A semantic-first DeepLens assistant that analyzes uploaded lens files, "
            "creates starting lens designs, exports outputs, and submits long-running "
            "optimization workflows through the AG runner."
        ),
        "icon_key": "microscope",
        "color": "teal",
        "mode": "chat_v1",
        "memory_level": "session",
        "slash_commands": [
            {"name": "/design", "description": "Route to lens design workflow."},
            {"name": "/analysis", "description": "Route to lens analysis workflow."},
            {"name": "/optimize", "description": "Submit optimization as a background workflow."},
            {"name": "/mode full", "description": "Use broader context / higher-cost mode."},
            {"name": "/mode lite", "description": "Use selective lower-cost mode."},
        ],
    },
)
async def deeplens_agent(
    message: str,
    attachments: list[dict[str, Any]] | None = None,
    session_id: str | None = None,
    user_meta: dict[str, Any] | None = None,
    *,
    context: NodeContext,
) -> dict[str, str]:
    del session_id
    raw_message = (message or "").strip()
    attachments = attachments or []

    if not raw_message and not attachments:
        return {
            "reply": (
                "DeepLens Assistant ready.\n\n"
                "Try `/analysis` with a `.json` or `.zmx` upload, `/design` with target specs, "
                "or `/optimize` to submit a background run."
            )
        }

    mem = context.memory()
    chan = context.channel("ui:session")
    state = await load_state(context=context, level="session")

    await mem.record_chat_user(
        text=raw_message,
        tags=["ag.deeplens.v4.user"],
        data={"attachments_count": len(attachments)},
    )

    interpretation = await interpret_turn(
        message=raw_message,
        attachments=attachments,
        state=state,
        user_meta=user_meta,
        context=context,
    )
    print("INTERPRETATION", interpretation)
    task_frame = interpretation.task_frame
    execution = interpretation.execution

    mode_override = _mode_command_policy(interpretation.envelope.explicit_command)
    if mode_override is not None:
        state.context_policy = mode_override.value

    if interpretation.immediate_reply:
        state.active_task_frame = asdict(task_frame)
        state.active_execution = asdict(execution) if execution else None
        reply = interpretation.immediate_reply
    elif task_frame.task_shape == TaskShape.UNSUPPORTED:
        state.active_task_frame = asdict(task_frame)
        state.active_execution = asdict(execution) if execution else None
        reply = (
            "This DeepLens agent currently supports lens analysis, starting-point lens design, "
            "background optimization submission, run status or cancellation, and bounded optics interpretation."
        )
    else:
        _roll_loop_history(state)
        state.active_task_frame = asdict(task_frame)
        state.active_execution = asdict(execution) if execution else None
        await save_state(context=context, state=state)
        context_bundle = await build_context_bundle(task=task_frame, state=state, context=context)
        controller_kind = select_controller(task_frame)
        out = await _dispatch_controller(
            controller_kind=controller_kind,
            interpretation=interpretation,
            state=state,
            context_bundle=context_bundle,
            context=context,
        )
        reply = out["reply"]
        state = out.get("state", state)

    await chan.send_text(reply)
    await save_state(context=context, state=state)
    return {"reply": reply}
