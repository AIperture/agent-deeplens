from __future__ import annotations

from typing import Any
from aethergraph import NodeContext, graph_fn


from . import policy
from .context_builder import build_context_bundle
from .loop import run_loop
from .response_compose import compose_reply
from .response_frame import build_response_frame
from .router import route
from .types import PlanAgenda, ResponseOutcomeKind, TERMINAL_TASK_STATUSES, TaskShape, TemplateState, TemplateTask, load_state, save_state


def _clear_task_runtime_state(state: TemplateState) -> None:
    state.pending_action = None
    state.pending_approval = None
    state.waiting_prompt = None
    state.active_agenda = None
    state.runtime_missing_fields = []
    state.runtime_invalid_fields = {}
    state.active_recovery = None


def _archive_current_task(state: TemplateState) -> None:
    if not state.active_task and not state.loop_trace:
        return
    state.loop_history.append({"task": state.active_task, "trace_tail": state.loop_trace[-8:], "agenda": state.active_agenda})
    state.loop_history = state.loop_history[-policy.LOOP_HISTORY_LIMIT:]
    state.loop_trace = []
    _clear_task_runtime_state(state)


def _task_from_state(state: TemplateState) -> TemplateTask | None:
    return TemplateTask.from_dict(state.active_task if isinstance(state.active_task, dict) else None)


def _prepare_task_transition(state: TemplateState, turn_role: str) -> None:
    prior_task = _task_from_state(state)
    if turn_role != "new_task" or prior_task is None:
        return
    if prior_task.task_status not in TERMINAL_TASK_STATUSES:
        prior_task.task_status = "superseded"
        state.active_task = prior_task.to_dict()
    _archive_current_task(state)


def _finalize_task_lifecycle(state: TemplateState, out: dict[str, Any]) -> None:
    task = _task_from_state(state)
    if task is None:
        return
    agenda = PlanAgenda.from_dict(state.active_agenda)
    outcome = out.get("outcome_kind", ResponseOutcomeKind.COMPLETE)
    if outcome == ResponseOutcomeKind.WAITING:
        task.task_status = "waiting"
        state.active_task = task.to_dict()
        return
    if outcome == ResponseOutcomeKind.COMPLETE and agenda is not None and agenda.status.value == "waiting":
        task.task_status = "waiting"
        state.active_task = task.to_dict()
        return
    task.task_status = "completed" if outcome == ResponseOutcomeKind.COMPLETE else "failed"
    state.active_task = task.to_dict()
    _archive_current_task(state)
    if task.task_status in TERMINAL_TASK_STATUSES:
        state.active_task = None


@graph_fn(
    name="deeplens_template_agent",
    inputs=["message", "attachments", "session_id", "user_meta"],
    outputs=["reply"],
    as_agent={
        "id": "deeplens_template_agent",
        "title": policy.AGENT_TITLE,
        "short_description": policy.AGENT_SHORT_DESCRIPTION,
        "description": policy.AGENT_DESCRIPTION,
        "icon_key": "microscope",
        "color": "amber",
        "mode": "chat_v1",
        "memory_level": "session",
        "slash_commands": [
            {"name": "/mode lite", "description": "Use lighter context mode."},
            {"name": "/mode full", "description": "Use fuller context mode."},
            {"name": "/debug plan", "description": "Show the active agenda."},
            {"name": "/debug state", "description": "Show the active state."},
        ],
    },
)
async def template_agent(
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
        return {"reply": policy.WELCOME_MESSAGE}

    state = await load_state(context, state_key=policy.STATE_KEY, level=policy.SESSION_MEMORY_LEVEL)
    await context.memory().record_chat_user(
        text=raw_message,
        tags=[f"ag.{policy.AGENT_ID}.user"],
        data={"attachments_count": len(attachments)},
    )
    route_result = await route(message=raw_message, attachments=attachments, state=state, context=context, user_meta=user_meta)
    decision = route_result["decision"]
    state = route_result["state"]
    turn_role = route_result.get("turn_role", "new_task")

    if route_result["immediate_reply"]:
        reply = route_result["immediate_reply"] or ""
    elif decision["task_shape"] == TaskShape.UNSUPPORTED:
        reply = policy.UNSUPPORTED_MESSAGE
    else:
        _prepare_task_transition(state, turn_role)
        state.context_mode = decision["context_mode"].value
        state.active_task = decision["task"].to_dict()
        if state.pending_approval and raw_message.lower() in {"approve", "yes", "approved"}:
            state.approval_tokens[state.pending_approval["action_id"]] = "approved"
            state.pending_action = None
            state.pending_approval = None
        elif state.pending_action == "ask_user":
            task = TemplateTask.from_dict(state.active_task) or decision["task"]
            task.parsed_args.update(decision["task"].parsed_args)
            state.active_task = task.to_dict()
            state.pending_action = None
            state.waiting_prompt = None
        await save_state(context, state=state, state_key=policy.STATE_KEY, agent_id=policy.AGENT_ID)
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
        state = out.get("state", state)
        _finalize_task_lifecycle(state, out)
        agenda = PlanAgenda.from_dict(state.active_agenda)
        frame = build_response_frame(
            reply=out["reply"],
            outcome_kind=out.get("outcome_kind", ResponseOutcomeKind.COMPLETE),
            state=state,
            agenda=agenda,
            last_tool_result=out.get("last_tool_result"),
            approval_prompt=out.get("approval_prompt"),
        )
        reply = await compose_reply(frame=frame, context=context)

    await context.channel("ui:session").send_text(reply)
    await save_state(context, state=state, state_key=policy.STATE_KEY, agent_id=policy.AGENT_ID)
    return {"reply": reply}
