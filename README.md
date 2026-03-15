# aethergraph-agent-deeplens

This package is now centered on `v7`, a bounded DeepLens assistant for AetherGraph with:
- spec-driven extraction owned by `v7`
- fixed-tool planning with deterministic fallback
- session memory for active lens, artifacts, and run tracking
- background optimization submission through AG child runs

The main entrypoint to use is:
- module: `v7`
- agent: `deeplens_agent_v7`
- UI title: `DeepLens Assistant v7`

## What V7 Registers

The `v7/` package exports:
- `deeplens_agent`
- `deeplens_v7_optimize_launcher`
- `deeplens_v7_optimize_workflow`

The chat agent plans and executes only this bounded workflow surface:
- `dl.create_lens`
- `dl.load_lens`
- `dl.analysis`
- `dl.optimize`
- `dl.export_lens`
- `ag.status`
- `ag.cancel`
- `ag.send_file`
- `ag.send_image`

## What V7 Can Do

`v7` supports these user-facing capabilities:
- Create a starting lens from a design spec. Required fields are `fov` and `fnum`.
- Parse optional design fields such as `foclen`, `imgh`, `bfl`, `thickness`, `save_name`, and `surf_list`.
- Analyze an uploaded or active lens with `full`, `spot`, `mtf`, or `rms` modes.
- Export the active or provided lens to `json` and/or `zmx`.
- Submit a background optimization run and track its AG run id.
- Check status or request cancellation for the active or specified run.
- Deliver generated artifacts back into the UI when the user clearly asks to send, show, download, or export them.

## What Changed In V7

Compared with earlier versions, `v7` now uses a local parse and field-spec layer instead of relying on `v3` extraction logic.

That means:
- extraction prompts are tool-aware
- missing-input prompts are generated from field metadata rather than hardcoded labels
- defaults are applied at binding time from `ToolSpec`
- required inputs are validated in binding before execution
- planner output is still bounded to the fixed DeepLens tool set

## Essential Setup

Run commands from `aethergraph-agent-deeplens/`.

1. Install AetherGraph.

```bash
pip install aethergraph
```

2. Create a local env file:

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

3. Fill in the essential LLM settings.

Minimum expected settings:
- `AETHERGRAPH_ROOT=./aethergraph_data`
- `AETHERGRAPH_LLM__ENABLED=true`
- `AETHERGRAPH_LLM__DEFAULT__PROVIDER=openai`
- `AETHERGRAPH_LLM__DEFAULT__MODEL=gpt-5-mini`
- `AETHERGRAPH_LLM__DEFAULT__API_KEY=...`
- `AETHERGRAPH_LLM__PROVIFILE__FAST__PROVIDER=openai`
- `AETHERGRAPH_LLM__PROVIFILE__FAST__MODEL=gpt-4o-mini`
- `AETHERGRAPH_LLM__PROVIFILE__FAST__API_KEY=...`


`v7` uses LLM-backed task extraction, follow-up parsing, and planning. It also has deterministic fallbacks when extraction or planning fails.

4. Start the AG server and load `v7`:

```bash
aethergraph serve --project-root . --workspace ./aethergraph_data --load-module v7 --reload --reload-dir v7
```

5. Launch the agent in the UI.

In AetherGraph UI:
- open `Agents`
- launch `DeepLens Assistant v7`
- start chatting with it

If the default port is available, the UI is usually:
- `http://127.0.0.1:8745/ui`

Workspace log file:
- `aethergraph_data/logs/aethergraph.log`

## Prompts To Try

These prompts match the current `v7` behavior and are good smoke tests.

### Basic design

```text
Create a starting lens with 60 degree FOV, f/2.8, and 35 mm focal length.
```

```text
Design a new lens with 50 deg FOV and f/4. Use defaults for the rest.
```

### Design with surf_list

```text
Create a lens with 55 degree FOV, f/2.8, and surf_list [["Aspheric", "Aspheric"], ["Aperture"], ["Aspheric", "Aspheric"]].
```

```text
Create a lens set with four lenses, all with aspherical surfaces, 60 degree FOV, and f/2.8.
```

### Analysis

You can upload a file or attach one from `Outputs` and then send: 
```text
Analyze this uploaded lens.
```

```text
Run MTF analysis on the active lens and send me the files.
```

### Export

```text
Export the active lens as JSON and ZMX.
```

### Optimization

```text
Optimize this lens for 500 iterations with checkpoint every 100.
```

```text
Optimize the active lens for edge sharpness, keep compact packaging, and save JSON plus ZMX.
```

### Run control

```text
status
```

```text
cancel
```

```text
Check run abc123xyz
```

## Current Capabilities

The current `v7` surface is intentionally narrow and execution-oriented:
- Lens source resolution from uploads, active session state, or explicit refs
- Tool-aware extraction of design, analysis, run, and delivery parameters
- Missing-input recovery through follow-up questions
- Starting-point lens creation with optional `surf_list`
- Background optimization submission with AG runner integration
- Session memory for active source, active run, recent artifacts, and prior plan history
- Artifact delivery back into the UI
- Stub execution paths when explicitly requested

## Current Limitations

These are actual `v7` limitations:
- The agent is not an open-ended DeepLens copilot. It only supports the fixed create, load, analyze, export, optimize, status, and cancel workflow surface.
- `surf_list` parsing is improved, but heuristics are still narrow. It handles explicit nested lists and some shorthand patterns like uniform all-aspheric or all-spheric layouts. More complex optical-layout prose may still need clarification.
- Analysis mode support is still limited to `full`, `spot`, `mtf`, and `rms`.


## Next Steps

We can improve the agent towards a general design agent or copilot with the improvements:
- Introduce lens generator agent as the very first planning step when needed; it should translate user request to initial design spec. The design of such agent can be heuristics-based or ReAct-based loop 
- Introduce optimization agent for auto-tune the specs and optimization hyperparamters. This should have its own agent loop with instructions on optimization strategies 
- Tighten the UX in planned steps in SKILL.md


## Practical Guidance

If you want the smoothest experience:
- Upload a `.json` or `.zmx` lens before asking for analysis or optimization.
- Be explicit with `fov` and `fnum` when creating a new lens.
- If you care about layout, specify `surf_list` directly.
- Ask to `send`, `show`, `download`, or `export` when you want files delivered back into the UI.
- Use the run id explicitly when checking or canceling a non-active optimization run.
