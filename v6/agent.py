from __future__ import annotations

from dataclasses import asdict
from typing import Any

from aethergraph import NodeContext, graph_fn

from .context.memory_policy import build_context_bundle, maybe_distill_session_summary
from .loop_engine import run_loop
from .response_compose import compose_reply
from .response_frame import build_response_frame
from .router import route
from .types import ActionAgenda, DeepLensTask, ResponseOutcomeKind, TaskShape, TERMINAL_TASK_STATUSES, load_state, save_state


def _clear_task_runtime_state(state: Any) -> None:
    state.pending_action = None
    state.pending_approval = None
    state.active_agenda = None
    state.active_intent = None
    state.active_recovery = None
    state.last_replan_reason = None
    state.runtime_missing_fields = []
    state.runtime_invalid_fields = {}
    state.last_prompt_reason = None


def _archive_current_task(state: Any) -> None:
    if not state.active_task and not state.loop_trace:
        return
    archived = {
        "task": state.active_task,
        "trace_tail": state.loop_trace[-8:],
        "pending_runs_tail": state.pending_runs[-3:],
        "agenda": state.active_agenda,
    }
    state.loop_history.append(archived)
    state.loop_history = state.loop_history[-6:]
    state.loop_trace = []
    state.retry_counters = {}
    state.recovery_attempts = {}
    _clear_task_runtime_state(state)


def _task_from_state(state: Any) -> DeepLensTask | None:
    return DeepLensTask.from_dict(state.active_task if isinstance(state.active_task, dict) else None)


def _prepare_task_transition(state: Any, turn_role: str) -> None:
    prior_task = _task_from_state(state)
    if turn_role != "new_task" or prior_task is None:
        return
    if prior_task.task_status not in TERMINAL_TASK_STATUSES:
        prior_task.task_status = "superseded"
        state.active_task = asdict(prior_task)
    _archive_current_task(state)


def _finalize_task_lifecycle(state: Any, out: dict[str, Any]) -> None:
    task = _task_from_state(state)
    if task is None:
        return

    agenda = ActionAgenda.from_dict(state.active_agenda)
    outcome = out.get("outcome_kind", ResponseOutcomeKind.COMPLETE)

    if outcome == ResponseOutcomeKind.WAITING:
        task.task_status = "waiting"
        state.active_task = asdict(task)
        return

    if outcome == ResponseOutcomeKind.COMPLETE and out.get("reply") == "Okay, I stopped before making changes.":
        task.task_status = "canceled"
    elif outcome == ResponseOutcomeKind.COMPLETE and agenda is not None and agenda.status.value == "waiting":
        task.task_status = "waiting"
        state.active_task = asdict(task)
        return
    elif outcome == ResponseOutcomeKind.COMPLETE:
        task.task_status = "completed"
    elif outcome in {ResponseOutcomeKind.ESCALATE, ResponseOutcomeKind.FAILED}:
        task.task_status = "failed"

    state.active_task = asdict(task)
    _archive_current_task(state)
    if task.task_status in TERMINAL_TASK_STATUSES:
        state.active_task = None


@graph_fn(
    name="deeplens_agent_v6",
    inputs=["message", "attachments", "session_id", "user_meta"],
    outputs=["reply"],
    as_agent={
        "id": "deeplens_agent_v6",
        "title": "DeepLens Assistant v6",
        "short_description": "Agenda-driven DeepLens agent for design, analysis, export, and optimization workflows.",
        "description": (
            "A bounded DeepLens assistant that composes multi-tool agendas for design, "
            "analysis, export, and background optimization workflows."
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
        tags=["ag.deeplens.v6.user"],
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
    turn_role = route_result.get("turn_role", "new_task")

    if route_result["immediate_reply"]:
        reply = route_result["immediate_reply"] or ""
    elif decision["task_shape"] == TaskShape.UNSUPPORTED:
        reply = "This DeepLens agent currently supports lens design, analysis, export, optimization submission, and run status or cancellation."
    else:
        _prepare_task_transition(state, turn_role)
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
        state = out.get("state", state)
        _finalize_task_lifecycle(state, out)
        agenda = ActionAgenda.from_dict(state.active_agenda)
        frame = build_response_frame(
            reply=out["reply"],
            outcome_kind=out.get("outcome_kind", ResponseOutcomeKind.COMPLETE),
            state=state,
            agenda=agenda,
            last_tool_result=out.get("last_tool_result"),
            approval_prompt=out.get("approval_prompt"),
        )
        reply = await compose_reply(frame=frame, context=context)

    await chan.send_text(reply)
    await save_state(context=context, state=state)
    return {"reply": reply}
