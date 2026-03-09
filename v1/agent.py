from __future__ import annotations

from typing import Any 

from aethergraph import graph_fn, NodeContext 

from .executor import execute_task 
from .router import route_v2 
from .types import DeepLensState, load_state, save_state 


@graph_fn(
    name="deeplens_agent",
    inputs=["message", "attachments", "session_id", "user_meta"],
    outputs=["reply"],
    as_agent={
        "id": "deeplens_agent",
        "title": "DeepLens Assistant",
        "short_description": "Lens design, simulation, optimization, visualization, and debugging assistant.",
        "description": (
            "Hybrid DeepLens assistant with policy-driven execution. "
            "Supports explicit workflow-style slash commands and freeform loop-based agent behavior."
        ),
        "icon_key": "microscope",
        "color": "teal",
        "mode": "chat_v1",
        "memory_level": "session",
        "slash_commands": [
            {"name": "/lens", "description": "Route to lens-design workflow."},
            {"name": "/simulate", "description": "Route to simulation workflow."},
            {"name": "/optimize", "description": "Route to optimization workflow."},
            {"name": "/viz", "description": "Route to visualization workflow."},
            {"name": "/debug", "description": "Route to debugging workflow or toggle debug mode."},
            {"name": "/chat", "description": "Route to general DeepLens chat."},
            {"name": "/agent on", "description": "Enable freeform loop-capable mode."},
            {"name": "/agent off", "description": "Disable freeform loop-capable mode."},
            {"name": "/agent status", "description": "Show whether freeform agent mode is enabled."},
            {"name": "/mode full", "description": "Use broader context / higher-cost mode."},
            {"name": "/mode lite", "description": "Use selective structured context / lower-cost mode."},
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
                "Try `/lens`, `/simulate`, `/optimize`, `/viz`, `/debug`, or `/chat`.\n"
                "Use `/agent on|off|status` for freeform loop mode and `/mode full|lite` for context policy."
            )
        }
    
    mem = context.memory()
    chan = context.channel("ui:session")

    state: DeepLensState = await load_state(context=context, level="user")

    # Persist raw user turn 
    await mem.record_chat_user(
        text=raw_message,
        tags=["ag.deeplens.user"]
    )

    await chan.send_phase(
        phase="routing",
        status="active",
        label="Routing request",
        detail="Selecting DeepLens intent and execution mode.",
    )

    route_result = await route_v2(
        message=raw_message,
        attachments=attachments,
        state=state,
        context=context,
    )

    state = route_result["state"]
    decision = route_result["decision"]

    await chan.send_phase(
        phase="routing",
        status="done",
        label="Route selected",
        detail=(
            f"intent={decision['intent'].value}, "
            f"mode={decision['execution_mode'].value}, "
            f"context={decision['context_mode'].value}"
        ),
    )

    if route_result["immediate_reply"]:
        reply = route_result["immediate_reply"] or ""
    else:
        out = await execute_task(
            message=raw_message,
            attachments=attachments,
            session_id=session_id,
            user_meta=user_meta,
            decision=decision,
            state=state,
            context=context,
        )

        reply = out["reply"]
        state = out.get("state", state)

    state.last_intent = decision["intent"]

    await chan.send_text(reply)
    await save_state(context=context, state=state)

    await mem.record_chat_assistant(
        text=reply,
        tags=["ag.deeplens.reply", f"ag.deeplens.intent:{decision['intent'].value}"],
    )

    return {"reply": reply}