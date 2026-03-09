from __future__ import annotations

import json
from typing import Any, cast

from .fake_runs import fake_heavy_run_reply, heavy_pathway_placeholder
from .types import (
    DEEPLENS_SKILL_ID,
    LOOP_CONTROLLER_SCHEMA,
    DeepLensBranch,
    DeepLensState,
    LoopControllerDecision,
    save_state,
)


def _needs_heavy_pathway(message: str) -> bool:
    msg = (message or "").lower()
    return any(
        k in msg
        for k in (
            "run full simulation",
            "large sweep",
            "batch optimization",
            "high fidelity",
            "expensive",
            "long run",
        )
    )


def _history_policy_note(state: DeepLensState) -> str:
    if state.history_mode == "full":
        return "Use broader chat history for reasoning."
    return "Use selective compact history for token-saving mode."


def _normalize_loop_rounds(state: DeepLensState) -> int:
    max_rounds = state.loop_max_rounds
    if not isinstance(max_rounds, int):
        max_rounds = 4
    return max(1, min(max_rounds, 8))


def _strip_agentic_prefix(message: str) -> str:
    msg = (message or "").strip()
    msg_l = msg.lower()
    if msg_l.startswith("/agentic on "):
        return msg[12:].strip()
    return msg


async def _recent_chat_for_llm(*, context: Any, state: DeepLensState) -> list[dict[str, str]]:
    rows = await context.memory().recent_chat(limit=40 if state.history_mode == "full" else 8)  # type: ignore[attr-defined]
    out: list[dict[str, str]] = []
    for row in rows:
        role = str(row.get("role") or "user").lower()
        if role not in {"user", "assistant", "system"}:
            role = "assistant"
        text = str(row.get("text") or "").strip()
        if text:
            out.append({"role": role, "content": text})
    return out


def _compile_branch_prompt(*, context: Any, section: str, state: DeepLensState) -> str:
    skills = context.skills()
    base = skills.compile_prompt(
        DEEPLENS_SKILL_ID,
        "deeplens.system",
        section,
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )
    return base + "\n\nContext policy: " + _history_policy_note(state)


def _compose_spec(*, branch: DeepLensBranch, message: str) -> str:
    payload = {
        "branch": branch.value,
        "intent": "compose_graph_spec",
        "status": "draft",
        "next_steps": [
            "confirm inputs/outputs",
            "pin operating assumptions",
            "map to executable graph nodes",
        ],
        "user_request": message.strip(),
    }
    return "Proposed execution spec:\n```json\n" + json.dumps(payload, indent=2) + "\n```"


async def _handle_domain_branch(
    *,
    branch: DeepLensBranch,
    section_key: str,
    message: str,
    state: DeepLensState,
    context: Any,
) -> dict[str, str]:
    if _needs_heavy_pathway(message):
        if not state.debug_heavy_runs_enabled:
            return {"reply": fake_heavy_run_reply(branch=branch, message=message)}
        return {
            "reply": heavy_pathway_placeholder(branch=branch)
            + "\n\n"
            + _compose_spec(branch=branch, message=message)
        }

    llm = context.llm()
    history = await _recent_chat_for_llm(context=context, state=state)
    system_prompt = _compile_branch_prompt(context=context, section=section_key, state=state)
    user_prompt = (
        f"User request:\n{message}\n\n"
        "Provide concise DeepLens guidance. "
        "If user asks for rigid execution, include compact structured spec in JSON."
    )
    text, _usage = await llm.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": user_prompt},
        ],
        max_output_tokens=1500,
    )
    return {"reply": str(text)}


async def handle_lens_generation(
    message: str,
    attachments: list[dict[str, Any]] | None,
    session_id: str | None,
    user_meta: dict[str, Any] | None,
    *,
    state: DeepLensState,
    context: Any,
) -> dict[str, str]:
    return await _handle_domain_branch(
        branch=DeepLensBranch.LENS_GENERATION,
        section_key="deeplens.lens_generation",
        message=message,
        state=state,
        context=context,
    )


async def handle_simulation(
    message: str,
    attachments: list[dict[str, Any]] | None,
    session_id: str | None,
    user_meta: dict[str, Any] | None,
    *,
    state: DeepLensState,
    context: Any,
) -> dict[str, str]:
    return await _handle_domain_branch(
        branch=DeepLensBranch.SIMULATION,
        section_key="deeplens.simulation",
        message=message,
        state=state,
        context=context,
    )


async def handle_optimization(
    message: str,
    attachments: list[dict[str, Any]] | None,
    session_id: str | None,
    user_meta: dict[str, Any] | None,
    *,
    state: DeepLensState,
    context: Any,
) -> dict[str, str]:
    return await _handle_domain_branch(
        branch=DeepLensBranch.OPTIMIZATION,
        section_key="deeplens.optimization",
        message=message,
        state=state,
        context=context,
    )


async def handle_visualization(
    message: str,
    attachments: list[dict[str, Any]] | None,
    session_id: str | None,
    user_meta: dict[str, Any] | None,
    *,
    state: DeepLensState,
    context: Any,
) -> dict[str, str]:
    return await _handle_domain_branch(
        branch=DeepLensBranch.VISUALIZATION,
        section_key="deeplens.visualization",
        message=message,
        state=state,
        context=context,
    )


async def handle_chat(
    message: str,
    attachments: list[dict[str, Any]] | None,
    session_id: str | None,
    user_meta: dict[str, Any] | None,
    *,
    state: DeepLensState,
    context: Any,
) -> dict[str, str]:
    llm = context.llm()
    chan = context.ui_session_channel()
    history = await _recent_chat_for_llm(context=context, state=state)
    system_prompt = _compile_branch_prompt(context=context, section="deeplens.style", state=state)
    text, _usage, _thinking = await chan.chat_and_stream(
        llm=llm,
        messages=[
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": message},
        ],
        memory_log=False,
        emit_thinking_phase=False,
        max_output_tokens=1200,
    )
    return {"reply": text}


def _safe_snippet(text: str, max_len: int = 300) -> str:
    t = (text or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 3] + "..."


def _normalize_controller_decision(raw: dict[str, Any]) -> LoopControllerDecision:
    next_branch = str(raw.get("next_branch") or DeepLensBranch.CHAT.value).strip().lower()
    if next_branch not in {
        DeepLensBranch.LENS_GENERATION.value,
        DeepLensBranch.SIMULATION.value,
        DeepLensBranch.OPTIMIZATION.value,
        DeepLensBranch.VISUALIZATION.value,
        DeepLensBranch.CHAT.value,
    }:
        next_branch = DeepLensBranch.CHAT.value
    step_objective = str(raw.get("step_objective") or "Refine the request and assumptions.")
    done = bool(raw.get("done", False))
    done_reason = str(raw.get("done_reason") or "No explicit done reason provided.")
    update_goal = str(raw.get("update_goal") or "").strip()
    update_plan = raw.get("update_plan")
    confidence_raw = raw.get("confidence", 0.5)
    confidence = confidence_raw if isinstance(confidence_raw, (int, float)) else 0.5
    confidence = float(max(0.0, min(1.0, confidence)))
    out: LoopControllerDecision = {
        "next_branch": next_branch,
        "step_objective": step_objective,
        "done": done,
        "done_reason": done_reason,
        "confidence": confidence,
    }
    if update_goal:
        out["update_goal"] = update_goal
    if isinstance(update_plan, dict):
        out["update_plan"] = update_plan
    return out


async def _loop_controller_decide(
    *,
    message: str,
    state: DeepLensState,
    context: Any,
    round_index: int,
) -> LoopControllerDecision:
    llm = context.llm()
    skills = context.skills()
    loop_prompt = skills.compile_prompt(
        DEEPLENS_SKILL_ID,
        "deeplens.system",
        "deeplens.agentic",
        "deeplens.loop_controller",
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )
    trace = state.loop_trace or []
    trace_summary = json.dumps(trace[-4:], ensure_ascii=False, indent=2) if trace else "[]"
    user_prompt = (
        "Choose the next DeepLens branch for iterative problem solving.\n"
        "Available branches: lens_generation | simulation | optimization | visualization | chat.\n\n"
        f"Original user request:\n{message}\n\n"
        f"Current goal:\n{state.active_goal or message}\n\n"
        f"Current plan:\n{json.dumps(state.active_plan or {}, ensure_ascii=False, indent=2)}\n\n"
        f"Round index: {round_index}\n"
        f"Max rounds: {_normalize_loop_rounds(state)}\n\n"
        f"Recent loop trace:\n{trace_summary}\n\n"
        "Return strict JSON only."
    )
    resp, _usage = await llm.chat(
        messages=[
            {"role": "system", "content": loop_prompt},
            {"role": "user", "content": user_prompt},
        ],
        output_format="json",
        json_schema=LOOP_CONTROLLER_SCHEMA,
        schema_name="DeepLensLoopControllerV1",
        strict_schema=True,
        validate_json=True,
        max_output_tokens=400,
    )
    obj = json.loads(resp) if isinstance(resp, str) else resp
    return _normalize_controller_decision(obj if isinstance(obj, dict) else {})


async def _execute_loop_step(
    *,
    branch: DeepLensBranch,
    step_objective: str,
    message: str,
    attachments: list[dict[str, Any]] | None,
    session_id: str | None,
    user_meta: dict[str, Any] | None,
    state: DeepLensState,
    context: Any,
) -> dict[str, str]:
    step_message = (
        f"Loop objective: {step_objective}\n\n"
        f"Original request:\n{message}"
    )
    if branch == DeepLensBranch.LENS_GENERATION:
        return await handle_lens_generation(
            step_message, attachments, session_id, user_meta, state=state, context=context
        )
    if branch == DeepLensBranch.SIMULATION:
        return await handle_simulation(
            step_message, attachments, session_id, user_meta, state=state, context=context
        )
    if branch == DeepLensBranch.OPTIMIZATION:
        return await handle_optimization(
            step_message, attachments, session_id, user_meta, state=state, context=context
        )
    if branch == DeepLensBranch.VISUALIZATION:
        return await handle_visualization(
            step_message, attachments, session_id, user_meta, state=state, context=context
        )
    return await handle_chat(
        step_message, attachments, session_id, user_meta, state=state, context=context
    )


def _is_stagnating(trace: list[dict[str, Any]]) -> bool:
    if len(trace) < 2:
        return False
    last = trace[-1]
    prev = trace[-2]
    return (
        str(last.get("branch")) == str(prev.get("branch"))
        and str(last.get("objective", "")).strip().lower()
        == str(prev.get("objective", "")).strip().lower()
    )


def _summarize_loop_final(*, state: DeepLensState, stop_reason: str) -> str:
    trace = state.loop_trace or []
    lines: list[str] = []
    lines.append("Agentic loop summary:")
    lines.append(f"- status: {state.loop_status}")
    lines.append(f"- rounds: {state.loop_round}/{_normalize_loop_rounds(state)}")
    lines.append(f"- goal: {state.active_goal or '(not set)'}")
    lines.append(f"- stop_reason: {stop_reason}")
    if trace:
        lines.append("")
        lines.append("Step trace:")
        for row in trace[-6:]:
            lines.append(
                f"- r{row.get('round')} [{row.get('branch')}]: "
                f"{row.get('objective')} | confidence={row.get('confidence')}"
            )
            if row.get("reply_snippet"):
                lines.append(f"  snippet: {row.get('reply_snippet')}")
    if state.active_plan:
        lines.append("")
        lines.append("Current plan snapshot:")
        lines.append("```json")
        lines.append(json.dumps(state.active_plan, ensure_ascii=False, indent=2))
        lines.append("```")
    if state.loop_last_outcome:
        lines.append("")
        lines.append("Latest outcome:")
        lines.append(state.loop_last_outcome)
    return "\n".join(lines)


async def handle_agentic_loop(
    message: str,
    attachments: list[dict[str, Any]] | None,
    session_id: str | None,
    user_meta: dict[str, Any] | None,
    *,
    state: DeepLensState,
    context: Any,
) -> dict[str, str]:
    # TODO(v3): add search branch for literature/prior-art retrieval before simulation design.
    # TODO(v3): add aux_math branch for symbolic/analytic optics helper calculations.
    # TODO(v3): add send_files branch to package reports/specs as artifacts for user download.
    # TODO(v3): add loop tool-call branch when real DeepLens execution APIs are wired.
    chan = context.ui_session_channel()
    user_goal = _strip_agentic_prefix(message)
    if not user_goal:
        user_goal = message

    state.loop_status = "running"
    state.active_goal = user_goal
    state.active_plan = state.active_plan or {"status": "draft"}
    state.loop_round = 0
    state.loop_trace = []
    state.loop_last_outcome = None
    state.pending_action = "agentic_loop"
    await save_state(context=context, state=state)

    await chan.send_phase(
        phase="loop.init",
        status="active",
        label="Initializing agentic loop",
        detail="Setting loop goal and starting iterative planning.",
    )

    max_rounds = _normalize_loop_rounds(state)
    stop_reason = "max_rounds_reached"
    for round_index in range(1, max_rounds + 1):
        await chan.send_phase(
            phase="loop.round",
            status="active",
            label=f"Loop round {round_index}",
            detail="Selecting next branch and executing step.",
        )
        try:
            decision = await _loop_controller_decide(
                message=user_goal,
                state=state,
                context=context,
                round_index=round_index,
            )
        except Exception:
            context.logger().exception("deeplens: loop controller failed")
            decision = {
                "next_branch": DeepLensBranch.CHAT.value,
                "step_objective": "Recover from controller error and provide safe summary.",
                "done": False,
                "done_reason": "controller_error_fallback",
                "confidence": 0.2,
            }

        if decision.get("update_goal"):
            state.active_goal = str(decision["update_goal"])
        if isinstance(decision.get("update_plan"), dict):
            state.active_plan = cast(dict[str, Any], decision["update_plan"])

        if bool(decision.get("done")):
            state.loop_round = round_index - 1
            state.loop_status = "completed"
            stop_reason = str(decision.get("done_reason") or "controller_done")
            break

        next_branch = DeepLensBranch(str(decision.get("next_branch") or DeepLensBranch.CHAT.value))
        step_objective = str(decision.get("step_objective") or "Refine the solution.")
        confidence = float(decision.get("confidence", 0.5))

        try:
            step_out = await _execute_loop_step(
                branch=next_branch,
                step_objective=step_objective,
                message=user_goal,
                attachments=attachments,
                session_id=session_id,
                user_meta=user_meta,
                state=state,
                context=context,
            )
        except Exception:
            context.logger().exception("deeplens: loop step failed")
            state.loop_status = "failed"
            state.loop_round = round_index
            state.loop_last_outcome = "Loop step execution failed."
            stop_reason = "step_execution_failed"
            await save_state(context=context, state=state)
            return {"reply": _summarize_loop_final(state=state, stop_reason=stop_reason)}

        reply = str(step_out.get("reply") or "")
        trace_row = {
            "round": round_index,
            "branch": next_branch.value,
            "objective": step_objective,
            "confidence": round(confidence, 3),
            "reply_snippet": _safe_snippet(reply),
        }
        if state.loop_trace is None:
            state.loop_trace = []
        state.loop_trace.append(trace_row)
        state.loop_last_outcome = reply
        state.last_branch = next_branch
        state.loop_round = round_index
        await save_state(context=context, state=state)

        await chan.send_phase(
            phase="loop.round",
            status="done",
            label=f"Round {round_index} complete",
            detail=f"Executed branch: {next_branch.value}",
        )

        if _is_stagnating(state.loop_trace):
            state.loop_status = "stopped"
            stop_reason = "stagnation_guard"
            break
    else:
        state.loop_status = "stopped"

    if state.loop_status == "running":
        state.loop_status = "completed" if stop_reason == "controller_done" else "stopped"
    state.pending_action = None
    await save_state(context=context, state=state)

    await chan.send_phase(
        phase="loop.finalize",
        status="done",
        label="Loop finalized",
        detail=f"Loop ended with status: {state.loop_status}",
    )
    return {"reply": _summarize_loop_final(state=state, stop_reason=stop_reason)}
