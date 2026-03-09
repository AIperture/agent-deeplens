# aethergraph-agent-deeplens

DeepLens assistant scaffold for AetherGraph, organized as two self-contained agent packages:

- `v1/`: original policy-driven workflow/loop scaffold
- `v2/`: simplified router with stronger memory, loop, and tool-dispatch utilities
- `v3/`: workflow-first design and analysis agent with AG-run background optimization

## Layout

```text
aethergraph-agent-deeplens/
  v1/
    agent.py
    router.py
    loop_engine.py
    ...
    skills/SKILL.md
  v2/
    agent.py
    router.py
    loop_engine.py
    ...
    skills/SKILL.md
  v3/
    agent.py
    router.py
    loop_engine.py
    workflows.py
    ...
    skills/SKILL.md
```

Each version is now a root-level Python package. There is no `src/` wrapper anymore.

## Launch

Run commands from the `aethergraph-agent-deeplens/` directory so Python can import `v1` or `v2` directly.

Launch `v1`:

```powershell
aethergraph serve --load-module v1 --reload --reload-dir v1
```

Launch `v2`:

```powershell
aethergraph serve --load-module v2 --reload --reload-dir v2
```

Launch `v3`:

```powershell
aethergraph serve --load-module v3 --reload --reload-dir v3
```

Only load one agent package at a time. Both versions use the same user-facing agent id, so loading both together is not intended.

## Notes

- `v1` keeps the broader original execution scaffold and its version-specific skill file under `v1/skills/`.
- `v2` keeps the intentionally reduced router surface, but restores the richer memory/context assembly, bounded loop controls, and approval-aware tool utilities under `v2/`.
- `v2` keeps a separate persisted state key (`deeplens_state_v2`) so its runtime state does not collide with `v1`.
- `v3` adds workflow-level design/analysis tools, artifact delivery, and a spawned optimization workflow with AG-based status/cancel semantics.
