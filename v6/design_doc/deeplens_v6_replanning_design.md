# DeepLens v6 Reliable Loop Replanning Design

## Problem Statement

The existing v6 loop treated the agenda as authoritative after initial planning. When a tool failed, the loop marked the action as failed but still continued to later pending actions. This created invalid downstream execution such as:

- `dl.create_lens` fails
- `dl.export_lens` still runs
- the agent surfaces a second, derivative failure instead of recovering or escalating

The original `ToolResult` shape was also too narrow. It conveyed success or failure, but not enough runtime semantics for the loop to decide whether to retry, ask for input, replan, or escalate.

## Design Principles

- Runtime truth overrides the current agenda.
- Deterministic recovery runs before LLM replanning.
- The LLM replanner is broad within the current task, but cannot switch domains, invent tools, or bypass approval rules.
- Blocking prerequisite failures must stop the current agenda tail until recovery explicitly replaces it.
- Recovery is bounded. Repeated identical failures escalate to a human.

## Runtime Contracts

### ToolResult

`ToolResult` now includes:

- `tool_name`
- `failure_kind`
- `blocking`
- `repairable`
- `needs_replan`
- `needs_user_input`
- `missing_fields`
- `invalid_fields`
- `repair_hints`
- `dependency_failures`
- `human_escalation_reason`
- `diagnostics`

This lets the loop distinguish:

- invalid input repair
- missing input clarification
- dependency loss
- internal tool bugs
- unrecoverable failure

### RecoveryDecision

The recovery controller returns one of:

- `retry_action`
- `replace_remaining_agenda`
- `ask_user`
- `escalate`
- `fail`

The loop only continues after a failure if recovery explicitly returns `retry_action` or `replace_remaining_agenda`.

### Agenda Invariants

- `FAILED` agendas are not executable.
- Completed actions are preserved across replans.
- Pending tails may be replaced after failure recovery.
- Approval-gated steps remain approval-gated after replan.

## Control Flow

### Success Path

1. Build or resume agenda.
2. Execute one action.
3. Apply tool state updates.
4. Mark action complete.
5. Continue to the next pending action.

### Deterministic Repair Path

On failed tool execution:

1. Record the failure in state.
2. Merge runtime missing fields back into the task.
3. Attempt deterministic repair:
   - normalize numeric tool inputs
   - patch args from task/state
   - retry against active lens source when safe
   - replace remaining tail for obvious dependency recovery cases
4. If deterministic recovery succeeds, continue from the repaired action or replacement tail.

### LLM Replan Path

If deterministic recovery cannot safely recover:

1. Call the LLM replanner with:
   - current task
   - agenda history
   - failed action
   - structured tool failure
   - current working state
2. Accept only bounded outputs:
   - retry
   - replace remaining agenda
   - ask user
   - escalate
   - fail
3. Reject unregistered tool names and domain switching.

### Ask-User Path

If runtime failure reveals missing or contradictory user inputs:

- convert runtime findings into task missing fields
- surface a narrow clarification prompt
- wait for a follow-up turn before resuming execution

### Escalation Path

Escalate when:

- deterministic recovery cannot repair the failure
- LLM replanning fails or repeats the same failure signature
- the failure is classified as internal and persists after bounded attempts

## Refactor Structure

The v6 code now groups adjacent runtime responsibilities:

```text
v6/
  planning/
    agenda.py
    agenda_planner.py
    plan.py
  recovery/
    deterministic_repair.py
    llm_replanner.py
    recovery_engine.py
  tools/
    tool_arg_refine.py
    tool_dispatch.py
    tool_registry.py
  context/
    memory_policy.py
  fields/
    field_specs.py
    field_resolution.py
```

Top-level v6 modules remain as compatibility wrappers where needed, while the active runtime imports the adjacent package modules directly.

## Reliability Policy

- maximum one deterministic repair retry per logical failed step
- maximum one LLM-driven repair/replan cycle per repeated failure signature before escalation
- identical failure signatures are tracked in state
- blocking prerequisite failure prevents blind downstream continuation

## Examples

### Example 1: `create_lens` numeric type mismatch

- `dl.create_lens` receives `"40"` instead of `40.0`
- deterministic normalization coerces the field
- recovery retries the same action
- downstream export does not run unless creation succeeds or recovery explicitly replans

### Example 2: export after failed design creation

- `dl.create_lens` fails
- `dl.export_lens` is still in the planned tail
- loop stops trusting the tail
- deterministic recovery may switch to an already active lens source if present
- otherwise the LLM replanner or escalation path decides the next step

### Example 3: persistent internal backend error

- tool returns `internal_tool_error`
- deterministic repair cannot safely recover
- LLM replanning cannot find a safe agenda replacement
- loop escalates with explicit human handoff

## Migration Notes

- Public tool names and slash-command routing are preserved.
- Backend primitives such as `create_lens_design()` remain intact.
- The main behavior change is that execution now treats the plan as provisional and uses runtime observations to control continuation.
