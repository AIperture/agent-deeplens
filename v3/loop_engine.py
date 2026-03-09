from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from .extraction import (
    extract_analysis_request,
    extract_design_spec,
    extract_run_request,
)
from .memory_policy import build_context_bundle
from .plan import advance_plan, build_plan, plan_from_dict, plan_step_to_action, plan_to_dict
from .router import _missing_fields_for_domain
from .tool_dispatch import dispatch_tool_action
from .tool_registry import get_tool_spec
from .types import DEEPLENS_SKILL_ID, LoopAction, TaskShape, save_state


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


def _format_action_detail(action: LoopAction) -> str:
    kind = action.get("kind") or "unknown"
    name = action.get("name")
    args = action.get("args") or {}
    if kind == "tool_call":
        return f"tool_call:{name or 'unknown'}"
    if kind == "request_approval":
        return f"approval:{name or 'next tool'}"
    if kind == "ask_user":
        return f"ask_user:{str(args.get('prompt') or 'missing information')[:80]}"
    if kind in {"respond", "finish", "fail"}:
        return f"{kind}:{str(args.get('text') or action.get('rationale') or kind)[:80]}"
    return kind


def _default_ask_prompt(task: Any) -> str:
    missing = list(getattr(task, "missing_fields", []) or [])
    if task.domain_hint.value == "design":
        prompts: list[str] = []
        if "foclen_or_imgh" in missing:
            prompts.append("target focal length (`foclen`) or image height / sensor format (`imgh`)")
        if "fnum" in missing:
            prompts.append("F-number (`fnum`)")
        if "fov" in missing:
            prompts.append("field of view (`fov`)")
        if prompts:
            joined = ", ".join(prompts)
            return f"I need the remaining design inputs before creating the lens: {joined}."
    if "lens_source" in missing:
        return "Please upload a `.json` or `.zmx` lens file, or confirm the active lens to use."
    if "run_id" in missing:
        return "I need the background run id before I can check status or cancel it."
    return "Please provide the missing information so I can continue."


def _consume_requested_next_step(state: Any) -> LoopAction | None:
    """Consume `requested_next_step` from state and return a deterministic action, or None."""
    step = state.requested_next_step
    if not step:
        return None
    state.requested_next_step = None
    if step == "interpret_or_export":
        return {
            "kind": "respond",
            "name": None,
            "args": {"text": "Analysis complete. You can review the results above, or ask me to export or optimize."},
            "rationale": "Interpreting analysis results.",
        }
    if step == "analyze_or_optimize":
        return {
            "kind": "respond",
            "name": None,
            "args": {"text": "Design created. You can ask me to analyze it, optimize it, or export it."},
            "rationale": "Suggesting next steps after design.",
        }
    if step == "share_or_optimize":
        return {
            "kind": "respond",
            "name": None,
            "args": {"text": "Export complete. You can optimize the lens or start a new design."},
            "rationale": "Suggesting next steps after export.",
        }
    if step == "status_or_cancel":
        return {
            "kind": "respond",
            "name": None,
            "args": {"text": "Background run submitted. Use ‘status’ to check progress or ‘cancel’ to stop it."},
            "rationale": "Guiding user after run submission.",
        }
    return None


def _fallback_next_action(task: Any, state: Any) -> LoopAction:
    missing = list(getattr(task, "missing_fields", []) or [])
    preferred_tool = getattr(task, "preferred_tool", None)
    if task.task_shape == TaskShape.UNSUPPORTED:
        return {
            "kind": "fail",
            "name": None,
            "args": {"text": "This DeepLens agent does not support that request yet."},
            "rationale": "Unsupported request.",
        }
    if missing:
        return {
            "kind": "ask_user",
            "name": None,
            "args": {"prompt": _default_ask_prompt(task)},
            "rationale": "Required inputs are still missing.",
        }
    # Consume requested_next_step if set (from a prior tool result).
    next_step_action = _consume_requested_next_step(state)
    if next_step_action is not None:
        return next_step_action
    if task.task_shape == TaskShape.RUN_CONTROL and preferred_tool in {"ag.status", "ag.cancel"}:
        return {
            "kind": "tool_call",
            "name": preferred_tool,
            "args": {},
            "rationale": "Run-control tool is explicit.",
        }
    if preferred_tool == "ag.spawn_graph" and state.approved_action != preferred_tool:
        return {
            "kind": "request_approval",
            "name": preferred_tool,
            "args": {"approval_prompt": "I’m ready to submit the background optimization run. Approve?"},
            "rationale": "Background optimization needs approval.",
        }
    if preferred_tool:
        return {
            "kind": "tool_call",
            "name": preferred_tool,
            "args": {},
            "rationale": "Preferred workflow tool is available.",
        }
    if task.task_shape == TaskShape.DIRECT_ANSWER:
        return {
            "kind": "respond",
            "name": None,
            "args": {"text": "I can help with analysis, design, export, optimization status, or cancellation."},
            "rationale": "Direct answer requested.",
        }
    return {
        "kind": "fail",
        "name": None,
        "args": {"text": "I could not determine a safe next action."},
        "rationale": "No safe fallback action.",
    }


def _is_deterministic(task: Any, state: Any) -> bool:
    """Check if the next action can be determined without an LLM call."""
    if task.task_shape in (TaskShape.DIRECT_ANSWER, TaskShape.UNSUPPORTED):
        return True
    if task.task_shape == TaskShape.RUN_CONTROL:
        return True
    if state.requested_next_step:
        return True
    missing = list(getattr(task, "missing_fields", []) or [])
    if missing:
        return True
    preferred_tool = getattr(task, "preferred_tool", None)
    if preferred_tool:
        return True
    return False


async def _emit_loop_update(
    chan: Any,
    *,
    phase: str,
    phase_status: str,
    label: str,
    detail: str,
    step_index: int,
    kind: str,
    note_text: str | None = None,
    tool_name: str | None = None,
    loop_status: str | None = None,
) -> None:
    """Emit a phase update and optionally a memory-logged note in a single call."""
    await chan.send_phase(phase=phase, status=phase_status, label=label, detail=detail)
    if note_text:
        await chan.send_text(
            note_text,
            memory_log=True,
            memory_tags=["ag.deeplens.v3.progress", f"loop_kind:{kind}"],
            memory_data={
                "step_index": step_index,
                "kind": kind,
                "tool_name": tool_name,
                "status": loop_status or phase_status,
            },
            memory_severity=1,
        )


def _merge_task_update_from_user(*, task: Any, text: str, files: list[dict[str, Any]]) -> None:
    answer = (text or "").strip()
    if answer:
        task.user_goal = answer
        task.notes.append(f"user_answer:{answer}")

    normalized_files = [f for f in files if isinstance(f, dict)]
    if normalized_files:
        task.attachments.extend(normalized_files)
        task.source_refs.extend(
            [
                {
                    "name": item.get("name") or item.get("filename") or item.get("uri"),
                    "artifact_id": item.get("artifact_id"),
                    "uri": item.get("uri"),
                }
                for item in normalized_files
            ]
        )
        if not task.lens_source:
            first = normalized_files[0]
            task.lens_source = {
                "name": first.get("name") or first.get("filename") or first.get("uri"),
                "artifact_id": first.get("artifact_id"),
                "uri": first.get("uri"),
            }

    if answer:
        design_spec = extract_design_spec(answer)
        if design_spec:
            merged_design = dict(task.design_spec or {})
            merged_design.update({k: v for k, v in design_spec.items() if v is not None})
            task.design_spec = merged_design

        run_request = extract_run_request(answer)
        if run_request:
            merged_run = dict(task.run_request or {})
            merged_run.update(run_request)
            task.run_request = merged_run

        analysis_request = extract_analysis_request(answer, task.attachments or [])
        if analysis_request:
            merged_analysis = dict(task.analysis_request or {})
            merged_analysis.update({k: v for k, v in analysis_request.items() if v is not None})
            if analysis_request.get("source_refs"):
                merged_analysis["source_refs"] = analysis_request["source_refs"]
            task.analysis_request = merged_analysis

    task.missing_fields = _missing_fields_for_domain(task.domain_hint, task)


async def _save_loop_state(task: Any, state: Any, context: Any) -> None:
    """Cheap: sync active task into state and persist. No context rebuild."""
    _sync_active_task(state, task)
    await save_state(context=context, state=state)


async def refresh_loop_context(task: Any, state: Any, context: Any, context_mode: Any) -> Any:
    """Expensive: save state AND rebuild the full context bundle from memory."""
    await _save_loop_state(task, state, context)
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
        "analysis": "deeplens.domain_analysis",
        "design": "deeplens.domain_design",
        "optimization": "deeplens.domain_optimization",
        "debug": "deeplens.domain_debug",
        "unknown": "deeplens.refusal",
    }.get(task.domain_hint.value, "deeplens.refusal")
    system_prompt = skills.compile_prompt(
        DEEPLENS_SKILL_ID,
        "deeplens.system",
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
            "For single_action, prefer one workflow-level tool_call if the tool is obvious.",
            "For multi_step, ask for missing info or approval before expensive work.",
            "For run_control, prefer status or cancel related tool calls only.",
            "If a long-running graph is submitted, usually end the turn.",
            "If unsupported, prefer fail with a concise refusal.",
            "Keep rationale under 40 words.",
            "Keep prompt, approval_prompt, and text concise; prefer at most 2 short sentences.",
            "For request_approval, use approval_prompt instead of a long detailed prompt.",
            "Do not include job plans, file lists, bullet lists, or long summaries inside prompt fields.",
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
            "rationale": {"type": "string"},
            "prompt": {"type": ["string", "null"]},
            "text": {"type": ["string", "null"]},
            "approval_prompt": {"type": ["string", "null"]},
            "run_id": {"type": ["string", "null"]},
            "graph_id": {"type": ["string", "null"]},
            "timeout_s": {"type": ["number", "null"]},
            "use_stub": {"type": ["boolean", "null"]},
            "analysis_mode": {"type": ["string", "null"], "enum": ["full", "spot", "mtf", "rms", None]},
            "iterations": {"type": ["integer", "null"]},
            "checkpoint_every": {"type": ["integer", "null"]},
        },
        "required": [
            "kind",
            "name",
            "rationale",
            "prompt",
            "text",
            "approval_prompt",
            "run_id",
            "graph_id",
            "timeout_s",
            "use_stub",
            "analysis_mode",
            "iterations",
            "checkpoint_every",
        ],
        "additionalProperties": False,
    }
    try:
        resp, _usage = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=action_schema,
            schema_name="DeepLensLoopAction",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=320,
            reasoning_effort="low",
        )
    except Exception:
        return _fallback_next_action(task, state)
    obj = json.loads(resp) if isinstance(resp, str) else resp
    args: dict[str, Any] = {}
    for key in ("prompt", "text", "approval_prompt", "run_id", "graph_id", "timeout_s", "use_stub"):
        value = obj.get(key)
        if value is not None:
            args[key] = value

    analysis_mode = obj.get("analysis_mode")
    if analysis_mode is not None:
        args["analysis_request"] = {"mode": analysis_mode}

    run_request: dict[str, Any] = {}
    if obj.get("iterations") is not None:
        run_request["iterations"] = obj["iterations"]
    if obj.get("checkpoint_every") is not None:
        run_request["checkpoint_every"] = obj["checkpoint_every"]
    if run_request:
        args["run_request"] = run_request

    return {
        "kind": obj["kind"],
        "name": obj.get("name"),
        "args": args,
        "rationale": obj["rationale"],
    }


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
    await _emit_loop_update(
        chan,
        phase="execution",
        phase_status="active",
        label="Running loop",
        detail=f"domain={task.domain_hint.value} shape={task.task_shape.value}",
        step_index=0,
        kind="start",
        note_text=f"I’m handling this as a {task.domain_hint.value} workflow.",
    )
    context_bundle = await refresh_loop_context(
        task=task,
        state=state,
        context=context,
        context_mode=context_mode,
    )
    # Build or restore a multi-step plan for known workflows.
    plan = plan_from_dict(state.active_plan) or build_plan(task, state)
    state.active_plan = plan_to_dict(plan)

    result_out: dict[str, Any] | None = None
    max_steps = _max_steps(task.task_shape)
    try:
        for step_index in range(max_steps):
            await chan.send_phase(
                phase="execution.step",
                status="active",
                label=f"Loop step {step_index + 1}",
                detail="Choosing the next bounded action.",
            )
            # 1) Try the plan first (deterministic, no LLM).
            action = None
            if plan is not None:
                step_str = advance_plan(plan)
                if step_str is not None:
                    action = plan_step_to_action(step_str, task, state)
                state.active_plan = plan_to_dict(plan)

            # 2) If plan didn’t produce an action, try deterministic fallback.
            if action is None and _is_deterministic(task, state):
                action = _fallback_next_action(task, state)

            # 3) Last resort: LLM-based propose.
            if action is None:
                action = await propose_next_action(
                    task=task,
                    state=state,
                    context_bundle=context_bundle,
                    step_index=step_index,
                    context=context,
                )
            await chan.send_phase(
                phase="execution.step",
                status="active",
                label=f"Loop step {step_index + 1}",
                detail=f"Proposed {_format_action_detail(action)}",
            )
            ok, err = validate_action(action=action)
            if not ok:
                _append_trace(state, {"step_index": step_index, "action": action, "error": err})
                await _save_loop_state(task, state, context)
                retries = _increment_retry(state, "validation")
                if retries > 2:
                    result_out = {
                        "reply": f"I stopped because loop validation failed repeatedly: {err}",
                        "state": state,
                    }
                    return result_out
                continue
            _reset_retry(state, "validation")
            kind = action["kind"]
            # For tool_call, trace is recorded after execution with the result merged in.
            if kind != "tool_call":
                _append_trace(state, {"step_index": step_index, "action": action})
            if kind == "ask_user":
                prompt = action["args"].get("prompt") or "Please provide the missing information."
                await _emit_loop_update(
                    chan,
                    phase="execution.step",
                    phase_status="active",
                    label=f"Loop step {step_index + 1}",
                    detail="Waiting for user input.",
                    step_index=step_index,
                    kind=kind,
                    note_text="I need a few required inputs before I can continue.",
                    loop_status="waiting_for_user",
                )
                state.pending_action = "ask_user"
                state.pending_approval = None
                state.approved_action = None
                await _save_loop_state(task, state, context)
                reply = await chan.ask_text_or_files(prompt=prompt)
                answer = str(reply.get("text") or "")
                files = reply.get("files") or []
                _merge_task_update_from_user(task=task, text=answer, files=files)
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
                prompt = (
                    action["args"].get("approval_prompt")
                    or action["args"].get("prompt")
                    or "Approve this action?"
                )
                target_name = action.get("name") or task.preferred_tool
                approval_detail = f"Requesting approval for {target_name}." if target_name else "Requesting approval for next action."
                approval_note = f"I’m ready to proceed with `{target_name}` and need your approval first." if target_name else "I’m ready to proceed and need your approval first."
                await _emit_loop_update(
                    chan,
                    phase="execution.step",
                    phase_status="active",
                    label=f"Loop step {step_index + 1}",
                    detail=approval_detail,
                    step_index=step_index,
                    kind=kind,
                    note_text=approval_note,
                    tool_name=target_name,
                    loop_status="awaiting_approval",
                )
                state.pending_action = "request_approval"
                state.pending_approval = {
                    "prompt": prompt,
                    "step_index": step_index,
                    "action": target_name,
                }
                await _save_loop_state(task, state, context)
                resp = await chan.ask_approval(prompt=prompt, options=["Approve", "Reject"])
                approved = bool(resp.get("approved"))
                approval_str = "approved" if approved else "rejected"
                task.notes.append(f"approval:{approval_str}:{prompt}")
                state.pending_action = None
                state.pending_approval = None
                state.approved_action = target_name if approved else None
                await _save_loop_state(task, state, context)
                if not approved:
                    result_out = {"reply": "Okay, I stopped before making changes.", "state": state}
                    return result_out
                continue
            if kind == "tool_call":
                tool_name = action.get("name")
                tool_label = tool_name or "tool"
                state.pending_action = tool_name
                state.pending_approval = None
                await _emit_loop_update(
                    chan,
                    phase="execution.step",
                    phase_status="active",
                    label=f"Loop step {step_index + 1}",
                    detail=f"Executing {tool_label}.",
                    step_index=step_index,
                    kind=kind,
                    note_text=f"I’m executing `{tool_label}` now.",
                    tool_name=tool_name,
                    loop_status="running",
                )
                await _save_loop_state(task, state, context)
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
                        "tool_name": tool_name,
                        "rationale": action.get("rationale"),
                        "summary": result.summary,
                        "ok": result.ok,
                        "status": result.status,
                    },
                )
                state.pending_action = None
                result_ok_str = "ok" if result.ok else "error"
                task.notes.append(f"tool_result:{tool_name}:{result_ok_str}:{result.status}")
                result_phase_status = "active" if result.ok and not result.should_end_turn else "done"
                await _emit_loop_update(
                    chan,
                    phase="execution.step",
                    phase_status=result_phase_status,
                    label=f"Loop step {step_index + 1}",
                    detail=f"{tool_label} -> {result.status}",
                    step_index=step_index,
                    kind=kind,
                    note_text=result.summary,
                    tool_name=tool_name,
                    loop_status=result.status,
                )
                context_bundle = await refresh_loop_context(
                    task=task,
                    state=state,
                    context=context,
                    context_mode=context_mode,
                )
                if not result.ok:
                    retries = _increment_retry(state, tool_name or "tool")
                    if retries > 2:
                        result_out = {
                            "reply": f"I stopped after repeated failures while executing `{tool_name}`.",
                            "state": state,
                        }
                        return result_out
                    continue
                _reset_retry(state, tool_name or "tool")
                if result.should_end_turn:
                    result_out = {"reply": result.summary, "state": state}
                    return result_out
                continue
            if kind == "respond":
                result_out = {"reply": action["args"].get("text") or "Done.", "state": state}
                return result_out
            if kind == "finish":
                result_out = {"reply": "Done.", "state": state}
                return result_out
            if kind == "fail":
                result_out = {
                    "reply": action["args"].get("text")
                    or "This DeepLens agent does not support that request yet.",
                    "state": state,
                }
                return result_out
        result_out = {"reply": "I stopped after the current step budget.", "state": state}
        return result_out
    finally:
        # Always emit a final "done" phase so the UI clears the progress indicator.
        reply_text = (result_out or {}).get("reply", "Loop ended.")
        await chan.send_phase(
            phase="execution",
            status="done",
            label="Loop finished",
            detail=reply_text[:120],
        )
