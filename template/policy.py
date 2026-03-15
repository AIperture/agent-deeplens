from __future__ import annotations

from .types import ContextMode, TaskShape


AGENT_ID = "deeplens_template_agent"
AGENT_TITLE = "DeepLens Template Agent"
AGENT_SHORT_DESCRIPTION = "Debuggable capability-first agent kernel with fake tools."
AGENT_DESCRIPTION = (
    "Domain-neutral template agent for validating routing, capability planning, binding, "
    "approval enforcement, recovery, and resumable bounded execution."
)
STATE_KEY = "deeplens_template_state_v1"
SESSION_MEMORY_LEVEL = "session"

DEFAULT_CONTEXT_MODE = ContextMode.LITE
LITE_CHAT_LIMIT = 6
FULL_CHAT_LIMIT = 16

MAX_STEPS: dict[TaskShape, int] = {
    TaskShape.DIRECT_ANSWER: 2,
    TaskShape.SINGLE_ACTION: 5,
    TaskShape.MULTI_STEP: 10,
    TaskShape.UNSUPPORTED: 1,
}
MAX_RETRIES_PER_TOOL = 2
MAX_RECOVERY_ATTEMPTS = 2
LOOP_TRACE_LIMIT = 50
LOOP_HISTORY_LIMIT = 6
FAILURE_HISTORY_LIMIT = 20

CAPABILITY_CATALOG: dict[str, dict[str, object]] = {
    "inspect_context": {
        "keywords": ["inspect", "context", "state", "debug"],
        "required_fields": [],
        "optional_fields": [],
        "candidate_tools": ["inspect_context"],
        "expected_output": "A state summary and candidate inputs.",
    },
    "fetch_reference": {
        "keywords": ["reference", "lookup", "find", "fetch", "topic"],
        "required_fields": ["topic"],
        "optional_fields": ["detail_level"],
        "candidate_tools": ["fetch_reference"],
        "expected_output": "A fake reference retrieval result.",
    },
    "analyze_options": {
        "keywords": ["analyze", "compare", "options", "evaluate"],
        "required_fields": ["objective"],
        "optional_fields": ["constraints"],
        "candidate_tools": ["analyze_options"],
        "expected_output": "A structured options analysis.",
    },
    "draft_plan": {
        "keywords": ["plan", "draft", "outline", "steps"],
        "required_fields": ["objective"],
        "optional_fields": ["constraints"],
        "candidate_tools": ["draft_plan"],
        "expected_output": "A multi-step plan artifact.",
    },
    "apply_change": {
        "keywords": ["apply", "change", "update", "modify"],
        "required_fields": ["change_summary"],
        "optional_fields": ["target"],
        "candidate_tools": ["apply_change"],
        "expected_output": "A fake state-changing action result.",
    },
    "summarize_result": {
        "keywords": ["summarize", "summary", "result", "wrap up"],
        "required_fields": [],
        "optional_fields": ["audience"],
        "candidate_tools": ["summarize_result"],
        "expected_output": "A final plain-language summary.",
    },
}

CAPABILITY_ORDER: list[str] = [
    "inspect_context",
    "fetch_reference",
    "analyze_options",
    "draft_plan",
    "apply_change",
    "summarize_result",
]

SHORT_ANSWER_TOKENS: set[str] = {"yes", "y", "no", "n", "continue", "resume", "retry", "same", "approve", "approved"}
CONTINUATION_PHRASES: tuple[str, ...] = ("continue", "resume", "retry", "go ahead", "use that")
NEW_TASK_MARKERS: tuple[str, ...] = ("start over", "new task", "instead", "separate task", "different task")
NEW_TASK_VERBS: tuple[str, ...] = ("inspect", "fetch", "find", "analyze", "plan", "draft", "apply", "summarize")

WELCOME_MESSAGE = (
    "DeepLens Template Agent ready.\n\n"
    "Try asking for a plan, a fake reference lookup, an approval-gated change, or `/debug state`."
)
UNSUPPORTED_MESSAGE = (
    "This template agent supports inspect, fetch reference, analyze options, draft plan, apply change, and summarize result."
)
