from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any

from . import policy
from .types import ACTIVE_TASK_STATUSES, ContextMode, RouteDecision, RouteResult, TaskShape, TemplateState, TemplateTask


def _default_context_mode(state: TemplateState) -> ContextMode:
    return ContextMode.FULL if state.context_mode == "full" else ContextMode.LITE


def _active_task_from_state(state: TemplateState) -> TemplateTask | None:
    return TemplateTask.from_dict(state.active_task if isinstance(state.active_task, dict) else None)


def _detect_capabilities(message: str) -> list[str]:
    lowered = (message or "").lower()
    found: list[tuple[int, str]] = []
    for capability in policy.CAPABILITY_ORDER:
        for keyword in list(policy.CAPABILITY_CATALOG[capability]["keywords"]):
            idx = lowered.find(keyword)
            if idx >= 0:
                found.append((idx, capability))
                break
    found.sort(key=lambda item: item[0])
    dedup: list[str] = []
    for _, capability in found:
        if capability not in dedup:
            dedup.append(capability)
    return dedup


def _infer_task_shape(capabilities: list[str], message: str) -> TaskShape:
    if not capabilities and any(token in (message or "").lower() for token in ("what", "why", "explain", "help")):
        return TaskShape.DIRECT_ANSWER
    if len(capabilities) <= 1:
        return TaskShape.SINGLE_ACTION if capabilities else TaskShape.UNSUPPORTED
    return TaskShape.MULTI_STEP


def _parse_message_args(message: str) -> dict[str, Any]:
    text = message or ""
    args: dict[str, Any] = {}
    for key, value in re.findall(r"(\w+)\s*=\s*\"?([^\",]+)\"?", text):
        args[key] = value.strip()
    lowered = text.lower()
    if " about " in lowered and "topic" not in args:
        args["topic"] = text.split(" about ", 1)[1].strip()
    if " for " in lowered and "objective" not in args:
        args["objective"] = text.split(" for ", 1)[1].strip()
    if "change:" in lowered and "change_summary" not in args:
        args["change_summary"] = text.split(":", 1)[1].strip()
    if "constraints" in lowered and "constraints" not in args:
        after = lowered.split("constraints", 1)[1].lstrip(":=").strip()
        if after:
            args["constraints"] = [item.strip() for item in after.split(",") if item.strip()]
    return args


def _make_task(*, message: str, attachments: list[dict[str, Any]], task_shape: TaskShape) -> TemplateTask:
    parsed_args = _parse_message_args(message)
    capabilities = _detect_capabilities(message)
    return TemplateTask(
        user_goal=message,
        task_shape=task_shape,
        attachments=attachments,
        parsed_args=parsed_args,
        requested_capabilities=capabilities,
    )


def apply_slash_command(message: str, state: TemplateState) -> dict[str, Any] | None:
    msg = (message or "").strip().lower()
    if msg == "/mode full":
        state.context_mode = "full"
        return {"reply": "Context mode set to full."}
    if msg == "/mode lite":
        state.context_mode = "lite"
        return {"reply": "Context mode set to lite."}
    if msg == "/debug state":
        return {"reply": json.dumps(state.to_dict(), ensure_ascii=False, indent=2)}
    if msg == "/debug plan":
        return {"reply": json.dumps(state.active_agenda or {}, ensure_ascii=False, indent=2)}
    return None


async def _llm_turn_role(message: str, state: TemplateState, active_task: TemplateTask, context: Any) -> tuple[str, str] | None:
    try:
        llm = context.llm("fast")
        schema = {
            "type": "object",
            "properties": {
                "turn_role": {"type": "string", "enum": ["new_task", "continue_task", "answer_pending_interaction"]},
                "reason": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["turn_role", "reason", "confidence"],
            "additionalProperties": False,
        }
        payload = {
            "message": message,
            "active_task": active_task.to_dict(),
            "pending_action": state.pending_action,
            "waiting_prompt": state.waiting_prompt,
        }
        response, _usage = await llm.chat(
            messages=[
                {"role": "system", "content": "Classify the turn as new_task, continue_task, or answer_pending_interaction."},
                {"role": "user", "content": json.dumps(payload)},
            ],
            output_format="json_schema",
            json_schema=schema,
            schema_name="TemplateTurnRole",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=100,
        )
        obj = json.loads(response) if isinstance(response, str) else response
        if float(obj.get("confidence") or 0) < 0.6:
            return None
        return str(obj.get("turn_role")), str(obj.get("reason") or "llm")
    except Exception:
        return None


async def route(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    state: TemplateState,
    context: Any | None = None,
    user_meta: dict[str, Any] | None = None,
) -> RouteResult:
    del user_meta

    slash = apply_slash_command(message, state)
    if slash is not None:
        task = _make_task(message=message, attachments=attachments, task_shape=TaskShape.DIRECT_ANSWER)
        decision: RouteDecision = {
            "route_kind": "new_task",
            "task_shape": task.task_shape,
            "context_mode": _default_context_mode(state),
            "reason": "slash_command",
            "confidence": 1.0,
            "task": task,
        }
        return {"decision": decision, "state": state, "immediate_reply": slash["reply"], "turn_role": "slash_command"}

    active_task = _active_task_from_state(state)
    normalized = " ".join((message or "").lower().split())
    if active_task is not None and active_task.task_status in ACTIVE_TASK_STATUSES:
        if any(marker in normalized for marker in policy.NEW_TASK_MARKERS):
            active_task = None
        elif state.pending_action or state.waiting_prompt:
            if normalized in policy.SHORT_ANSWER_TOKENS or len(normalized.split()) <= 5:
                merged = TemplateTask.from_dict(asdict(active_task)) or active_task
                merged.notes.append(f"user_answer:{message}")
                merged.parsed_args.update(_parse_message_args(message))
                decision = {
                    "route_kind": "answer_pending_interaction",
                    "task_shape": merged.task_shape,
                    "context_mode": _default_context_mode(state),
                    "reason": "state:pending_interaction",
                    "confidence": 0.95,
                    "task": merged,
                }
                state.active_agenda = None
                return {"decision": decision, "state": state, "immediate_reply": None, "turn_role": "answer_pending_interaction"}
        elif any(phrase in normalized for phrase in policy.CONTINUATION_PHRASES):
            merged = TemplateTask.from_dict(asdict(active_task)) or active_task
            merged.notes.append(f"continuation:{message}")
            decision = {
                "route_kind": "continue_task",
                "task_shape": merged.task_shape,
                "context_mode": _default_context_mode(state),
                "reason": "state:continuation",
                "confidence": 0.9,
                "task": merged,
            }
            state.active_agenda = None
            return {"decision": decision, "state": state, "immediate_reply": None, "turn_role": "continue_task"}
        elif context is not None:
            llm_role = await _llm_turn_role(message, state, active_task, context)
            if llm_role is not None and llm_role[0] != "new_task":
                merged = TemplateTask.from_dict(asdict(active_task)) or active_task
                merged.parsed_args.update(_parse_message_args(message))
                state.active_agenda = None
                decision = {
                    "route_kind": llm_role[0],  # type: ignore[assignment]
                    "task_shape": merged.task_shape,
                    "context_mode": _default_context_mode(state),
                    "reason": llm_role[1],
                    "confidence": 0.75,
                    "task": merged,
                }
                return {"decision": decision, "state": state, "immediate_reply": None, "turn_role": llm_role[0]}

    capabilities = _detect_capabilities(message)
    task_shape = _infer_task_shape(capabilities, message)
    task = _make_task(message=message, attachments=attachments, task_shape=task_shape)
    decision = {
        "route_kind": "new_task",
        "task_shape": task_shape,
        "context_mode": _default_context_mode(state),
        "reason": "heuristic:capability_detected" if capabilities else "heuristic:fallback",
        "confidence": 0.8 if capabilities else 0.5,
        "task": task,
    }
    print(f"🍎 Routing decision: {decision['reason']} (shape={decision['task_shape']}, capabilities={task.requested_capabilities})")
    return {"decision": decision, "state": state, "immediate_reply": None, "turn_role": "new_task"}
