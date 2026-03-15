# DeepLens Agent v5 Design

## Goal

Design a cleaner **v5 agent architecture** that preserves the strengths of v3 and the semantic improvements of v4, while removing the patch-heavy, rigid behavior that emerged in the v4 controller design.

The v5 design should:

- keep a strong structured task representation
- simplify the core loop
- use AG-native `ask_*` interaction primitives instead of re-creating interaction state everywhere
- separate **conversation control** from **task execution**
- make repair explicit but narrow
- support multi-turn continuation and task switching cleanly
- stay general enough to serve as a reusable pattern for other domain agents

---

# Core Design Principle

> **Interpret conversationally, execute locally, escalate when uncertain.**

This is the central v5 pattern.

In practice:

- a **master interpreter** decides what the user is doing relative to current conversation state
- a **local task loop** progresses one active task through bounded atomic actions
- a **repair layer** fixes failed actions only
- an **escalation policy** hands control back to the master interpreter when the local loop loses confidence or the conversation shifts

This avoids the two main problems seen in v4:

1. interpretation trying to do too much
2. repair being used as a substitute for task planning and task switching

---

# v5 High-Level Architecture

## Two-level control model

### Level 1 — Master Interpreter

The master interpreter is the high-level conversational judge.

Responsibilities:

- determine whether the new user turn is:
  - a continuation of the current task
  - an answer to a pending question
  - an approval response
  - a correction to the current task
  - a new / superseding task
  - a direct conversational question
  - a control/meta message
- create or patch the active `TaskFrame`
- decide whether to:
  - resume the local loop
  - replace the active task
  - answer directly
  - suspend the loop and reframe

The master interpreter should use lightweight cross-turn context.

### Level 2 — Local Task Loop

The local task loop is the bounded executor for one active task.

Responsibilities:

- select the next atomic action
- ask user for missing info through AG `ask_*`
- request approval through AG `ask_*`
- dispatch one tool call
- observe the result
- invoke repair if that action fails
- continue for a bounded number of steps
- return `WAITING`, `COMPLETE`, `ESCALATE`, or `FAILED`

The local loop should be narrow and myopic. It should not own full conversation-level reasoning.

### Repair Layer

Repair should only answer:

> The chosen atomic action failed. What is the smallest correction?

Typical repair outcomes:

- patch arguments
- ask for one missing field
- switch to an adjacent tool
- downgrade to a direct explanation
- fail safely

Repair should not own:

- task switching
- broad conversational reinterpretation
- multi-goal orchestration

### Escalation Layer

Escalation is the explicit handoff from the local loop back to the master interpreter.

Escalation is triggered when:

- a user reply does not fit the expected answer shape
- approval response is ambiguous
- repair attempts exceed threshold
- the same tool fails repeatedly
- the current turn strongly suggests a topic switch
- the local loop loses confidence in task continuity

This should be a first-class transition, not just a failure case.

---

# Core v5 Objects

The v5 design should reduce the number of first-class objects.

## 1. `TaskFrame`

This is the single main semantic object.

Suggested fields:

- `task_id`
- `user_goal`
- `domain_hint`
- `task_shape`
- `status`
- `preferred_tool`
- `fields`
- `field_map`
- `missing_fields`
- `notes`
- `assumptions`
- `created_from_turn`
- `updated_at`

Purpose:

- represent the current best task hypothesis
- carry extracted values and provenance
- serve as the common interface between interpreter, loop, repair, and response

## 2. `FieldValue`

Keep provenance, but keep it lightweight.

Suggested fields:

- `value`
- `source`
- `confidence`
- `inferred`
- `confirmed`
- `notes`

This is useful enough to keep.

## 3. `PendingInteraction`

This replaces scattered question/approval state.

Suggested fields:

- `kind`: `missing_info` | `approval`
- `prompt`
- `expected_fields`
- `response_shape`
- `related_task_id`
- `strictness`

Purpose:

- represent what the local loop is currently waiting for
- make follow-up turn classification easier

## 4. `LoopAction`

The next atomic step.

Suggested fields:

- `kind`: `ask_user` | `request_approval` | `tool_call` | `respond` | `finish` | `fail`
- `tool_name`
- `args`
- `reason`
- `priority`

Purpose:

- keep the loop centered on one next atomic action

## 5. `ToolResult`

Normalized tool output.

Suggested fields:

- `ok`
- `outcome_type`
- `summary`
- `error_code`
- `missing_fields`
- `artifacts`
- `state_updates`
- `recommended_next_step`
- `should_end_turn`

Purpose:

- provide a consistent observation for the loop and repair layer

## 6. `LoopOutcome`

The local loop should return one of:

- `COMPLETE`
- `WAITING`
- `ESCALATE`
- `FAILED`

Suggested fields:

- `kind`
- `reason`
- `task_snapshot`
- `pending_interaction`
- `last_tool_result`

## 7. `ConversationState`

This should be much smaller than the current v4 state.

Suggested fields:

- `active_task`
- `pending_interaction`
- `active_run_id`
- `active_source_ref`
- `last_artifacts`
- `design_draft`
- `last_result_summary`
- `retry_counters`
- `loop_trace`

The key simplification is: do not persist every intermediate semantic artifact.

---

# Context Management Pattern

The v5 design should use **two separate context passes**.

## 1. Pre-interpretation context

Used only by the master interpreter.

Contents:

- active task summary
- pending interaction
- recent 4–8 chat turns
- active source/run refs
- last result summary

Purpose:

- judge continuity vs task switch
- understand short follow-up turns
- interpret ambiguous replies to questions/approval prompts

This should be lightweight.

## 2. Execution context

Built only after the loop has selected the next atomic action.

Examples:

- analysis tool → artifact-centric context
- design tool → task-local context
- explanation → memory-augmented context if needed

Purpose:

- load only the context needed for the chosen action
- avoid heavy context selection too early

## Key rule

> Interpretation needs conversation-local context.
> Execution needs action-specific context.

This is a cleaner pattern than choosing one big context policy at the start of every turn.

---

# Extraction and Missing Fields

## Turn-level extraction

On every user turn, run lightweight extraction against:

- current message
- attachments
- active refs
- current active task

This produces candidate fields with provenance.

Important rule:

> **Parse broadly, commit narrowly.**

This means:

- the parser may detect many potentially useful fields
- the active task decides which fields matter now
- the loop uses only the fields relevant to the current task / next action

## Tool-level validation

Each tool still validates its own strict input requirements.

Example:

- `create_lens` validates design requirements
- `analysis` validates source + mode
- `export_lens` validates source + format

This is not full re-extraction. It is final requirement checking.

## Missing field categories

### 1. Task-level missing fields

Stored on `TaskFrame`.

Represents:

- what is still missing for the current task goal

### 2. Action-level missing inputs

Computed when validating the selected next action.

Represents:

- what is missing for the chosen tool call / approval / question

### 3. Repair-discovered missing fields

Returned by failed tool execution or repair.

Represents:

- what execution revealed was still missing or contradictory

These should flow back into `TaskFrame.missing_fields`.

---

# Master Interpreter Flow

The master interpreter should be much simpler than the v4 semantic front door.

Its primary decision is:

## What is this new turn relative to the current conversation state?

The output should be one of:

- `NEW_TASK`
- `UPDATE_OPEN_TASK`
- `ANSWER_PENDING_INTERACTION`
- `CONTROL_OR_META`
- `DIRECT_REPLY`

This is the main simplification.

Instead of trying to decide the full workflow up front, the interpreter first decides the turn’s conversational role.

## Master interpreter responsibilities

### `NEW_TASK`

- create a new `TaskFrame`
- possibly supersede the previous task

### `UPDATE_OPEN_TASK`

- patch the active `TaskFrame`
- merge any new extracted fields

### `ANSWER_PENDING_INTERACTION`

- interpret the turn as answer to:
  - missing info request
  - approval request
- update task / pending interaction accordingly

### `CONTROL_OR_META`

- cancel
- stop
- status
- change mode
- request explanation

### `DIRECT_REPLY`

- answer directly without entering the local loop

---

# Local Task Loop Flow

The local task loop should own **task progression**, not full conversation interpretation.

## Local loop contract

Input:

- current `TaskFrame`
- `ConversationState`
- runtime context

Output:

- `LoopOutcome`

## Local loop phases

### Phase 1 — Select next atomic action

Select only one of:

- `ask_user`
- `request_approval`
- `tool_call`
- `respond`
- `finish`
- `fail`

This is the key design rule.

### Phase 2 — Execute action

If `ask_user`:
- call AG `ask_*`
- preserve UI continuation automatically
- update task from returned answer

If `request_approval`:
- call AG approval primitive
- parse response
- update task or escalate if ambiguous

If `tool_call`:
- validate strict tool inputs
- dispatch tool
- normalize result into `ToolResult`

If `respond` / `finish` / `fail`:
- exit loop with `LoopOutcome`

### Phase 3 — Observe result

- success
- needs input
- approval required
- background run started
- failure

### Phase 4 — Transition

- apply tool result to task/state
- if failure, invoke repair
- if stable, continue
- if waiting on user, return `WAITING`
- if misaligned, return `ESCALATE`
- if done, return `COMPLETE`

---

# Repair Design

Repair should sit **inside the local loop**, after a selected atomic action fails or proves invalid.

## Repair inputs

- current `TaskFrame`
- selected `LoopAction`
- `ToolResult`
- `ConversationState`

## Repair outputs

- patched task
- patched action
- ask-user request
- adjacent tool switch
- fail safely
- escalate

## Repair policy

### Deterministic repair first

Examples:

- fill action args from task state
- map adjacent argument names
- remove stale missing fields
- detect obvious missing source / run id / export format

### LLM repair second

Used only when deterministic repair cannot safely recover.

LLM repair may:

- propose tentative defaults
- reframe the task slightly
- choose a neighboring tool
- ask for a narrower clarification

## What repair should never own

Repair should not own:

- full task switching
- broad cross-turn conversation control
- all continuation logic
- general workflow planning

If things get that broad, the loop should `ESCALATE`.

---

# Escalation and Loop Break Mechanism

The local loop must be able to break and hand control back to the master interpreter.

This is a core v5 feature.

## Escalation triggers

### 1. Unexpected answer shape

The loop expected:
- a scalar field
- a yes/no approval
- a specific missing input

But the user replied with:
- a new request
- an unrelated question
- a contradictory instruction

### 2. Too many repair cycles

Examples:

- same tool failed twice
- same missing field persists after repair
- repeated repair produces low-confidence outcomes

### 3. Topic switch / interruption

Examples:

- "never mind"
- "instead"
- "actually do this"
- a clearly unrelated new request

### 4. Ambiguous approval response

If approval response is not clearly parseable after a bounded retry, escalate.

### 5. Local confidence collapse

If the loop cannot confidently decide whether the new user turn is:
- answer to pending interaction
- continuation of current task
- correction
- new task

then it should stop trying to own the conversation.

## Important rule

> Topic shift is not repair failure.
> It is interruption / supersession / escalation.

This distinction keeps the architecture clean.

---

# Explicit Component Flow

Below is the v5 flow across components.

## A. New user turn enters the agent

```text
User Turn
  -> Turn Normalizer
  -> Pre-Interpretation Context Loader
  -> Master Interpreter
```

## B. Master interpreter classifies the turn

```text
Master Interpreter
  -> NEW_TASK
  -> UPDATE_OPEN_TASK
  -> ANSWER_PENDING_INTERACTION
  -> CONTROL_OR_META
  -> DIRECT_REPLY
```

## C. State update

```text
Interpreter Decision
  -> Update ConversationState
  -> Create / patch active TaskFrame
  -> Clear / keep / replace PendingInteraction
```

## D. Local loop begins or resumes

```text
ConversationState + TaskFrame
  -> Local Task Loop
  -> select Next LoopAction
```

## E. Local loop executes one atomic action

```text
LoopAction
  -> ask_user via AG ask_*
  -> request_approval via AG ask_*
  -> tool_call via tool dispatcher
  -> respond / finish / fail
```

## F. Observation

```text
Action Result
  -> ToolResult / UserAnswer / ApprovalAnswer
  -> apply observation to task + state
```

## G. Failure path

```text
Failed / Invalid Action
  -> Repair Layer
  -> patch action/task OR ask user OR escalate OR fail
```

## H. Exit conditions

```text
Loop exits with:
  -> COMPLETE
  -> WAITING
  -> ESCALATE
  -> FAILED
```

## I. If escalated

```text
ESCALATE
  -> hand control back to Master Interpreter
  -> reinterpret turn with broader context
```

---

# Detailed End-to-End Flow

## Flow 1 — Ordinary completion

```text
User asks for analysis
  -> Master Interpreter classifies as NEW_TASK
  -> TaskFrame created
  -> Local Loop selects tool_call(analysis)
  -> Tool executes successfully
  -> Result applied
  -> LoopOutcome = COMPLETE
  -> Response composer returns analysis summary + artifacts
```

## Flow 2 — Missing input collection

```text
User asks for lens design but omits image height
  -> Master Interpreter creates / updates design TaskFrame
  -> TaskFrame shows missing field(s)
  -> Local Loop selects ask_user(missing_info)
  -> AG ask_* collects the answer
  -> Turn answer merged into TaskFrame
  -> Local Loop resumes
  -> create_lens tool executes
  -> LoopOutcome = COMPLETE
```

## Flow 3 — Tool failure with repair

```text
User asks for export
  -> Local Loop selects tool_call(export)
  -> Tool fails due to missing format / stale source
  -> Repair tries deterministic patch
  -> If patched, retry tool_call(export)
  -> If still unstable, escalate or ask user
```

## Flow 4 — Topic switch during pending question

```text
Loop asks for image height
  -> User replies: "Actually analyze the uploaded lens instead"
  -> New turn enters master interpreter
  -> Interpreter classifies as NEW_TASK / superseding task
  -> Existing pending interaction is suspended or superseded
  -> Active TaskFrame replaced
  -> Local loop starts new analysis task
```

## Flow 5 — Ambiguous approval response

```text
Loop asks approval for optimization run
  -> User replies with unclear or unrelated message
  -> Local loop cannot safely parse approval
  -> LoopOutcome = ESCALATE
  -> Master Interpreter decides whether:
       - user approved
       - user rejected
       - user changed task
       - agent should ask again
```

---

# v5 Agent Loop Shape

The agent loop should be explicit and small.

## Pseudocode

```python
async def run_task_loop(task: TaskFrame, state: ConversationState, context) -> LoopOutcome:
    for step_idx in range(max_steps_for(task)):
        action = select_next_action(task, state, context)

        if action.kind == "ask_user":
            answer = await channel.ask_text_or_files(
                prompt=action.args["prompt"],
                accept_files=action.args.get("accept_files", False),
            )
            task = merge_user_answer_into_task(task, answer)
            state.pending_interaction = None
            continue

        if action.kind == "request_approval":
            approval = await channel.ask_approval(
                prompt=action.args["prompt"],
            )
            if approval.approved:
                state.pending_interaction = None
                continue
            if approval.rejected:
                return LoopOutcome(kind="FAILED", reason="User rejected approval.", task_snapshot=task)
            return LoopOutcome(kind="ESCALATE", reason="Approval response ambiguous.", task_snapshot=task)

        if action.kind == "tool_call":
            validation = validate_action_inputs(action, task)
            if not validation.ok:
                state.pending_interaction = PendingInteraction(
                    kind="missing_info",
                    prompt=validation.ask_prompt,
                    expected_fields=validation.missing_fields,
                    related_task_id=task.task_id,
                )
                return LoopOutcome(kind="WAITING", reason="Need more information.", task_snapshot=task)

            result = await dispatch_tool(action, task, state, context)
            observation = normalize_tool_result(result)

            if observation.ok:
                task, state = apply_tool_observation(task, state, observation)
                if observation.should_end_turn or task_is_complete(task, state):
                    return LoopOutcome(kind="COMPLETE", reason="Task completed.", task_snapshot=task)
                continue

            repair = await repair_failed_action(task, action, observation, state, context)

            if repair.kind == "retry":
                task = repair.task
                action = repair.action
                continue

            if repair.kind == "ask_user":
                state.pending_interaction = repair.pending_interaction
                return LoopOutcome(kind="WAITING", reason="Need clarification.", task_snapshot=task)

            if repair.kind == "escalate":
                return LoopOutcome(kind="ESCALATE", reason=repair.reason, task_snapshot=task)

            return LoopOutcome(kind="FAILED", reason=repair.reason, task_snapshot=task)

        if action.kind == "respond":
            return LoopOutcome(kind="COMPLETE", reason="Responded to user.", task_snapshot=task)

        if action.kind == "finish":
            return LoopOutcome(kind="COMPLETE", reason="Task finished.", task_snapshot=task)

        if action.kind == "fail":
            return LoopOutcome(kind="FAILED", reason=action.reason, task_snapshot=task)

    return LoopOutcome(kind="ESCALATE", reason="Loop budget exceeded.", task_snapshot=task)
```

---

# v5 Action Selection Pattern

`select_next_action()` should be simple and explicit.

Suggested priority order:

## 1. Pending interaction completion
If a required answer is still missing, ask user first.

## 2. Approval gate
If approval is required before the next tool action, request approval.

## 3. Missing required inputs for immediate next tool
If the likely next tool is known but required fields are missing, ask user.

## 4. Tool call
If the next atomic action is well-formed, execute it.

## 5. Respond / finish
If no execution is needed, respond.

## 6. Escalate / fail
If the loop cannot safely proceed, escalate or fail.

This keeps the loop predictable.

---

# v5 File / Module Layout

```text
v5/
  __init__.py
  agent.py

  types.py
  state.py

  input_normalization.py
  extraction.py
  field_provenance.py

  master_interpreter.py
  loop_engine.py
  action_select.py
  repair.py

  tool_dispatch.py
  tool_registry.py
  tool_schemas.py
  tool_results.py

  context/
    preinterpret_context.py
    execution_context.py

  response_compose.py
```

## Notes

### `master_interpreter.py`
Owns:
- turn role classification
- task creation / update / supersession
- handing off to loop

### `loop_engine.py`
Owns:
- bounded task progression
- AG ask_* interaction steps
- tool execution / observe / repair cycle

### `action_select.py`
Owns:
- choosing one next atomic `LoopAction`

### `repair.py`
Owns:
- failed-action salvage only

### `context/`
Separate:
- pre-interpretation context from
- execution-time context

---

# Why v5 Is Better

The v5 pattern is better because it restores the correct essence of the agent:

## 1. One active task at a time
The system has a clear semantic center.

## 2. One next atomic action at a time
The loop stays simple.

## 3. Conversation control is separated from execution control
This removes many of the v4 patches.

## 4. AG-native interaction is reused
`ask_*` handles continuation and memory logging instead of forcing custom continuation state everywhere.

## 5. Repair is narrow
Repair stops being a dump site for missing agent logic.

## 6. Escalation is explicit
When the local loop loses confidence, it yields control instead of forcing brittle behavior.

---

# Recommended Implementation Order

## Stage 1 — Simplify the type system
Implement only:

- `TaskFrame`
- `FieldValue`
- `PendingInteraction`
- `LoopAction`
- `ToolResult`
- `LoopOutcome`
- smaller `ConversationState`

## Stage 2 — Implement master interpreter
Implement:

- turn role classification
- task create / update / supersede
- use of lightweight pre-interpretation context

## Stage 3 — Implement local loop
Implement:

- action selection
- AG ask_* interaction
- tool execution
- tool result application
- loop outcome handling

## Stage 4 — Add narrow repair
Implement:

- deterministic repair first
- LLM repair second
- escalation output

## Stage 5 — Add response composition polish
Implement:

- deterministic reply composition
- assumption disclosure
- clearer repair / escalation explanation

---

# Final Summary

The v5 design should move away from the patch-heavy v4 controller model and toward a cleaner two-level architecture:

## Final pattern

> **Master Interpreter** handles conversation-level meaning.
>
> **Local Task Loop** handles bounded task progression.
>
> **Repair** fixes failed atomic actions.
>
> **Escalation** hands control back up when the local loop no longer has enough confidence.

This is the correct balance between:

- v3’s practical loop strength
- v4’s structured semantic improvements
- AG’s built-in continuation through `ask_*`

It should produce a system that is:

- simpler
- more general
- less rigid
- easier to debug
- easier to port to other domain agents
- much less likely to keep growing through ad hoc patches

