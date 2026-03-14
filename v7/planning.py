from __future__ import annotations

import json
import logging
from typing import Any

from v3.extraction import (
    attachment_suggests_lens,
    extract_analysis_request,
    extract_design_spec,
    extract_run_request,
    infer_export_formats,
)
from .policies import should_plan_artifact_delivery
from .tool_registry import get_tool_spec
from .types import ContextMode, DeepLensTask, Plan, PlanStatus, PlanStep, RuntimeState


logger = logging.getLogger("ag.deeplens.v7.planning")

CAPABILITY_TO_STEP = {
    "design": ("Create lens", "Create a starting lens from the user specification.", "dl.create_lens"),
    "load": ("Load lens", "Load the provided lens into the active session.", "dl.load_lens"),
    "analysis": ("Analyze lens", "Analyze the requested or active lens.", "dl.analysis"),
    "optimize": ("Optimize lens", "Submit the optimization workflow in the background.", "dl.optimize"),
    "export": ("Export artifacts", "Export the current lens artifacts.", "dl.export_lens"),
    "status": ("Check run status", "Check the active optimization run.", "ag.status"),
    "cancel": ("Cancel run", "Request cancellation of the active optimization run.", "ag.cancel"),
}


def decide_context_mode(message: str, state: RuntimeState) -> ContextMode:
    lowered = (message or "").lower()
    if "/mode full" in lowered:
        return ContextMode.FULL
    if "/mode lite" in lowered:
        return ContextMode.LITE
    return ContextMode(state.context_mode)


def _infer_capabilities(message: str, attachments: list[dict[str, Any]], state: RuntimeState) -> list[str]:
    lowered = (message or "").lower()
    capabilities: list[str] = []
    if any(token in lowered for token in ("cancel", "stop run", "abort")):
        capabilities.append("cancel")
    elif "status" in lowered or "check run" in lowered:
        capabilities.append("status")
    else:
        if any(token in lowered for token in ("design", "create lens", "build lens", "new lens")):
            capabilities.append("design")
        if any(token in lowered for token in ("analy", "spot", "mtf", "rms", "evaluate", "analysis")):
            capabilities.append("analysis")
        if any(token in lowered for token in ("optimize", "optimization", "improve")):
            capabilities.append("optimize")
        if any(token in lowered for token in ("export", "download", "save", "deliver", "send", "show", "zmx", "json")):
            capabilities.append("export")
    if not capabilities:
        if attachment_suggests_lens(attachments):
            capabilities.append("analysis")
        elif state.active_source_ref:
            capabilities.append("analysis")
        else:
            capabilities.append("explain")
    return capabilities


def _heuristic_task(message: str, attachments: list[dict[str, Any]], state: RuntimeState) -> DeepLensTask:
    capabilities = _infer_capabilities(message, attachments, state)
    design_spec = extract_design_spec(message)
    analysis_request = extract_analysis_request(message, attachments)
    run_request = extract_run_request(message)
    delivery_request = {"formats": infer_export_formats(message, None)}
    lens_source = {}
    if attachments:
        first = attachments[0]
        lens_source = {
            "artifact_id": first.get("artifact_id"),
            "uri": first.get("uri"),
            "name": first.get("name") or first.get("filename"),
        }
    response_request = {"mode": "explain" if capabilities == ["explain"] else "workflow"}
    return DeepLensTask(
        user_goal=message,
        attachments=list(attachments),
        requested_capabilities=capabilities,
        design_spec=design_spec,
        analysis_request=analysis_request,
        run_request=run_request,
        delivery_request=delivery_request,
        lens_source=lens_source,
        response_request=response_request,
    )


def _task_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "requested_capabilities": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["design", "analysis", "optimize", "export", "status", "cancel", "explain"],
                },
            },
            "design_spec_json": {"type": "string"},
            "analysis_request_json": {"type": "string"},
            "run_request_json": {"type": "string"},
            "delivery_request_json": {"type": "string"},
            "response_mode": {"type": "string", "enum": ["workflow", "explain"]},
        },
        "required": [
            "requested_capabilities",
            "design_spec_json",
            "analysis_request_json",
            "run_request_json",
            "delivery_request_json",
            "response_mode",
        ],
        "additionalProperties": False,
    }


async def build_task(message: str, attachments: list[dict[str, Any]], state: RuntimeState, context: Any) -> DeepLensTask:
    fallback = _heuristic_task(message, attachments, state)
    llm = context.llm("fast")
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        "aethergraph-agent-deeplens-v7",
        "deeplens.system",
        "deeplens.extract",
        separator="\n\n",
        fallback_keys=["deeplens.system", "deeplens.parse"],
    )
    payload = {
        "message": message,
        "attachments": attachments,
        "active_source_ref": state.active_source_ref,
        "active_run_id": state.active_run_id,
    }
    try:
        response, _usage = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=_task_schema(),
            schema_name="DeepLensV7Task",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=500,
        )
        obj = json.loads(response) if isinstance(response, str) else response
        task = DeepLensTask.from_dict(fallback.to_dict()) or fallback
        task.requested_capabilities = list(obj.get("requested_capabilities") or fallback.requested_capabilities)
        task.response_request = {"mode": str(obj.get("response_mode") or "workflow")}
        for attr, key in (
            ("design_spec", "design_spec_json"),
            ("analysis_request", "analysis_request_json"),
            ("run_request", "run_request_json"),
            ("delivery_request", "delivery_request_json"),
        ):
            raw = obj.get(key) or "{}"
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                setattr(task, attr, parsed)
        if attachments and not task.lens_source:
            first = attachments[0]
            task.lens_source = {
                "artifact_id": first.get("artifact_id"),
                "uri": first.get("uri"),
                "name": first.get("name") or first.get("filename"),
            }
        return task
    except Exception:
        logger.warning("deeplens_v7: task extraction llm failed; using heuristic fallback", exc_info=True)
        return fallback


def _needs_lens_source(task: DeepLensTask) -> bool:
    return not (task.lens_source or task.attachments or task.response_request.get("mode") == "explain")


def _needs_create_before(task: DeepLensTask, capability: str) -> bool:
    if capability not in {"analysis", "optimize", "export"}:
        return False
    if not _needs_lens_source(task):
        return False
    spec = task.design_spec or {}
    return spec.get("fov") is not None and spec.get("fnum") is not None


def _send_step_for(tool_name: str, idx: int) -> PlanStep | None:
    spec = get_tool_spec(tool_name)
    if spec.name == "ag.send_file":
        selector = {"kind": "file"}
        title = "Send artifacts"
        goal = "Send the produced files to the UI."
    elif spec.name == "ag.send_image":
        selector = {"kind": "image"}
        title = "Send preview"
        goal = "Send the produced preview image to the UI."
    else:
        return None
    return PlanStep(
        step_id=f"step_{idx}",
        title=title,
        goal=goal,
        tool_name=tool_name,
        arg_overrides={"artifact_selector": selector},
        tool_policy_id=spec.tool_policy_id,
        interaction_policy_id=spec.interaction_policy_id or "missing_input",
    )


def _append_artifact_steps(steps: list[PlanStep], task: DeepLensTask, source_tool_name: str, idx: int) -> int:
    spec = get_tool_spec(source_tool_name)
    if not should_plan_artifact_delivery(task=task, tool_spec=spec):
        return idx
    send_step = _send_step_for("ag.send_file", idx)
    if send_step is not None:
        steps.append(send_step)
        idx += 1
    return idx


def _step_for_capability(capability: str, idx: int, task: DeepLensTask) -> PlanStep:
    title, goal, tool_name = CAPABILITY_TO_STEP[capability]
    spec = get_tool_spec(tool_name)
    arg_overrides: dict[str, Any] = {}
    if tool_name == "dl.create_lens":
        arg_overrides["design_spec"] = dict(task.design_spec)
        if task.delivery_request.get("formats"):
            arg_overrides["formats"] = list(task.delivery_request.get("formats") or [])
    elif tool_name == "dl.analysis":
        arg_overrides["analysis_request"] = dict(task.analysis_request)
    elif tool_name == "dl.optimize":
        arg_overrides["run_request"] = dict(task.run_request)
    elif tool_name == "dl.export_lens":
        arg_overrides["formats"] = list(task.delivery_request.get("formats") or [])
    return PlanStep(
        step_id=f"step_{idx}",
        title=title,
        goal=goal,
        tool_name=tool_name,
        arg_overrides=arg_overrides,
        tool_policy_id=spec.tool_policy_id,
        interaction_policy_id=spec.interaction_policy_id or "missing_input",
    )


def _fallback_plan(task: DeepLensTask) -> Plan:
    steps: list[PlanStep] = []
    idx = 1
    created = False
    for capability in task.requested_capabilities:
        if capability == "explain":
            continue
        if _needs_create_before(task, capability) and not created:
            create_step = _step_for_capability("design", idx, task)
            steps.append(create_step)
            idx += 1
            idx = _append_artifact_steps(steps, task, create_step.tool_name, idx)
            created = True
        step = _step_for_capability(capability, idx, task)
        steps.append(step)
        idx += 1
        idx = _append_artifact_steps(steps, task, step.tool_name, idx)
        if capability == "optimize":
            break
    status = PlanStatus.READY if steps else PlanStatus.COMPLETED
    return Plan(goal=task.user_goal, steps=steps, status=status, rationale="Deterministic DeepLens task plan.")


def _plan_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "rationale": {"type": "string"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "goal": {"type": "string"},
                        "tool_name": {
                            "type": "string",
                            "enum": [
                                "dl.create_lens",
                                "dl.load_lens",
                                "dl.analysis",
                                "dl.optimize",
                                "dl.export_lens",
                                "ag.send_file",
                                "ag.send_image",
                                "ag.status",
                                "ag.cancel",
                            ],
                        },
                        "arg_overrides_json": {"type": "string"},
                    },
                    "required": ["title", "goal", "tool_name", "arg_overrides_json"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["rationale", "steps"],
        "additionalProperties": False,
    }


async def draft_plan(
    *,
    task: DeepLensTask,
    state: RuntimeState,
    context_bundle: Any,
    context: Any,
) -> Plan:
    if task.response_request.get("mode") == "explain":
        return Plan(goal=task.user_goal, steps=[], status=PlanStatus.COMPLETED, rationale="Explanation-only request.")

    fallback = _fallback_plan(task)
    llm = context.llm("default")
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        "aethergraph-agent-deeplens-v7",
        "deeplens.system",
        "deeplens.plan",
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )
    payload = {
        "goal": task.user_goal,
        "requested_capabilities": task.requested_capabilities,
        "design_spec": task.design_spec,
        "analysis_request": task.analysis_request,
        "run_request": task.run_request,
        "delivery_request": task.delivery_request,
        "lens_source": task.lens_source,
        "active_source_ref": state.active_source_ref,
        "active_run_id": state.active_run_id,
        "working_state": context_bundle.working_state,
        "fallback_steps": [step.to_dict() for step in fallback.steps],
    }
    try:
        response, _usage = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=_plan_schema(),
            schema_name="DeepLensV7Plan",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=2048,
            reasoning_effort="low",
        )
        obj = json.loads(response) if isinstance(response, str) else response
        steps: list[PlanStep] = []
        for idx, item in enumerate(obj.get("steps", []), start=1):
            tool_name = str(item["tool_name"])
            spec = get_tool_spec(tool_name)
            raw_overrides = str(item.get("arg_overrides_json") or "{}")
            arg_overrides = json.loads(raw_overrides)
            if not isinstance(arg_overrides, dict):
                arg_overrides = {}
            steps.append(
                PlanStep(
                    step_id=f"step_{idx}",
                    title=str(item["title"]),
                    goal=str(item["goal"]),
                    tool_name=tool_name,
                    arg_overrides=arg_overrides,
                    tool_policy_id=spec.tool_policy_id,
                    interaction_policy_id=spec.interaction_policy_id or "missing_input",
                )
            )
        if steps:
            return Plan(
                goal=task.user_goal,
                steps=steps,
                status=PlanStatus.READY,
                rationale=str(obj.get("rationale") or fallback.rationale),
            )
    except Exception:
        logger.warning("deeplens_v7: planner llm failed; using deterministic fallback", exc_info=True)
    return fallback
