from __future__ import annotations

import json
from typing import Any

from v3.extraction import (
    attachment_suggests_lens,
    extract_analysis_request,
    extract_design_spec,
    extract_run_request,
    infer_export_formats,
    missing_design_fields,
)
from .tool_registry import get_tool_spec
from .types import ContextMode, DeepLensTask, Plan, PlanStatus, PlanStep, RuntimeState


CAPABILITY_TO_STEP = {
    "design": ("Create lens", "Create a starting lens from the user specification.", "dl.create_lens"),
    "analysis": ("Analyze lens", "Analyze the requested or active lens.", "dl.analysis"),
    "optimize": ("Optimize lens", "Submit the optimization workflow in the background.", "ag.spawn_graph"),
    "export": ("Export artifacts", "Export or deliver the current lens artifacts.", "dl.export_lens"),
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
        if any(token in lowered for token in ("export", "download", "save", "deliver", "zmx", "json")):
            capabilities.append("export")
    if not capabilities:
        if attachment_suggests_lens(attachments):
            capabilities.append("analysis")
        elif state.active_source_ref:
            capabilities.append("analysis")
        else:
            capabilities.append("explain")
    return capabilities


def build_task(message: str, attachments: list[dict[str, Any]], state: RuntimeState) -> DeepLensTask:
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


def _required_fields(capability: str, task: DeepLensTask) -> list[str]:
    if capability == "design":
        return missing_design_fields(task.design_spec)
    if capability in {"analysis", "optimize", "export"}:
        if task.lens_source or task.attachments:
            return []
        return ["lens_source"]
    if capability in {"status", "cancel"}:
        return [] if task.run_request.get("run_id") else ["run_id"]
    return []


def _fallback_plan(task: DeepLensTask) -> Plan:
    steps: list[PlanStep] = []
    idx = 1
    for capability in task.requested_capabilities:
        if capability == "explain":
            continue
        title, goal, tool_name = CAPABILITY_TO_STEP[capability]
        spec = get_tool_spec(tool_name)
        steps.append(
            PlanStep(
                step_id=f"step_{idx}",
                title=title,
                goal=goal,
                tool_name=tool_name,
                required_fields=_required_fields(capability, task),
                arg_overrides={"graph_id": "deeplens_v7_optimize_workflow"} if tool_name == "ag.spawn_graph" else {},
                tool_policy_id=spec.tool_policy_id,
                interaction_policy_id=spec.interaction_policy_id or "missing_input",
            )
        )
        idx += 1
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
                                "dl.analysis",
                                "dl.export_lens",
                                "ag.spawn_graph",
                                "ag.status",
                                "ag.cancel",
                            ],
                        },
                    },
                    "required": ["title", "goal", "tool_name"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["rationale", "steps"],
        "additionalProperties": False,
    }


def _capability_for_tool(tool_name: str) -> str:
    mapping = {
        "dl.create_lens": "design",
        "dl.analysis": "analysis",
        "dl.export_lens": "export",
        "ag.spawn_graph": "optimize",
        "ag.status": "status",
        "ag.cancel": "cancel",
    }
    return mapping[tool_name]


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
        "active_source_ref": state.active_source_ref,
        "active_run_id": state.active_run_id,
        "working_state": context_bundle.working_state,
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
            max_output_tokens=500,
            reasoning_effort="low",
        )
        obj = json.loads(response) if isinstance(response, str) else response
        steps: list[PlanStep] = []
        for idx, item in enumerate(obj.get("steps", []), start=1):
            tool_name = str(item["tool_name"])
            capability = _capability_for_tool(tool_name)
            spec = get_tool_spec(tool_name)
            steps.append(
                PlanStep(
                    step_id=f"step_{idx}",
                    title=str(item["title"]),
                    goal=str(item["goal"]),
                    tool_name=tool_name,
                    required_fields=_required_fields(capability, task),
                    arg_overrides={"graph_id": "deeplens_v7_optimize_workflow"} if tool_name == "ag.spawn_graph" else {},
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
        context.logger().warning("deeplens_v7: planner llm failed; using deterministic fallback", exc_info=True)
    return fallback
