"""Event emission helpers wrapping context.channel().

Extracts v6's inline _emit_loop_update into a dedicated module with tags
derived from policy.AGENT_ID.
"""
from __future__ import annotations

from typing import Any

from . import policy


async def emit_phase(
    chan: Any,
    *,
    phase: str,
    status: str,
    label: str,
    detail: str = "",
) -> None:
    """Emit a phase update to the UI channel."""
    await chan.send_phase(phase=phase, status=status, label=label, detail=detail)


async def emit_progress(
    chan: Any,
    *,
    step_index: int,
    kind: str,
    note_text: str | None = None,
    tool_name: str | None = None,
    status: str | None = None,
) -> None:
    """Emit a progress note with memory logging."""
    if not note_text:
        return
    await chan.send_text(
        note_text,
        memory_log=True,
        memory_tags=[f"ag.{policy.AGENT_ID}.progress", f"loop_kind:{kind}"],
        memory_data={
            "step_index": step_index,
            "kind": kind,
            "tool_name": tool_name,
            "status": status or "active",
        },
        memory_severity=1,
    )


async def emit_agent_event(
    chan: Any,
    *,
    event_type: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Emit a generic agent event for major state changes.

    event_type examples: "task_started", "tool_completed", "tool_failed",
    "recovery_attempted", "agenda_replanned", "task_completed", "task_failed".
    """
    await chan.send_text(
        f"[{event_type}]" + (f" {data}" if data else ""),
        memory_log=True,
        memory_tags=[f"ag.{policy.AGENT_ID}.event", f"event:{event_type}"],
        memory_data={"event_type": event_type, **(data or {})},
        memory_severity=2,
    )


async def emit_loop_update(
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
    """Combined phase + progress emission (convenience wrapper)."""
    await emit_phase(chan, phase=phase, status=phase_status, label=label, detail=detail)
    if note_text:
        await emit_progress(
            chan,
            step_index=step_index,
            kind=kind,
            note_text=note_text,
            tool_name=tool_name,
            status=loop_status or phase_status,
        )
