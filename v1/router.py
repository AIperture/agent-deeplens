from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from aethergraph import NodeContext

from .types import (
    ContextMode,
    DEEPLENS_SKILL_ID,
    DeepLensIntent,
    DeepLensState,
    DeepLensTask,
    ExecutionMode,
    RouterDecision,
    ROUTER_JSON_SCHEMA,
)

@dataclass
class SlashCommandResult:
    state: DeepLensState 
    decision: RouterDecision
    reply: str | None = None 

def _copy_state(state: DeepLensState) -> DeepLensState:
    return DeepLensState.from_dict(state.to_dict())

def _default_context_mode(state: DeepLensState) -> ContextMode:
    return ContextMode.FULL if state.context_mode == "full" else ContextMode.LITE

def _make_task(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    intent: DeepLensIntent,
    execution_preference: ExecutionMode,
    parsed_args: dict[str, Any] | None = None,
    missing_fields: list[str] | None = None,
) -> DeepLensTask:
    return DeepLensTask(
        user_goal=message,
        intent=intent,
        execution_preference=execution_preference,
        attachments=attachments,
        parsed_args=parsed_args or {},
        missing_fields=missing_fields or [],
    )


def apply_slash_command(
    message: str,
    attachments: list[dict[str, Any]],
    state: DeepLensState,
) -> SlashCommandResult | None:
    msg = (message or "").strip()
    msg_l = msg.lower()
    if not msg_l.startswith("/"):
        return None

    ctx_mode = _default_context_mode(state)

    if msg_l.startswith("/lens"):
        return SlashCommandResult(
            state=state,
            decision={
                "intent": DeepLensIntent.LENS_DESIGN,
                "execution_mode": ExecutionMode.WORKFLOW,
                "context_mode": ctx_mode,
                "reason": "slash:/lens",
                "confidence": 1.0,
                "task": _make_task(
                    message=message,
                    attachments=attachments,
                    intent=DeepLensIntent.LENS_DESIGN,
                    execution_preference=ExecutionMode.WORKFLOW,
                ),
            },
        )

    if msg_l.startswith("/simulate"):
        return SlashCommandResult(
            state=state,
            decision={
                "intent": DeepLensIntent.SIMULATION,
                "execution_mode": ExecutionMode.WORKFLOW,
                "context_mode": ctx_mode,
                "reason": "slash:/simulate",
                "confidence": 1.0,
                "task": _make_task(
                    message=message,
                    attachments=attachments,
                    intent=DeepLensIntent.SIMULATION,
                    execution_preference=ExecutionMode.WORKFLOW,
                ),
            },
        )

    if msg_l.startswith("/optimize"):
        return SlashCommandResult(
            state=state,
            decision={
                "intent": DeepLensIntent.OPTIMIZATION,
                "execution_mode": ExecutionMode.WORKFLOW,
                "context_mode": ctx_mode,
                "reason": "slash:/optimize",
                "confidence": 1.0,
                "task": _make_task(
                    message=message,
                    attachments=attachments,
                    intent=DeepLensIntent.OPTIMIZATION,
                    execution_preference=ExecutionMode.WORKFLOW,
                ),
            },
        )

    if msg_l.startswith("/viz"):
        return SlashCommandResult(
            state=state,
            decision={
                "intent": DeepLensIntent.VISUALIZATION,
                "execution_mode": ExecutionMode.WORKFLOW,
                "context_mode": ctx_mode,
                "reason": "slash:/viz",
                "confidence": 1.0,
                "task": _make_task(
                    message=message,
                    attachments=attachments,
                    intent=DeepLensIntent.VISUALIZATION,
                    execution_preference=ExecutionMode.WORKFLOW,
                ),
            },
        )

    if msg_l.startswith("/chat"):
        return SlashCommandResult(
            state=state,
            decision={
                "intent": DeepLensIntent.CHAT,
                "execution_mode": ExecutionMode.AUTO,
                "context_mode": ctx_mode,
                "reason": "slash:/chat",
                "confidence": 1.0,
                "task": _make_task(
                    message=message,
                    attachments=attachments,
                    intent=DeepLensIntent.CHAT,
                    execution_preference=ExecutionMode.AUTO,
                ),
            },
        )

    if msg_l.startswith("/debug"):
        # You can later choose whether '/debug' means "toggle debug mode"
        # or "route to debug workflow". For now I recommend routing.
        return SlashCommandResult(
            state=state,
            decision={
                "intent": DeepLensIntent.DEBUG,
                "execution_mode": ExecutionMode.LOOP,
                "context_mode": ctx_mode,
                "reason": "slash:/debug",
                "confidence": 1.0,
                "task": _make_task(
                    message=message,
                    attachments=attachments,
                    intent=DeepLensIntent.DEBUG,
                    execution_preference=ExecutionMode.LOOP,
                ),
            },
        )

    if msg_l.startswith("/agent"):
        parts = msg.split(maxsplit=2)
        action = parts[1].lower() if len(parts) >= 2 else "status"
        remainder = parts[2].strip() if len(parts) >= 3 else ""

        if action not in {"on", "off", "status"}:
            return SlashCommandResult(
                state=state,
                decision={
                    "intent": DeepLensIntent.CHAT,
                    "execution_mode": ExecutionMode.AUTO,
                    "context_mode": ctx_mode,
                    "reason": "slash:/agent invalid",
                    "confidence": 1.0,
                    "task": _make_task(
                        message=message,
                        attachments=attachments,
                        intent=DeepLensIntent.CHAT,
                        execution_preference=ExecutionMode.AUTO,
                    ),
                },
                reply="Usage: `/agent on`, `/agent off`, or `/agent status`.",
            )

        if action == "status":
            status = "ON" if state.agent_mode_enabled else "OFF"
            return SlashCommandResult(
                state=state,
                decision={
                    "intent": DeepLensIntent.CHAT,
                    "execution_mode": ExecutionMode.AUTO,
                    "context_mode": ctx_mode,
                    "reason": "slash:/agent status",
                    "confidence": 1.0,
                    "task": _make_task(
                        message=message,
                        attachments=attachments,
                        intent=DeepLensIntent.CHAT,
                        execution_preference=ExecutionMode.AUTO,
                    ),
                },
                reply=f"Freeform agent mode is currently {status}.",
            )

        new_state = _copy_state(state)
        new_state.agent_mode_enabled = action == "on"

        if not remainder:
            status = "ON" if new_state.agent_mode_enabled else "OFF"
            return SlashCommandResult(
                state=new_state,
                decision={
                    "intent": DeepLensIntent.CHAT,
                    "execution_mode": ExecutionMode.AUTO,
                    "context_mode": ctx_mode,
                    "reason": f"slash:/agent {action}",
                    "confidence": 1.0,
                    "task": _make_task(
                        message=message,
                        attachments=attachments,
                        intent=DeepLensIntent.CHAT,
                        execution_preference=ExecutionMode.AUTO,
                    ),
                },
                reply=f"Freeform agent mode is now {status}.",
            )

        if new_state.agent_mode_enabled:
            return SlashCommandResult(
                state=new_state,
                decision={
                    "intent": DeepLensIntent.CHAT,
                    "execution_mode": ExecutionMode.LOOP,
                    "context_mode": ctx_mode,
                    "reason": "slash:/agent on task",
                    "confidence": 1.0,
                    "task": _make_task(
                        message=remainder,
                        attachments=attachments,
                        intent=DeepLensIntent.CHAT,
                        execution_preference=ExecutionMode.LOOP,
                    ),
                },
            )

        return SlashCommandResult(
            state=new_state,
            decision={
                "intent": DeepLensIntent.CHAT,
                "execution_mode": ExecutionMode.AUTO,
                "context_mode": ctx_mode,
                "reason": "slash:/agent off task",
                "confidence": 1.0,
                "task": _make_task(
                    message=message,
                    attachments=attachments,
                    intent=DeepLensIntent.CHAT,
                    execution_preference=ExecutionMode.AUTO,
                ),
            },
            reply="Freeform agent mode is OFF. Enable with `/agent on` first.",
        )

    if msg_l.startswith("/mode"):
        new_state = _copy_state(state)
        if " full" in f" {msg_l}":
            new_state.context_mode = "full"
            return SlashCommandResult(
                state=new_state,
                decision={
                    "intent": DeepLensIntent.CHAT,
                    "execution_mode": ExecutionMode.AUTO,
                    "context_mode": ContextMode.FULL,
                    "reason": "slash:/mode full",
                    "confidence": 1.0,
                    "task": _make_task(
                        message=message,
                        attachments=attachments,
                        intent=DeepLensIntent.CHAT,
                        execution_preference=ExecutionMode.AUTO,
                    ),
                },
                reply="Context mode set to FULL.",
            )
        if " lite" in f" {msg_l}":
            new_state.context_mode = "lite"
            return SlashCommandResult(
                state=new_state,
                decision={
                    "intent": DeepLensIntent.CHAT,
                    "execution_mode": ExecutionMode.AUTO,
                    "context_mode": ContextMode.LITE,
                    "reason": "slash:/mode lite",
                    "confidence": 1.0,
                    "task": _make_task(
                        message=message,
                        attachments=attachments,
                        intent=DeepLensIntent.CHAT,
                        execution_preference=ExecutionMode.AUTO,
                    ),
                },
                reply="Context mode set to LITE.",
            )
        return SlashCommandResult(
            state=state,
            decision={
                "intent": DeepLensIntent.CHAT,
                "execution_mode": ExecutionMode.AUTO,
                "context_mode": ctx_mode,
                "reason": "slash:/mode invalid",
                "confidence": 1.0,
                "task": _make_task(
                    message=message,
                    attachments=attachments,
                    intent=DeepLensIntent.CHAT,
                    execution_preference=ExecutionMode.AUTO,
                ),
            },
            reply="Usage: `/mode full` or `/mode lite`.",
        )

    return SlashCommandResult(
        state=state,
        decision={
            "intent": DeepLensIntent.CHAT,
            "execution_mode": ExecutionMode.AUTO,
            "context_mode": ctx_mode,
            "reason": "slash:unknown",
            "confidence": 1.0,
            "task": _make_task(
                message=message,
                attachments=attachments,
                intent=DeepLensIntent.CHAT,
                execution_preference=ExecutionMode.AUTO,
            ),
        },
        reply=(
            "Unknown command. Supported: "
            "`/lens`, `/simulate`, `/optimize`, `/viz`, `/debug`, `/chat`, "
            "`/agent on|off|status`, `/mode full`, `/mode lite`."
        ),
    )

def _state_sensitive_route(message: str, state: DeepLensState) -> RouterDecision | None:
    msg = (message or "").strip().lower()

    if state.pending_action and any(k in msg for k in ("cancel", "stop", "pause")):
        return {
            "intent": DeepLensIntent.CHAT,
            "execution_mode": ExecutionMode.AUTO,
            "context_mode": _default_context_mode(state),
            "reason": "state:pending_cancel",
            "confidence": 0.95,
            "task": _make_task(
                message=message,
                attachments=[],
                intent=DeepLensIntent.CHAT,
                execution_preference=ExecutionMode.AUTO,
            ),
        }

    if state.active_run_id and any(k in msg for k in ("cancel run", "stop run", "cancel optimization")):
        return {
            "intent": DeepLensIntent.CHAT,
            "execution_mode": ExecutionMode.LOOP,
            "context_mode": _default_context_mode(state),
            "reason": "state:cancel_active_run",
            "confidence": 0.98,
            "task": _make_task(
                message=message,
                attachments=[],
                intent=DeepLensIntent.CHAT,
                execution_preference=ExecutionMode.LOOP,
                parsed_args={"run_id": state.active_run_id, "operation": "cancel_run"},
            ),
        }

    if state.active_task and any(k in msg for k in ("continue", "resume", "run it", "try again", "fix that", "status")):
        task = DeepLensTask(**state.active_task)
        return {
            "intent": task.intent,
            "execution_mode": ExecutionMode.LOOP if state.agent_mode_enabled else ExecutionMode.AUTO,
            "context_mode": _default_context_mode(state),
            "reason": "state:continue_active_task",
            "confidence": 0.9,
            "task": task,
        }

    return None

def _fallback_intent(message: str) -> DeepLensIntent:
    msg = (message or "").lower()
    if any(k in msg for k in ("nan", "self-intersection", "explode", "diverge", "bad mtf", "bad rms", "fix")):
        return DeepLensIntent.DEBUG
    if any(k in msg for k in ("lens", "aperture", "focal", "surface", "glass")):
        return DeepLensIntent.LENS_DESIGN
    if any(k in msg for k in ("simulate", "ray trace", "trace", "propagation", "psf", "mtf", "spot")):
        return DeepLensIntent.SIMULATION
    if any(k in msg for k in ("optimize", "loss", "constraint", "search", "tune")):
        return DeepLensIntent.OPTIMIZATION
    if any(k in msg for k in ("visual", "plot", "render", "chart", "display", "viz")):
        return DeepLensIntent.VISUALIZATION
    return DeepLensIntent.CHAT

def _is_complex_request(message: str, state: DeepLensState) -> bool:
    if not state.agent_mode_enabled:
        return False
    
    msg = (message or "").strip().lower()
    if len(msg) < 40:
        return False
    sequence_hits = sum(
        1
        for k in (
            "then",
            "and then",
            "pipeline",
            "end-to-end",
            "iterate",
            "tradeoff",
            "after that",
            "step by step",
            "compare and improve",
        )
        if k in msg
    )
    domain_hits = sum(
        1
        for k in ("lens", "simulate", "optimization", "optimize", "visualization", "plot", "mtf", "psf", "spot")
        if k in msg
    )
    return sequence_hits >= 1 and domain_hits >= 2


async def _llm_route(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    state: DeepLensState,
    context: NodeContext,
) -> RouterDecision:
    llm = context.llm() 
    skills = context.skills()
    
    system_prompt = skills.compile_prompt(
        DEEPLENS_SKILL_ID,
        "deeplens.system",
        "deeplens.router",
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )

    user_prompt = (
        "Classify the user's request into one DeepLens intent and execution mode.\n\n"
        "Valid intents:\n"
        "- chat\n"
        "- lens_design\n"
        "- simulation\n"
        "- optimization\n"
        "- visualization\n"
        "- debug\n\n"
        "Valid execution modes:\n"
        "- auto\n"
        "- workflow\n"
        "- loop\n\n"
        # "Prefer workflow for rigid, well-scoped tasks like simulation/report/export.\n"
        "Prefer loop for multi-step, underspecified, exploratory, or debugging tasks.\n\n"
        f"Message:\n{message}\n\n"
        f"State:\n"
        f"- context_mode={state.context_mode}\n"
        f"- agent_mode_enabled={state.agent_mode_enabled}\n"
        f"- debug_heavy_runs_enabled={state.debug_heavy_runs_enabled}\n"
        f"- pending_action={state.pending_action or 'none'}\n"
        f"- last_intent={state.last_intent.value if state.last_intent else 'none'}\n"
        f"- attachments_count={len(attachments)}\n\n"
        "Return strict JSON with fields:\n"
        '- intent\n'
        '- execution_mode\n'
        '- reason\n'
        '- confidence\n'
        '- parsed_args\n'
        '- missing_fields\n'
    )

    print(f"🍎 System prompt:\n{system_prompt}\n")
    resp, _usage = await llm.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        output_format="json",
        json_schema=ROUTER_JSON_SCHEMA,
        schema_name="DeepLensRouteV2",
        strict_schema=True,
        validate_json=True,
        max_output_tokens=384,
    )

    obj = json.loads(resp) if isinstance(resp, str) else resp
    print(f"🍎 Router LLM response:\n{json.dumps(obj, indent=2)}\n")

    intent = DeepLensIntent(str(obj.get("intent", "chat")).strip().lower())
    execution_mode = ExecutionMode(str(obj.get("execution_mode", "auto")).strip().lower())
    parsed_args = obj.get("parsed_args") or {}
    missing_fields = obj.get("missing_fields") or []

    return {
        "intent": intent,
        "execution_mode": execution_mode,
        "context_mode": _default_context_mode(state),
        "reason": str(obj.get("reason") or "llm_route"),
        "confidence": float(obj.get("confidence") or 0.5),
        "task": _make_task(
            message=message,
            attachments=attachments,
            intent=intent,
            execution_preference=execution_mode,
            parsed_args=parsed_args,
            missing_fields=missing_fields,
        ),
    }

async def route_v2(
    *,
    message: str,
    attachments: list[dict[str, Any]],
    state: DeepLensState,
    context: NodeContext,
) -> RouteResult:
    slash = apply_slash_command(message, attachments, state)
    if slash is not None:
        return {
            "decision": slash.decision,
            "state": slash.state,
            "immediate_reply": slash.reply,
        }
    
    state_override = _state_sensitive_route(message, state)
    if state_override is not None:
        return {
            "decision": state_override,
            "state": state,
            "immediate_reply": None,
        }
    
    if _is_complex_request(message, state):
        intent = _fallback_intent(message)
        return {
            "decision": {
                "intent": intent,
                "execution_mode": ExecutionMode.LOOP,
                "context_mode": _default_context_mode(state),
                "reason": "complex_request_agent_mode",
                "confidence": 0.8,
                "task": _make_task(
                    message=message,
                    attachments=attachments,
                    intent=intent,
                    execution_preference=ExecutionMode.LOOP,
                ),
            },
            "state": state,
            "immediate_reply": None,
        }


    if context is not None:
        try:
            decision = await _llm_route(
                message=message,
                attachments=attachments,
                state=state,
                context=context,
            )
            return {
                "decision": decision,
                "state": state,
                "immediate_reply": None,
            }
        except Exception as e:
            context.logger().error(f"LLM routing failed, falling back to chat. Error: {e}")

    fallback_intent = _fallback_intent(message)
    fallback_mode = ExecutionMode.LOOP if (_is_complex_request(message, state) or fallback_intent == DeepLensIntent.DEBUG) else ExecutionMode.AUTO

    return {
        "decision": {
            "intent": fallback_intent,
            "execution_mode": fallback_mode,
            "context_mode": _default_context_mode(state),
            "reason": "fallback_route",
            "confidence": 0.55,
            "task": _make_task(
                message=message,
                attachments=attachments,
                intent=fallback_intent,
                execution_preference=fallback_mode,
            ),
        },
        "state": state,
        "immediate_reply": None,
    }