# field_provenance.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .types import FieldSource, FieldValue, TaskFrame


@dataclass
class ProvenanceRecord:
    field_name: str
    value: Any
    source: str
    confidence: float
    inferred: bool
    confirmed: bool
    notes: list[str]


def make_field_value(
    *,
    value: Any,
    source: FieldSource,
    confidence: float,
    inferred: bool = False,
    confirmed: bool = False,
    notes: list[str] | None = None,
) -> FieldValue:
    return FieldValue(
        value=value,
        source=source,
        confidence=confidence,
        inferred=inferred,
        confirmed=confirmed,
        notes=list(notes or []),
    )


def set_field_value(
    task: TaskFrame,
    field_name: str,
    value: Any,
    *,
    source: FieldSource,
    confidence: float,
    inferred: bool = False,
    confirmed: bool = False,
    notes: list[str] | None = None,
    overwrite: bool = False,
) -> None:
    if value is None:
        return

    existing = task.field_map.get(field_name)
    if existing is not None and not overwrite:
        if existing.confirmed:
            return
        if existing.confidence >= confidence and existing.value not in (None, "", []):
            return

    task.field_map[field_name] = make_field_value(
        value=value,
        source=source,
        confidence=confidence,
        inferred=inferred,
        confirmed=confirmed,
        notes=notes,
    )


def merge_field_value(
    task: TaskFrame,
    field_name: str,
    incoming: FieldValue,
    *,
    overwrite: bool = False,
) -> None:
    existing = task.field_map.get(field_name)
    if existing is None:
        task.field_map[field_name] = incoming
        return

    if overwrite:
        task.field_map[field_name] = incoming
        return

    if existing.confirmed:
        return

    if incoming.confirmed and not existing.confirmed:
        task.field_map[field_name] = incoming
        return

    if incoming.confidence > existing.confidence:
        task.field_map[field_name] = incoming
        return


def confirm_field(task: TaskFrame, field_name: str) -> None:
    fv = task.field_map.get(field_name)
    if fv is None:
        return
    fv.confirmed = True


def append_field_note(task: TaskFrame, field_name: str, note: str) -> None:
    fv = task.field_map.get(field_name)
    if fv is None:
        return
    fv.notes.append(note)


def field_value(task: TaskFrame, field_name: str, default: Any = None) -> Any:
    fv = task.field_map.get(field_name)
    if fv is None:
        return default
    return fv.value


def has_field(task: TaskFrame, field_name: str) -> bool:
    fv = task.field_map.get(field_name)
    if fv is None:
        return False
    return fv.value not in (None, "", [])


def export_provenance(task: TaskFrame) -> list[ProvenanceRecord]:
    out: list[ProvenanceRecord] = []
    for field_name, fv in task.field_map.items():
        out.append(
            ProvenanceRecord(
                field_name=field_name,
                value=fv.value,
                source=fv.source.value if isinstance(fv.source, FieldSource) else str(fv.source),
                confidence=float(fv.confidence),
                inferred=bool(fv.inferred),
                confirmed=bool(fv.confirmed),
                notes=list(fv.notes or []),
            )
        )
    return out