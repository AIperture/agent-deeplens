# DeepLens Agent v4 Development Plan

## Goal

Build a **clean v4 foundation** for the DeepLens agent by introducing a stronger interpretation layer instead of continuing to patch the v3 routing-first design.

This phase focuses only on **Phase 1 improvements for DeepLens and similar agents**:

- task repair
- hybrid extraction
- response composition
- richer tool schemas
- explicit context policies
- reduced LLM usage for responsiveness

The main philosophy for v4 is:

> **Interpret first, then execute.**

Instead of treating the front of the agent as a router that later gets patched by extraction and refinement, v4 should produce a stronger task hypothesis up front, then drive execution from that structured understanding.

---

## Core v4 Design Decision

### Replace the front door

In v3, the conceptual front door is roughly:

- route the user message
- extract fields
- refine tool arguments later if needed
- run the loop

In v4, the front door should become:

1. normalize turn
2. interpret turn
3. select controller
4. run controller
5. compose reply

This is the central breaking change for v4.

---

## Why this is the right starting point

The current system already has useful modular pieces, but the semantic understanding is still distributed across several places:

- routing
- extraction
- tool argument refinement
- loop-time repair-like behavior

That creates several problems:

- harder to understand conceptually
- more scattered control flow
- more repeated ambiguity handling
- unnecessary LLM calls
- weaker repair behavior because there is no single explicit object representing the agent's task hypothesis

So the first v4 goal is not to improve every behavior at once. It is to introduce a **strong semantic backbone** that the later Phase 1 work can build on.

---

# v4 Phase 1 Scope

## Included

- new interpretation layer
- new semantic data model
- field provenance tracking
- optional single-pass LLM interpretation fallback
- explicit context policy selection
- later task repair hook
- later richer tool results
- later response composition layer

## Not included yet

- stage graph architecture
- planner-driven multi-stage execution
- major memory redesign
- freeform exploratory agent loops
- large workflow abstraction changes unrelated to Phase 1

The objective is to make **DeepLens v4 reliable, understandable, and fast** before moving to more advanced agent abstractions.

---

# Development Stages

## Stage 0 — Intentional Breaking Change

### Objective
Stop treating routing as the main abstraction.

### Change
Replace the old entry-point mindset:

- `route()`

with a new one:

- `normalize_turn()`
- `interpret_turn()`
- `select_controller()`
- `run_controller()`
- `compose_reply()`

### Why
This clarifies the architecture immediately and gives every later feature a clean place to live.

---

## Stage 1 — Build the New Semantic Data Model

### Objective
Create explicit structured objects for turn understanding before touching loop behavior.

### New objects

#### 1. `TurnEnvelope`
Represents the normalized raw turn input.

Suggested contents:

- raw message
- cleaned message
- attachments
- slash command
- UI hints
- active references
- user metadata

#### 2. `FieldValue`
Represents one extracted field plus provenance.

Suggested contents:

- value
- source
- confidence
- inferred
- confirmed
- notes

#### 3. `TaskFrame`
Represents the agent’s structured hypothesis of what the user wants.

Suggested contents:

- domain hint
- intent
- task shape
- workflow family
- entry stage
- preferred tool
- missing fields
- risk level
- approval required
- context policy
- expected output
- confidence
- extracted field map
- repairable fields

#### 4. `ExecutionRequest`
Represents the execution payload derived from the task frame.

Suggested contents:

- selected tool or action
- normalized arguments
- controller kind
- execution constraints
- user-facing execution summary

#### 5. `InterpretationResult`
Represents the result of interpretation.

Suggested contents:

- envelope
- task frame
- execution request
- immediate reply if no execution is needed

### Why this stage comes first
Without these objects:

- repair has nothing explicit to repair
- context policy stays ad hoc
- response composition depends on weak state
- extraction and routing remain conceptually tangled

---

## Stage 2 — Implement Turn Normalization

### Objective
Normalize input into one consistent representation before semantic interpretation.

### Add
`normalize_turn()`

### Responsibilities

- normalize raw message text
- detect slash commands
- collect attachment summaries
- pull active refs from state if needed
- identify simple UI hints
- prepare a consistent `TurnEnvelope`

### Design note
This stage should be mostly deterministic and cheap.

### Why this matters
Interpretation quality improves when every later step consumes a single clean turn format rather than loosely assembled state.

---

## Stage 3 — Implement the Interpretation Layer

### Objective
Create a single front-door function that owns routing + extraction + task framing.

### Add
`interpret_turn()`

### Responsibilities

1. consume `TurnEnvelope`
2. apply explicit command rules
3. apply state-sensitive deterministic rules
4. run lexical extraction helpers
5. build a provisional `TaskFrame`
6. validate and normalize extracted fields
7. decide whether an LLM call is needed
8. if needed, make one structured LLM interpretation call
9. finalize `TaskFrame`
10. derive `ExecutionRequest`

### Key design principle
Do **not** collapse all logic into one giant function.

Instead:

- keep extraction helpers modular
- keep deterministic rules modular
- keep interpretation as the orchestrator

### Result
This becomes the new semantic front door for the entire agent.

---

## Stage 4 — LLM Usage Policy for Interpretation

### Objective
Minimize latency and token cost while preserving semantic quality.

### Rule 1: No LLM call when deterministic signals are strong
Examples:

- explicit slash commands
- clear run control requests
- obvious export/status/cancel requests
- clear analysis request tied to a known attachment
- design fields already extracted clearly

### Rule 2: Use one small LLM interpretation call only when needed
Examples:

- route confidence is low
- user intent mixes multiple goals
- key fields are missing but probably inferable
- tool choice is ambiguous
- state and current message conflict

### Rule 3: Do not use tool-refinement LLM by default
In v4, tool argument refinement should be downgraded to fallback behavior.

Use LLM refinement only when:

- a tool is selected but critical arguments remain ambiguous
- a previous tool call failed due to argument mismatch
- repair mode is attempting salvage

### Model profile guidance
Use cheaper/faster profiles by role.

Suggested profile strategy:

- `fast` or `router` profile for interpretation fallback
- `coding` profile only when tool reasoning or schema-heavy logic truly needs it
- `response` or cheap profile later for optional composition
- `reasoning` profile reserved for hard ambiguity or repair, not normal flow

---

## Stage 5 — Wire the Agent Entry Point to Interpretation

### Objective
Switch the agent to the new front door while preserving existing loop behavior temporarily.

### Change
Update `agent.py` so that it:

- uses `normalize_turn()`
- uses `interpret_turn()`
- receives `InterpretationResult`
- runs the existing loop/controller path with the derived execution request

### Important constraint
At this stage, do **not** rewrite the loop engine yet.

### Why
This allows v4 to gain the semantic backbone first while limiting the amount of moving parts changed at once.

---

## Stage 6 — Add Provenance-Tracked Hybrid Extraction

### Objective
Make extraction more reliable and more repairable.

### Required behavior
For each meaningful field:

- store the value
- store where it came from
- store confidence
- track whether it was inferred or explicitly provided
- track whether the user has confirmed it

### Example provenance sources

- regex
- lexical heuristic
- attachment metadata
- active state
- LLM inference
- user follow-up

### Why this matters
This enables:

- safer execution
- better follow-up questions
- clearer debugging
- better repair behavior
- more controlled overwriting rules

---

## Stage 7 — Add Task Repair

### Objective
Allow the agent to recover gracefully when the chosen task hypothesis or tool arguments were wrong or incomplete.

### Add
`repair_task()`

### Inputs

- current task frame
- tool result or failure
- state
- latest message
- contradiction signals

### Outputs

- updated task frame
- repair reason
- whether retry is appropriate
- whether user clarification is required

### Repair policy
1. deterministic repair first
2. LLM repair second

### Typical repair cases

- missing required input discovered during execution
- task type should shift after tool failure
- chosen tool is wrong for the interpreted intent
- prior inferred fields were weak and should be revised

### Why this stage comes after interpretation
Repair is much easier once the system has an explicit task frame and field provenance.

---

## Stage 8 — Upgrade Tool Result Schemas

### Objective
Make tool outputs easier for the controller and response layer to reason about.

### Current issue
Many tool results are still summary-driven and not rich enough for downstream control.

### Add or standardize fields such as

- outcome type
- needs input
- missing fields
- artifacts created
- state updates
- recommended next actions
- cost class
- user-visible summary
- should end turn

### Why this matters
Richer tool outputs reduce controller-specific branching and make both repair and response composition simpler.

---

## Stage 9 — Add Response Composition Layer

### Objective
Separate execution from user-facing communication.

### Add
`compose_reply()` or a response composition module

### Inputs

- task frame summary
- latest tool result
- created artifacts
- next-step suggestions
- follow-up requirements

### Design strategy
Start with deterministic templates for common cases.

Only use an LLM composer when:

- explanation quality matters significantly
- the turn is semantically complex
- a deterministic response would be too brittle

### Why this stage is later
Response composition becomes much easier and much cleaner once interpretation, repair, and tool results are already structured.

---

## Stage 10 — Expand Explicit Context Policies

### Objective
Move beyond vague lite/full loading and make context selection an explicit part of task framing.

### Suggested context policy levels

- minimal
- task-local
- artifact-centric
- memory-augmented
- full

### Example uses

- run status request → minimal
- simple analysis on uploaded lens → artifact-centric
- explanation of prior result → task-local
- ambiguous follow-up in long session → memory-augmented or full

### Why this is later
Context policy becomes much easier to choose correctly once `TaskFrame` exists.

---

# Recommended v4 File Layout

```text
v4/
  __init__.py
  agent.py

  types.py
  state.py

  input_normalization.py
  interpretation.py
  extraction.py
  field_provenance.py

  context_policy.py
  controller_select.py

  controllers/
    direct_controller.py
    workflow_controller.py

  repair.py
  response_compose.py

  tool_dispatch.py
  tool_registry.py
  tool_schemas.py

  policies/
    state_sensitive_rules.py
    missing_input_rules.py
    approval_rules.py
    post_tool_rules.py
    run_control_rules.py
    repair_rules.py
```

This layout reflects the new semantic-first architecture and gives policy logic a cleaner home.

---

# Recommended Implementation Order

## First slice to implement

### Slice A — Interpretation foundation
Implement only:

- `TurnEnvelope`
- `FieldValue`
- `TaskFrame`
- `ExecutionRequest`
- `InterpretationResult`
- `normalize_turn()`
- `interpret_turn()`
- minimal `agent.py` wiring

### Keep unchanged for now

- most of the loop engine
- most tool dispatch behavior
- most controller behavior
- planner-like extensions

### Reason
This gives v4 a strong semantic backbone with limited code churn.

---

# Short Practical Roadmap

## Phase 1A
Semantic foundation

1. create new types
2. add turn normalization
3. add interpretation pipeline
4. wire agent entry to interpretation

## Phase 1B
Execution reliability

5. add field provenance tracking
6. add task repair
7. enrich tool result schemas

## Phase 1C
User-facing quality + efficiency

8. add response composition layer
9. expand explicit context policies
10. tune LLM profile usage and fallback thresholds

---

# Guiding Principles for v4

## 1. Interpret once, early, and cheaply
Prefer one strong semantic pass over repeated LLM refinement throughout the turn.

## 2. Deterministic first, LLM second
Use LLMs where they add semantic value, not for basic mechanics.

## 3. Keep the loop simple while semantics improve
Do not redesign every layer at once.

## 4. Make repair explicit
Repair should operate on a visible task hypothesis, not hidden scattered state.

## 5. Separate execution from communication
Response composition should be its own concern.

## 6. Context should be selected intentionally
Do not over-load context by default.

---

# Immediate Next Step

The first implementation step should be:

## Draft the new v4 semantic types

Specifically:

- `TurnEnvelope`
- `FieldValue`
- `TaskFrame`
- `ExecutionRequest`
- `InterpretationResult`

That is the best starting point because every later Stage 1 feature depends on having those structures in place.

