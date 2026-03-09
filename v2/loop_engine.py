from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from .tool_dispatch import dispatch_tool_action
from .tool_registry import get_tool_spec
from .types import LoopAction, TaskShape, DEBUG, save_state
from .memory_policy import build_context_bundle


def _append_trace(state: Any, item: dict[str, Any]) -> None:
    state.loop_trace.append(item)
    state.loop_trace = state.loop_trace[-50:]


def _increment_retry(state: Any, key: str) -> int:
    state.retry_counters[key] = state.retry_counters.get(key, 0) + 1
    return state.retry_counters[key]


def _reset_retry(state: Any, key: str) -> None:
    state.retry_counters.pop(key, None)

def _sync_active_task(state: Any, task: Any) -> None:
    state.active_task = asdict(task)

async def refresh_loop_context(task, state, context, context_mode):
    _sync_active_task(state, task)
    await save_state(context=context, state=state)
    return await build_context_bundle(
        context_mode=context_mode,
        task=task,
        state=state,
        context=context,
    )

async def propose_next_action(
    *,
    task: Any,
    state: Any,
    context_bundle: Any,
    step_index: int,
    context: Any,
) -> LoopAction:
    llm = context.llm()
    skills = context.skills()
    domain_section = {
        "chat": "deeplens.domain_chat",
        "simulation": "deeplens.domain_simulation",
        "optimization": "deeplens.domain_optimization",
        "debug": "deeplens.domain_debug",
        "unknown": "deeplens.refusal",
    }.get(task.domain_hint.value, "deeplens.refusal")
    system_prompt = skills.compile_prompt(
        "aethergraph-agent-deeplens",
        "deeplens.system",
        "deeplens.router",
        "deeplens.loop",
        domain_section,
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )

    user_payload = {
        "task_shape": task.task_shape.value,
        "domain_hint": task.domain_hint.value,
        "preferred_tool": task.preferred_tool,
        "user_goal": task.user_goal,
        "working_state": context_bundle.working_state,
        "recent_messages": context_bundle.recent_messages,
        "long_term_summary": context_bundle.long_term_summary,
        "prompt_segments": context_bundle.prompt_segments,
        "current_loop_trace_tail": state.loop_trace[-8:],
        "previous_loop_summaries": state.loop_history[-3:],
        "pending_runs": state.pending_runs[-5:],
        "notes": [
            "Choose exactly one next action.",
            "For direct_answer, prefer respond or finish.",
            "For single_action, prefer one tool_call if the tool is obvious.",
            "For multi_step, ask for missing info or approval before expensive work.",
            "For run_control, prefer status or cancel related tool calls only.",
            "If a long-running graph is submitted, usually end the turn.",
            "If unsupported, prefer fail with a concise refusal.",
        ],
    }

    action_schema = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["ask_user", "request_approval", "tool_call", "respond", "finish", "fail"],
            },
            "name": {"type": ["string", "null"]},
            "args": {"type": "object"},
            "rationale": {"type": "string"},
        },
        "required": ["kind", "args", "rationale"],
        "additionalProperties": False,
    }
    resp, _usage = await llm.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        output_format="json",
        json_schema=action_schema,
        schema_name="DeepLensLoopAction",
        strict_schema=True,
        validate_json=True,
        max_output_tokens=420,
    )
    obj = json.loads(resp) if isinstance(resp, str) else resp
    return obj


def validate_action(*, action: LoopAction) -> tuple[bool, str | None]:
    kind = action.get("kind")
    name = action.get("name")
    if kind == "tool_call":
        if not name:
            return False, "tool_call missing action name"
        try:
            get_tool_spec(name)
        except KeyError:
            return False, f"unknown tool `{name}`"
    return True, None


def _max_steps(task_shape: TaskShape) -> int:
    return {
        TaskShape.DIRECT_ANSWER: 1,
        TaskShape.SINGLE_ACTION: 3,
        TaskShape.MULTI_STEP: 6,
        TaskShape.RUN_CONTROL: 3,
        TaskShape.UNSUPPORTED: 1,
    }[task_shape]


async def run_loop(
    *,
    task: Any,
    state: Any,
    context_bundle: Any,
    context_mode: Any,
    context: Any,
) -> dict[str, Any]:
    chan = context.channel("ui:session")
    mem = context.memory()
    await chan.send_phase(
        phase="execution",
        status="active",
        label="Running loop",
        detail=f"shape={task.task_shape.value}",
    )
    context_bundle = await refresh_loop_context(
        task=task,
        state=state,
        context=context,
        context_mode=context_mode,
    )
    max_steps = _max_steps(task.task_shape)
    for step_index in range(max_steps):
        await chan.send_phase(
            phase="execution.step",
            status="active",
            label=f"Loop step {step_index + 1}",
            detail="Choosing the next bounded action.",
        )
        action = await propose_next_action(
            task=task,
            state=state,
            context_bundle=context_bundle,
            step_index=step_index,
            context=context,
        )

        ok, err = validate_action(action=action)
        if not ok:
            _append_trace(state, {"step_index": step_index, "action": action, "error": err})
            context_bundle = await refresh_loop_context(
                task=task,
                state=state,
                context=context,
                context_mode=context_mode,
            )
            retries = _increment_retry(state, "validation")
            if retries > 2:
                return {
                    "reply": f"I stopped because loop validation failed repeatedly: {err}",
                    "state": state,
                }
            continue
        _reset_retry(state, "validation")
        _append_trace(state, {"step_index": step_index, "action": action})
        kind = action["kind"]
        if kind == "ask_user":
            prompt = action["args"].get("prompt") or "Please provide the missing information."
            state.pending_action = "ask_user"
            state.pending_approval = None
            context_bundle = await refresh_loop_context(
                task=task,
                state=state,
                context=context,
                context_mode=context_mode,
            )
            answer = await chan.ask_text(prompt=prompt)
            await mem.record_chat_user(
                text=answer,
                tags=["ag.deeplens.v2.user", "ag.deeplens.v2.loop_answer"],
                data={"prompt": prompt, "step_index": step_index},
            )
            task.notes.append(f"user_answer:{answer}")
            state.pending_action = None
            _sync_active_task(state, task)
            context_bundle = await refresh_loop_context(
                task=task,
                state=state,
                context=context,
                context_mode=context_mode,
            )
            continue
        if kind == "request_approval":
            prompt = action["args"].get("prompt") or "Approve this action?"
            state.pending_action = "request_approval"
            state.pending_approval = {
                "prompt": prompt,
                "step_index": step_index,
                "action": action.get("name"),
            }
            context_bundle = await refresh_loop_context(
                task=task,
                state=state,
                context=context,
                context_mode=context_mode,
            )
            resp = await chan.ask_approval(prompt=prompt, options=["Approve", "Reject"])
            approved = bool(resp.get("approved"))
            task.notes.append(f"approval:{'approved' if approved else 'rejected'}:{prompt}")
            state.pending_action = None
            state.pending_approval = None
            context_bundle = await refresh_loop_context(
                task=task,
                state=state,
                context=context,
                context_mode=context_mode,
            )
            if not approved:
                return {"reply": "Okay, I stopped before making changes.", "state": state}
            continue
        if kind == "tool_call":
            state.pending_action = action.get("name")
            state.pending_approval = None
            context_bundle = await refresh_loop_context(
                task=task,
                state=state,
                context=context,
                context_mode=context_mode,
            )
            result = await dispatch_tool_action(
                action=action,
                task=task,
                state=state,
                context_bundle=context_bundle,
                context=context,
            )
            _append_trace(
                state,
                {
                    "step_index": step_index,
                    "tool_name": action.get("name"),
                    "summary": result.summary,
                    "ok": result.ok,
                    "status": result.status,
                },
            )
            state.pending_action = None
            task.notes.append(
                f"tool_result:{action.get('name')}:{'ok' if result.ok else 'error'}:{result.status}"
            )
            context_bundle = await refresh_loop_context(
                task=task,
                state=state,
                context=context,
                context_mode=context_mode,
            )
            if not result.ok:
                retries = _increment_retry(state, action.get("name") or "tool")
                if retries > 2:
                    return {
                        "reply": f"I stopped after repeated failures while executing `{action.get('name')}`.",
                        "state": state,
                    }
                continue
            _reset_retry(state, action.get("name") or "tool")
            if result.should_end_turn:
                return {"reply": result.summary, "state": state}
            continue
        if kind == "respond":
            return {"reply": action["args"].get("text") or "Done.", "state": state}
        if kind == "finish":
            return {"reply": "Done.", "state": state}
        if kind == "fail":
            return {
                "reply": action["args"].get("text")
                or "This DeepLens agent does not support that request yet.",
                "state": state,
            }
    return {"reply": "I stopped after the current step budget.", "state": state}
