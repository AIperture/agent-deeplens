# aethergraph-agent-deeplens

This package is currently centered on `v3`, a workflow-first DeepLens assistant for AetherGraph.

`v3` is the version to use if you want:
- chat-driven lens analysis from `.json` or `.zmx` inputs
- starting-point lens design from a small spec
- artifact export back into the AG UI
- long-running optimization submitted as a background AG run
- session memory for active lens, recent artifacts, and active run tracking

## What v3 Does

The `v3/` package registers:
- `deeplens_agent`
- `deeplens_v3_optimize_workflow`

The chat agent supports these main actions:
- Analyze an uploaded or active lens with `dl.analysis`
- Create a starting lens from required design fields with `dl.create_lens`
- Export the active lens to JSON and/or ZMX with `dl.export_lens`
- Submit optimization as a background child run with `ag.spawn_graph`
- Check or cancel the active child run with `ag.status` and `ag.cancel`

The runtime keeps a session-scoped state record for:
- active lens source
- active run id
- last metrics and artifacts
- recent loop trace and pending runs

## Essential Setup

Run commands from `aethergraph-agent-deeplens/`.

1. Install AetherGraph.

Install from PyPI:

```bash
pip install aethergraph
```

You should then have the `aethergraph` CLI available.

2. Create a local env file from the example:

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

3. Fill in the minimal LLM settings.

The essential variables for `v3` are:
- `AETHERGRAPH_ROOT=./aethergraph_data`
- `AETHERGRAPH_LLM__ENABLED=true`
- `AETHERGRAPH_LLM__DEFAULT__PROVIDER=openai`
- `AETHERGRAPH_LLM__DEFAULT__MODEL=gpt-5o-mini`
- `AETHERGRAPH_LLM__DEFAULT__API_KEY=...`

`gpt-5o-mini` is the minimum recommended default model because `v3` uses the default LLM for routing, loop decisions, and tool-argument refinement. If you want to use AetherGraph's generic env overrides instead, `LLM_PROVIDER`, `LLM_MODEL`, and `OPENAI_API_KEY` are also supported by the runtime.

4. Start the AG server and load `v3`:

```bash
aethergraph serve --project-root . --workspace ./aethergraph_data --load-module v3 --reload --reload-dir v3
```

Windows PowerShell uses the same command.

When the server starts, AetherGraph prints the key local URLs in the terminal, including:
- server URL
- UI URL
- API URL
- workspace path

The log file for this workspace is:
- `aethergraph_data/logs/aethergraph.log`

If the default port is available, the UI is usually at:
- `http://127.0.0.1:8745/ui`

5. Start the agent in the UI.

In the AetherGraph UI:
- select `Agents` on the left sidebar
- launch `DeepLens Assistant`
- start chatting with it

## Typical Requests

Examples that match the current implementation:

```text
/analysis analyze this uploaded lens
/analysis show MTF for this lens
/design create a starting lens with 60 degree FOV, f/2.8, 35 mm focal length
/optimize optimize this lens for 500 iterations
status
cancel
```

Design creation currently requires:
- `fov`
- `fnum`
- one of `foclen` or `imgh`

If those are missing, the agent asks for them instead of guessing.

## Current Capabilities

The current `v3` surface is intentionally narrow and practical:

- Lens input resolution from uploaded artifacts, local file paths, or the active session lens
- Workflow-level analysis with summary plus generated files
- Starting-point lens creation with baseline output artifacts
- Export of the active/provided lens to JSON and ZMX
- Background optimization submission through `deeplens_v3_optimize_workflow`
- Run tracking, run status checks, and cancellation through the AG runner
- Artifact delivery back into the AG UI as files and images
- Session memory for recently active context
- Stub execution paths for analysis, export, and optimization when explicitly requested

## Current Limitations

These are real limitations in the current code, not wishlist items:

- Request extraction is still mostly regex and keyword based. Free-form design specs and optimization intents can be misread or only partially captured.
- The supported workflow surface is narrow: analysis, starting design, export, optimization submission, status, and cancel. It is not a general DeepLens copilot yet.
- UX is functional but not polished. The agent relies on chat messages, approval prompts, and artifact/file delivery rather than a purpose-built workflow UI.
- Analysis mode selection is coarse today: `full`, `spot`, `mtf`, and `rms`.
- Design generation is a starting-point scaffold, not a full constraint solver or iterative design assistant.
- Optimization is background-only. The main chat turn does not stream detailed iterative progress beyond AG run semantics.
- The agent is conservative about missing information and may stop to ask for fields instead of inferring aggressively.
- Lens support is centered on `.json` and `.zmx`.
- Error handling is serviceable but still backend-oriented; failures often surface as direct exception summaries.

## TODOs

Near-term follow-ups that would materially improve `v3`:

- Replace more of the regex extraction path with schema-first parsing and stronger LLM-backed normalization.
- Improve design-spec understanding for sensor format, wavelength sets, constraints, and exclusions.
- Add richer run progress reporting from optimization checkpoints instead of only submitted/status/cancel semantics.
- Make artifact summaries more structured and easier to scan in the UI.
- Add a clearer active-lens and active-run UX so users do not need to infer session state from chat history.
- Expand analysis outputs into more guided interpretation instead of only returning files plus a short summary.
- Add better test coverage around routing, argument refinement, and state transitions.
- Normalize and clean some user-facing strings and encoding artifacts in responses.
- Decide whether `v1/` and `v2/` should remain as historical references or be retired from the package docs entirely.
