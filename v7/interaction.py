from __future__ import annotations

import json
import logging
from typing import Any

from v3.extraction import extract_design_spec, extract_run_request, infer_analysis_mode, infer_export_formats
from .types import DeepLensTask


logger = logging.getLogger("ag.deeplens.v7.interaction")


def build_missing_prompt(missing_fields: list[str]) -> str:
    labels = {
        "fov": "field of view (fov)",
        "fnum": "f-number (fnum)",
        "foclen_or_imgh": "either focal length or image height",
        "lens_source": "a lens file (.json or .zmx) or an existing active lens",
        "run_id": "the run id",
    }
    rendered = [labels.get(field_name, field_name) for field_name in missing_fields]
    if not rendered:
        return "What should I fill in before continuing?"
    if len(rendered) == 1:
        return f"I need {rendered[0]} before continuing."
    return f"I need these inputs before continuing: {', '.join(rendered)}."


def parse_approval_response(message: str) -> bool:
    lowered = (message or "").strip().lower()
    return lowered in {"y", "yes", "approve", "approved", "ok", "okay", "continue", "go ahead"}


async def ask_for_missing_inputs(
    *,
    missing_fields: list[str],
    task: DeepLensTask,
    context: Any,
) -> tuple[str, list[dict[str, Any]]]:
    del task
    prompt = build_missing_prompt(missing_fields)
    channel = context.channel("ui:session")
    if hasattr(channel, "ask_text_or_files"):
        reply = await channel.ask_text_or_files(prompt=prompt)
        text = str(reply.get("text") or "")
        files = list(reply.get("files") or [])
        return text, files
    text = await channel.ask_text(prompt=prompt)
    return str(text or ""), []


async def ask_for_approval(*, prompt: str, context: Any) -> bool:
    response = await context.channel("ui:session").ask_approval(
        prompt=prompt,
        options=["Approve", "Reject"],
    )
    if isinstance(response, dict):
        return bool(response.get("approved"))
    return parse_approval_response(str(response))


async def confirm_plan(*, summary: str, prompt: str, context: Any) -> bool:
    channel = context.channel("ui:session")
    await channel.send_text(summary)
    response = await channel.ask_approval(
        prompt=prompt,
        options=["Confirm", "Cancel"],
    )
    if isinstance(response, dict):
        return bool(response.get("approved"))
    return parse_approval_response(str(response))


def _apply_heuristics(task: DeepLensTask, text: str, attachments: list[dict[str, Any]]) -> DeepLensTask:
    updated = DeepLensTask.from_dict(task.to_dict()) or task
    updated.attachments.extend(attachments)
    inferred_design = extract_design_spec(text)
    for key, value in inferred_design.items():
        updated.design_spec.setdefault(key, value)
    updated.analysis_request = infer_analysis_mode(text, updated.analysis_request)
    updated.delivery_request["formats"] = infer_export_formats(text, updated.delivery_request.get("formats"))
    updated.run_request.update(extract_run_request(text))
    if attachments and not updated.lens_source:
        first = attachments[0]
        updated.lens_source = {
            "artifact_id": first.get("artifact_id"),
            "uri": first.get("uri"),
            "name": first.get("name") or first.get("filename"),
        }
    return updated


def _parse_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "design_spec_json": {"type": "string"},
            "analysis_request_json": {"type": "string"},
            "run_request_json": {"type": "string"},
            "delivery_request_json": {"type": "string"},
        },
        "required": [
            "design_spec_json",
            "analysis_request_json",
            "run_request_json",
            "delivery_request_json",
        ],
        "additionalProperties": False,
    }


async def apply_user_inputs(
    *,
    task: DeepLensTask,
    text: str,
    attachments: list[dict[str, Any]],
    context: Any,
) -> DeepLensTask:
    updated = _apply_heuristics(task, text, attachments)
    llm = context.llm("fast")
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        "aethergraph-agent-deeplens-v7",
        "deeplens.system",
        "deeplens.parse",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )
    payload = {
        "user_reply": text,
        "attachments": attachments,
        "current_design_spec": updated.design_spec,
        "current_analysis_request": updated.analysis_request,
        "current_run_request": updated.run_request,
        "current_delivery_request": updated.delivery_request,
    }
    try:
        response, _usage = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=_parse_schema(),
            schema_name="DeepLensV7UserReply",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=1024,
        )
        obj = json.loads(response) if isinstance(response, str) else response
        for attr, key in (
            ("design_spec", "design_spec_json"),
            ("analysis_request", "analysis_request_json"),
            ("run_request", "run_request_json"),
            ("delivery_request", "delivery_request_json"),
        ):
            raw = obj.get(key) or "{}"
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                setattr(updated, attr, parsed)
    except Exception:
        logger.warning("deeplens_v7: user-input extraction llm failed; using heuristic fallback", exc_info=True)
    return updated
