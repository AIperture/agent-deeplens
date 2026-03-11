from __future__ import annotations

import os
import re
from typing import Any

from .types import DeepLensState, TurnEnvelope


_LENS_EXTENSIONS = {
    ".json",
    ".zmx",
    ".seq",
    ".txt",
    ".yaml",
    ".yml",
}

_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
}

_DATA_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".npz",
    ".npy",
}


def _clean_message_text(message: str | None) -> str:
    """
    Normalize raw message text for downstream interpretation.

    Keep this conservative:
    - trim leading/trailing whitespace
    - normalize repeated whitespace inside lines
    - preserve line breaks
    """
    raw = (message or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""

    cleaned_lines: list[str] = []
    for line in raw.split("\n"):
        cleaned = re.sub(r"\s+", " ", line).strip()
        cleaned_lines.append(cleaned)

    # Keep non-empty structure, but avoid giant empty gaps.
    return "\n".join(line for line in cleaned_lines if line)



def _extract_explicit_command(cleaned_message: str) -> str | None:
    """
    Detect a top-level slash command like:
    /analysis
    /design ...
    /optimize
    /mode full

    Returns the normalized command token or None.
    """
    if not cleaned_message.startswith("/"):
        return None

    first_line = cleaned_message.split("\n", 1)[0].strip()
    if not first_line:
        return None

    parts = first_line.split()
    if not parts:
        return None

    head = parts[0].lower()

    if head in {"/analysis", "/design", "/optimize"}:
        return head

    if head == "/mode":
        if len(parts) >= 2:
            return f"/mode {parts[1].lower()}"
        return "/mode"

    return head


def _guess_attachment_kind(filename: str, mime_type: str | None = None) -> str:
    ext = os.path.splitext(filename or "")[1].lower()

    if ext in _LENS_EXTENSIONS:
        return "lens"
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in _DATA_EXTENSIONS:
        return "data"

    mt = (mime_type or "").lower()
    if mt.startswith("image/"):
        return "image"
    if "json" in mt or "yaml" in mt or "text" in mt:
        return "document"

    return "unknown"


def _summarize_attachment(item: dict[str, Any]) -> dict[str, Any]:
    """
    Convert one raw attachment dict into a smaller normalized summary.

    Be tolerant of different frontend payload shapes.
    """
    filename = (
        item.get("filename")
        or item.get("name")
        or item.get("title")
        or ""
    )
    mime_type = item.get("mime_type") or item.get("content_type") or ""
    uri = item.get("uri") or item.get("url") or item.get("signed_url") or ""
    ext = os.path.splitext(filename)[1].lower()

    summary = {
        "name": filename,
        "filename": filename,
        "ext": ext,
        "mime_type": mime_type,
        "uri": uri,
        "kind": _guess_attachment_kind(filename, mime_type),
        "artifact_id": item.get("artifact_id"),
        "source": item.get("source") or "upload",
        "raw": item,
    }

    return summary



def _normalize_attachments(attachments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    items = attachments or []
    return [_summarize_attachment(item) for item in items if isinstance(item, dict)]



def _build_ui_hints(
    *,
    cleaned_message: str,
    attachments: list[dict[str, Any]],
    user_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Collect lightweight non-semantic hints for later interpretation.

    This is intentionally shallow. Do not infer intent here.
    """
    msg_lower = cleaned_message.lower()
    user_meta = user_meta or {}

    lens_attachments = [a for a in attachments if a.get("kind") == "lens"]
    image_attachments = [a for a in attachments if a.get("kind") == "image"]
    data_attachments = [a for a in attachments if a.get("kind") == "data"]

    hints = {
        "has_attachments": bool(attachments),
        "attachment_count": len(attachments),
        "has_lens_attachment": bool(lens_attachments),
        "has_image_attachment": bool(image_attachments),
        "has_data_attachment": bool(data_attachments),
        "mentions_status": any(word in msg_lower for word in ["status", "progress", "running"]),
        "mentions_cancel": any(word in msg_lower for word in ["cancel", "stop", "abort", "terminate"]),
        "mentions_export": any(word in msg_lower for word in ["export", "save", "download"]),
        "mentions_analysis": any(word in msg_lower for word in ["analyze", "analysis", "mtf", "spot", "psf"]),
        "mentions_design": any(word in msg_lower for word in ["design", "lens", "fov", "f/#", "fnum", "focal"]),
        "mentions_optimize": any(word in msg_lower for word in ["optimize", "optimization", "improve"]),
        "run_completed_event": bool(user_meta.get("type") == "run_completed" and user_meta.get("run_id")),
        "user_meta_type": user_meta.get("type"),
    }
    return hints


def _collect_active_refs(state: DeepLensState) -> tuple[list[str], dict[str, Any], str | None]:
    """
    Collect lightweight active references from state.

    Keep this minimal and descriptive; interpretation will decide whether they matter.
    """
    refs: list[str] = []

    if state.active_lens_ref:
        refs.append(state.active_lens_ref)

    for artifact in state.last_artifacts[-5:]:
        ref = artifact.get("artifact_id") or artifact.get("uri") or artifact.get("name")
        if ref:
            refs.append(str(ref))

    # De-duplicate while preserving order.
    deduped_refs: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if ref not in seen:
            deduped_refs.append(ref)
            seen.add(ref)

    return deduped_refs, dict(state.active_source_ref or {}), state.active_run_id



def normalize_turn(
    *,
    message: str,
    attachments: list[dict[str, Any]] | None,
    state: DeepLensState,
    user_meta: dict[str, Any] | None = None,
) -> TurnEnvelope:
    """
    Build the normalized input envelope for v4.

    Responsibilities:
    - normalize message text
    - detect explicit slash command
    - summarize attachments
    - pass through user metadata
    - collect lightweight state references

    Non-responsibilities:
    - routing
    - task selection
    - tool selection
    - semantic repair
    """
    cleaned_message = _clean_message_text(message)
    normalized_attachments = _normalize_attachments(attachments)
    explicit_command = _extract_explicit_command(cleaned_message)
    active_refs, active_source_ref, active_run_id = _collect_active_refs(state)
    ui_hints = _build_ui_hints(
        cleaned_message=cleaned_message,
        attachments=normalized_attachments,
        user_meta=user_meta,
    )

    return TurnEnvelope(
        raw_message=message or "",
        cleaned_message=cleaned_message,
        attachments=normalized_attachments,
        explicit_command=explicit_command,
        ui_hints=ui_hints,
        user_meta=user_meta or {},
        active_refs=active_refs,
        active_source_ref=active_source_ref,
        active_run_id=active_run_id,
    )