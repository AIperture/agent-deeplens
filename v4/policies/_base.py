# policies/_base.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PolicyDecision:
    matched: bool = False
    rule_name: str = ""
    priority: int = 0
    notes: list[str] = field(default_factory=list)
    updates: dict[str, Any] = field(default_factory=dict)