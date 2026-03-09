from __future__ import annotations

import json
from typing import Any

from aethergraph.core.runtime.node_context import NodeContext

from .types import LoopAction
from .tool_dispatch import dispatch_tool_action


def _append_trace(state: Any, item: dict[str, Any]) -> None:
    state.loop_trace.append(item)
    state.loop_trace = state.loop_trace[-50:]


def _increment_retry(state: Any, key: str) -> int:
    state.retry_counters[key] = state.retry_counters.get(key, 0) + 1
    return state.retry_counters[key]

def _reset_retry(state: Any, key: str) -> None:
    state.retry_counters.pop(key, None)


async def propose_next_action(
    *,
    task: Any,
    policy: Any,
    state: Any,
    context_bundle: Any,
    step_index: int,
    context: Any,
) -> LoopAction:
    llm = context.llm()
    skills = context.skills()

    intent_skill_key = {
        "chat": "deeplens.chat",
        "lens_design": "deeplens.design",
        "simulation": "deeplens.simulation",
        "optimization": "deeplens.optimization",
        "visualization": "deeplens.visualization",
        "debug": "deeplens.debug",
    }[task.intent.value]

    system_prompt = skills.compile_prompt(
        "aethergraph-agent-deeplens",
        "deeplens.system",
        "deeplens.execution_policy",
        "deeplens.loop",
        intent_skill_key,
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )

    user_payload = {
        "intent": task.intent.value,
        "user_goal": task.user_goal,
        "step_index": step_index,
        "policy": {
            "allowed_actions": policy.allowed_actions,
            "required_fields": policy.required_fields,
            "success_checks": policy.success_checks,
            "max_steps": policy.max_steps,
            "repair_budget": policy.repair_budget,
            "require_approval_for_execute": policy.require_approval_for_execute,
            "require_approval_for_write": policy.require_approval_for_write,
        },
        "working_state": context_bundle.working_state,
        "recent_messages": context_bundle.recent_messages,
        "long_term_summary": context_bundle.long_term_summary,
        "loop_trace_tail": state.loop_trace[-8:],
        "pending_runs": state.pending_runs[-5:],
        "notes": [
            "Choose exactly one next action.",
            "Do not choose a tool outside allowed_actions.",
            "If specs are missing, prefer ask_user.",
            "If a long-running task should be started, prefer request_approval or tool_call that submits background work.",
            "If a background run is submitted, usually end the turn rather than planning as if results already exist.",
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
        max_output_tokens=512,
    )

    obj = json.loads(resp) if isinstance(resp, str) else resp
    return obj


def validate_action(*, action: LoopAction, policy: Any) -> tuple[bool, str | None]:
    kind = action.get("kind")
    name = action.get("name")

    if kind == "tool_call":
        if not name:
            return False, "tool_call missing action name"
        if name not in policy.allowed_actions:
            return False, f"action `{name}` is not allowed for intent `{policy.intent.value}`"

    return True, None



async def execute_tool_action(
    *,
    action: LoopAction,
    task: Any,
    state: Any,
    context_bundle: Any,
    context: Any,
) -> dict[str, Any]:
    # Placeholder adapter layer.
    # Later map action["name"] to actual DeepLens branch functions / tools.
    name = action.get("name") or "unknown_action"

    # This is where you could:
    # - call rigid workflows as tools
    # - call domain Python helpers
    # - save artifacts
    # - record tool results into memory
    result = {
        "ok": True,
        "summary": f"Executed placeholder action `{name}`.",
        "data": {},
        "artifacts": [],
        "warnings": [],
        "error_code": None,
        "retryable": False,
    }

    await context.memory().record_tool_result(
        tool=name,
        ok=result["ok"],
        text=result["summary"],
        data=result["data"],
        tags=["ag.deeplens.tool", f"ag.deeplens.action:{name}"],
    )

    return result


def check_success(*, policy: Any, task: Any, state: Any) -> bool:
    # Placeholder:
    # later implement per-intent logic based on required completion facts
    # e.g. metrics produced, specs complete, design exported, root cause found, etc.
    return False


def finalize_reply(*, policy: Any, task: Any, state: Any) -> str:
    return f"[loop:{task.intent.value}] Completed current bounded loop scaffold."



async def run_reliable_loop(
    *,
    task: Any,
    policy: Any,
    state: Any,
    context_bundle: Any,
    context: Any,
) -> dict[str, Any]:
    chan = context.channel("ui:session")    

    await chan.send_phase(
        phase="execution",
        status="active",
        label="Running loop",
        detail=f"intent={task.intent.value}",
    )
    
    for step_index in range(policy.max_steps):
        action = await propose_next_action(
            task=task,
            policy=policy,
            state=state,
            context_bundle=context_bundle,
            step_index=step_index,
            context=context,
        )

        print(f"🍎 Proposed action for step {step_index}:\n{json.dumps(action, indent=2)}\n")
        ok, err = validate_action(action=action, policy=policy)

        if not ok:
            _append_trace(
                state,
                {
                    "step_index": step_index,
                    "action_kind": "validation_error",
                    "action_name": action.get("name"),
                    "summary": err,
                    "ok": False,
                },
            )
            retries = _increment_retry(state, "validation")
            if retries > policy.repair_budget:
                return {
                    "reply": f"I stopped because loop validation failed repeatedly: {err}",
                    "state": state,
                }
            continue

        _reset_retry(state, "validation")

        kind = action.get("kind")

        if kind == "ask_user":
            args = action.get("args") or {}
            prompt = args.get("prompt") or "Please provide the missing information."
            answer = await chan.ask_text(prompt=prompt)
            _append_trace(
                state,
                {
                    "step_index": step_index,
                    "action_kind": kind,
                    "action_name": None,
                    "summary": prompt,
                    "ok": True,
                    "user_answer": answer,
                },
            )

            # Minimal placeholder merge:
            task.notes.append(f"user_answer:{answer}")
            continue

        if kind == "request_approval":
            prompt = action.get("args", {}).get("prompt") or "Please approve this action to proceed."
            resp = await chan.ask_approval(prompt=prompt, options=["Approve", "Reject"])
            approved = resp.get("choice") == "Approve" or resp.get("choice") == "approve"
            print("🍎 Approval result:", approved)
            _append_trace(
                state,
                {
                    "step_index": step_index,
                    "action_kind": kind,
                    "action_name": None,
                    "summary": prompt,
                    "ok": approved,
                },
            )

            if not approved:
                return {
                    "reply": "Okay — I stopped before making changes.",
                    "state": state,
                }
            continue

        if kind == "tool_call":
            result = await dispatch_tool_action(
                action=action,
                task=task,
                policy=policy,
                state=state,
                context_bundle=context_bundle,
                context=context,
            )

            _append_trace(
                state,
                {
                    "step_index": step_index,
                    "action_kind": kind,
                    "action_name": action.get("name"),
                    "summary": result.summary,
                    "ok": result.ok,
                    "status": result.status,
                    "run_id": result.run_id,
                },
            )

            if not result.ok:
                retries = _increment_retry(state, action.get("name") or "tool")
                if retries > policy.repair_budget:
                    return {
                        "reply": (
                            f"I stopped after repeated failures while executing "
                            f"`{action.get('name')}`."
                        ),
                        "state": state,
                    }
                continue

            _reset_retry(state, action.get("name") or "tool")

            if result.should_end_turn:
                return {
                    "reply": result.summary,
                    "state": state,
                }

            if check_success(policy=policy, task=task, state=state):
                reply = finalize_reply(policy=policy, task=task, state=state)
                return {
                    "reply": reply,
                    "state": state,
                }
            continue

        if kind == "respond":
            return {
                "reply": action["args"].get("text") or "Done.",
                "state": state,
            }

        if kind == "finish":
            return {
                "reply": finalize_reply(policy=policy, task=task, state=state),
                "state": state,
            }

        if kind == "fail":
            return {
                "reply": action["args"].get("text") or "I couldn’t safely complete the task.",
                "state": state,
            }

    return {
        "reply": (
            f"I stopped after reaching the step budget for `{task.intent.value}`. "
            "This is expected for the scaffold until success checks and real actions are implemented."
        ),
        "state": state,
    }


