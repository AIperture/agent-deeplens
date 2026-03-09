from __future__ import annotations

from dataclasses import asdict
from typing import Any

from aethergraph import NodeContext, graph_fn

from .loop_engine import run_loop
from .memory_policy import build_context_bundle, maybe_distill_session_summary
from .router import route
from .types import TaskShape, load_state, save_state


def _roll_loop_history(state: Any) -> None:
    if not state.active_task and not state.loop_trace:
        return
    archived = {
        "task": state.active_task,
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


@graph_fn(
    name="deeplens_agent",
    inputs=["message", "attachments", "session_id", "user_meta"],
    outputs=["reply"],
    as_agent={
        "id": "deeplens_agent",
        "title": "DeepLens Assistant",
        "short_description": "Workflow-first DeepLens agent for design, analysis, and background optimization.",
        "description": (
            "A loop-based DeepLens assistant that analyzes uploaded lens files, "
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
        tags=["ag.deeplens.v3.user"],
        data={"attachments_count": len(attachments)},
    )

    route_result = await route(
        message=raw_message,
        attachments=attachments,
        state=state,
        context=context,
        user_meta=user_meta,
    )
    decision = route_result["decision"]
    state = route_result["state"]

    if route_result["immediate_reply"]:
        reply = route_result["immediate_reply"] or ""
    elif decision["task_shape"] == TaskShape.UNSUPPORTED:
        reply = (
            "This DeepLens agent currently supports lens analysis, starting-point lens design, "
            "background optimization submission, run status or cancellation, and bounded optics interpretation."
        )
    else:
        _roll_loop_history(state)
        state.context_mode = decision["context_mode"].value
        state.active_task = asdict(decision["task"])
        await save_state(context=context, state=state)
        await maybe_distill_session_summary(context)
        context_bundle = await build_context_bundle(
            context_mode=decision["context_mode"],
            task=decision["task"],
            state=state,
            context=context,
        )
        out = await run_loop(
            task=decision["task"],
            state=state,
            context_bundle=context_bundle,
            context_mode=decision["context_mode"],
            context=context,
        )
        reply = out["reply"]
        state = out.get("state", state)

    await chan.send_text(reply)
    await save_state(context=context, state=state)
    return {"reply": reply}
