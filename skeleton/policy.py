"""Centralized policy: all tunable knobs for the skeleton agent.

Vertical agents override this single file to customize behavior.
"""
from __future__ import annotations

from .types import ContextMode, TaskShape


# ---------------------------------------------------------------------------
# Agent identity
# ---------------------------------------------------------------------------
AGENT_ID = "skeleton_agent"
AGENT_TITLE = "Skeleton Agent"
AGENT_SHORT_DESCRIPTION = "A minimal agenda-driven agent skeleton for demo and prototyping."
AGENT_DESCRIPTION = (
    "Domain-agnostic agent that routes user requests, builds action agendas, "
    "dispatches tools, and recovers from errors. Ships with fake tools for testing."
)
STATE_KEY = "skeleton_state_v1"
SKILL_ID = "skeleton-agent-v1"

# ---------------------------------------------------------------------------
# Step budgets per task shape
# ---------------------------------------------------------------------------
MAX_STEPS: dict[TaskShape, int] = {
    TaskShape.DIRECT_ANSWER: 1,
    TaskShape.SINGLE_ACTION: 4,
    TaskShape.MULTI_STEP: 8,
    TaskShape.UNSUPPORTED: 1,
}

# ---------------------------------------------------------------------------
# Recovery limits
# ---------------------------------------------------------------------------
MAX_RETRIES_PER_TOOL = 2
MAX_RECOVERY_ATTEMPTS = 2

# ---------------------------------------------------------------------------
# Domain & capability mapping
# ---------------------------------------------------------------------------
DOMAINS: list[str] = ["general"]

# Maps each domain to capability keywords that the router scans for in user text
DOMAIN_CAPABILITY_MAP: dict[str, list[str]] = {
    "general": ["calculate", "search", "report"],
}

# Maps a detected capability to the tool that executes it
CAPABILITY_TOOL_MAP: dict[str, str] = {
    "calculate": "calculate",
    "search": "search_knowledge",
    "report": "generate_report",
}

# Capabilities that need explicit user approval before execution
APPROVAL_REQUIRED: set[str] = set()

# Ordering rules: (first, second) means first must run before second
SEQUENCING_RULES: list[tuple[str, str]] = [
    ("calculate", "report"),
    ("search", "report"),
]

# ---------------------------------------------------------------------------
# Keyword patterns for capability detection (used by router + planner)
# Each entry: (capability_name, list_of_trigger_keywords)
# ---------------------------------------------------------------------------
CAPABILITY_PATTERNS: list[tuple[str, list[str]]] = [
    ("calculate", ["calculate", "compute", "add", "subtract", "multiply", "divide", "sum", "math"]),
    ("search", ["search", "find", "look up", "lookup", "query", "knowledge"]),
    ("report", ["report", "summarize", "summary", "compile", "generate report"]),
]

# ---------------------------------------------------------------------------
# Context policy
# ---------------------------------------------------------------------------
DEFAULT_CONTEXT_MODE = ContextMode.LITE
LITE_CHAT_LIMIT = 6
FULL_CHAT_LIMIT = 20

# ---------------------------------------------------------------------------
# Loop trace limits
# ---------------------------------------------------------------------------
LOOP_TRACE_LIMIT = 50
LOOP_HISTORY_LIMIT = 6
ATTEMPT_HISTORY_LIMIT = 20
FAILURE_HISTORY_LIMIT = 20

# ---------------------------------------------------------------------------
# Slash commands (vertical agents add their own here)
# ---------------------------------------------------------------------------
SLASH_COMMANDS: list[dict[str, str]] = [
    # {"name": "/example", "description": "Example slash command."},
]

# ---------------------------------------------------------------------------
# Turn role detection helpers
# ---------------------------------------------------------------------------
SHORT_ANSWER_TOKENS: set[str] = {
    "yes", "y", "no", "n", "both", "continue", "resume", "retry", "same",
}
CONTINUATION_PHRASES: tuple[str, ...] = ("continue", "resume", "try again", "retry", "go ahead")
NEW_TASK_MARKERS: tuple[str, ...] = ("start over", "new task", "instead", "separate task", "different task")
NEW_TASK_VERBS: tuple[str, ...] = ("analyze", "calculate", "compute", "search", "find", "report", "summarize")

# ---------------------------------------------------------------------------
# Welcome / unsupported messages
# ---------------------------------------------------------------------------
WELCOME_MESSAGE = (
    f"{AGENT_TITLE} ready.\n\n"
    "I can calculate, search knowledge, and generate reports. "
    "Try: 'calculate 5 + 3' or 'search for Python best practices'."
)
UNSUPPORTED_MESSAGE = (
    f"This {AGENT_TITLE} currently supports: calculate, search, and report generation. "
    "Please rephrase your request or try one of the supported capabilities."
)
