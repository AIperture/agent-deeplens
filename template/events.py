from __future__ import annotations

from typing import Any

from . import policy


async def emit_agent_event(chan: Any, *, event_type: str, data: dict[str, Any] | None = None) -> None:
    await chan.send_text(
        f"[{event_type}]",
        memory_log=True,
        memory_tags=[f"ag.{policy.AGENT_ID}.event", f"event:{event_type}"],
        memory_data={"event_type": event_type, **(data or {})},
        memory_severity=1,
    )


async def emit_loop_update(
    chan: Any,
    *,
    phase: str,
    phase_status: str,
    label: str,
    detail: str,
) -> None:
    await chan.send_phase(phase=phase, status=phase_status, label=label, detail=detail)
