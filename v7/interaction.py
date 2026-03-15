from __future__ import annotations

import json
import logging
from typing import Any

from .field_specs import describe_missing_fields, render_parse_contract
from .parse import (
    build_extraction_schema,
    extract_deterministic_inputs,
    merge_task_update,
    parse_llm_json_response,
    render_missing_fields_prompt,
)
from .types import DeepLensTask, MissingFieldInfo, RuntimeState


logger = logging.getLogger("ag.deeplens.v7.interaction")


def build_missing_prompt(*, tool_name: str, missing_fields: list[str], missing_details: list[MissingFieldInfo]) -> str:
    if missing_details:
        rendered = render_missing_fields_prompt(
            missing_paths=[item.path for item in missing_details],
            tool_name=tool_name,
        )
        examples: list[str] = []
        for item in missing_details:
            examples.extend(item.examples[:1])
        if examples:
            rendered += "\nExamples: " + "; ".join(dict.fromkeys(examples))
        return rendered
    return render_missing_fields_prompt(missing_paths=missing_fields, tool_name=tool_name)


def parse_approval_response(message: str) -> bool:
    lowered = (message or "").strip().lower()
    return lowered in {"y", "yes", "approve", "approved", "ok", "okay", "continue", "go ahead"}


async def ask_for_missing_inputs(
    *,
    tool_name: str,
    missing_fields: list[str],
    missing_details: list[MissingFieldInfo] | None = None,
    task: DeepLensTask,
    context: Any,
) -> tuple[str, list[dict[str, Any]]]:
    del task
    prompt = build_missing_prompt(
        tool_name=tool_name,
        missing_fields=missing_fields,
        missing_details=missing_details or describe_missing_fields(missing_fields),
    )
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

async def apply_user_inputs(
    *,
    task: DeepLensTask,
    text: str,
    attachments: list[dict[str, Any]],
    missing_fields: list[str],
    state: RuntimeState,
    context: Any,
) -> DeepLensTask:
    deterministic = extract_deterministic_inputs(message=text, attachments=attachments, state=state)
    updated = DeepLensTask.from_dict(task.to_dict()) or task
    updated.attachments.extend(attachments)
    llm = context.llm("fast")
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        "aethergraph-agent-deeplens-v7",
        "deeplens.system",
        "deeplens.parse",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )
    system_prompt = f"{system_prompt}\n\n{render_parse_contract()}"
    payload = {
        "user_reply": text,
        "attachments": attachments,
        "missing_fields": missing_fields,
        "missing_field_details": [item.__dict__ for item in describe_missing_fields(missing_fields)],
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
            json_schema=build_extraction_schema(include_capabilities=False),
            schema_name="DeepLensV7UserReply",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=1024,
        )
        llm_payload = parse_llm_json_response(response)
        return merge_task_update(
            base_task=updated,
            deterministic=deterministic,
            llm_payload=llm_payload,
            attachments=[],
            state=state,
            include_capabilities=False,
        )
    except Exception:
        logger.warning("deeplens_v7: user-input extraction llm failed; using heuristic fallback", exc_info=True)
    return merge_task_update(
        base_task=updated,
        deterministic=deterministic,
        llm_payload=None,
        attachments=[],
        state=state,
        include_capabilities=False,
    )
