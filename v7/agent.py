from __future__ import annotations

from aethergraph import NodeContext, graph_fn

from .loop_engine import run_loop
from .memory_policy import build_context_bundle, maybe_distill_session_summary
from .planning import build_task, decide_context_mode, draft_plan
from .response_compose import compose_reply
from .types import load_state, save_state


@graph_fn(
    name="deeplens_agent_v7",
    inputs=["message", "attachments", "session_id", "user_meta"],
    outputs=["reply"],
    as_agent={
        "id": "deeplens_agent_v7",
        "title": "DeepLens Assistant v7",
        "short_description": "Policy-driven DeepLens agent for lens creation, analysis, export, and optimization workflows.",
        "description": (
            "A bounded DeepLens assistant that plans with an LLM, executes fixed DeepLens tools, "
            "and centralizes approvals and user interactions in policy-driven control flow."
        ),
        "icon_key": "microscope",
        "color": "teal",
        "mode": "chat_v1",
        "memory_level": "session",
    },
)
async def deeplens_agent(
    message: str,
    attachments: list[dict] | None = None,
    session_id: str | None = None,
    user_meta: dict | None = None,
    *,
    context: NodeContext,
) -> dict[str, str]:
    del session_id, user_meta

    raw_message = (message or "").strip()
    attachments = attachments or []
    if not raw_message and not attachments:
        reply = (
            "DeepLens Assistant ready.\n\n"
            "Ask me to create a lens, analyze a lens, optimize a lens, export artifacts, or check a run."
        )
        await context.channel("ui:session").send_text(reply)
        return {"reply": reply}

    state = await load_state(context=context)
    context_mode = decide_context_mode(raw_message, state)
    state.context_mode = context_mode.value

    await context.memory().record_chat_user(
        text=raw_message,
        tags=["ag.deeplens.v7.user"],
        data={"attachments_count": len(attachments)},
    )
    await maybe_distill_session_summary(context)

    task = build_task(raw_message, attachments, state)
    
    await context.emit_agent_event(event_type="task_built", summary="Task built successfully", payload=task.__dict__)
    
    context_bundle = await build_context_bundle(
        context_mode=context_mode,
        task=task,
        state=state,
        context=context,
    )
    plan = await draft_plan(
        task=task,
        state=state,
        context_bundle=context_bundle,
        context=context,
    )
    await context.emit_agent_event(event_type="plan_drafted", summary="Plan drafted successfully", payload=plan.__dict__)

    out = await run_loop(
        task=task,
        plan=plan,
        state=state,
        context=context,
    )
    
    reply = await compose_reply(
        task=task,
        plan=out["plan"],
        state=out["state"],
        tool_summaries=out["tool_summaries"],
        context=context,
    )
    out["state"].final_reply = reply
    await context.channel("ui:session").send_text(reply)
    await save_state(context=context, state=out["state"])
    return {"reply": reply}
