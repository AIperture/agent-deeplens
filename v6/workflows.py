from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aethergraph import NodeContext, graph_fn

from .backend import (
    _artifact_to_ref,
    _import_deeplens,
    deliver_artifacts,
    load_lens,
    persist_output_files,
)


@graph_fn(
    name="deeplens_v6_optimize_workflow",
    inputs=[
        "lens_source",
        "goal",
        "constraints",
        "excluded_objectives",
        "iterations",
        "checkpoint_every",
        "export_formats",
        "use_stub",
    ],
    outputs=["summary", "result_artifact_id", "result_json", "result_dir"],
)
async def deeplens_v6_optimize_workflow(
    lens_source: dict[str, Any] | None = None,
    goal: str | None = None,
    constraints: list[Any] | None = None,
    excluded_objectives: list[Any] | None = None,
    iterations: int = 500,
    checkpoint_every: int = 100,
    export_formats: list[str] | None = None,
    use_stub: bool = False,
    *,
    context: NodeContext,
) -> dict[str, Any]:
    chan = context.channel("ui:run")
    await chan.send_phase(
        phase="optimization",
        status="active",
        label="Optimizing lens",
        detail="Running DeepLens optimization workflow.",
    )

    export_formats = export_formats or ["json", "zmx"]
    if use_stub:
        summary_artifact = await context.artifacts().save_text(
            f"Stub optimization finished for goal: {goal or 'optimize lens'}",
            name="deeplens-optimize-summary.txt",
            labels={"workflow": "optimize", "stub": True},
        )
        summary = "Completed stub DeepLens optimization workflow."
        await chan.send_phase(
            phase="optimization",
            status="done",
            label="Optimization finished",
            detail=summary,
        )
        return {
            "summary": summary,
            "result_artifact_id": summary_artifact.artifact_id,
            "result_json": "",
            "result_dir": "",
        }

    lens, source = await load_lens(
        resolved_inputs={"lens_source": lens_source or {}},
        context=context,
        active_source_ref=lens_source or {},
    )
    _GeoLens, _create_lens = _import_deeplens()

    result_dir = Path(await context.artifacts().stage_dir("_deeplens_optimize"))
    result_dir.mkdir(parents=True, exist_ok=True)
    # CPU-bound – run in a thread so we don't block the event loop.
    await asyncio.to_thread(
        lens.optimize,
        iterations=iterations,
        test_per_iter=max(1, checkpoint_every),
        result_dir=str(result_dir),
    )

    if "json" in export_formats:
        lens.write_lens_json(str(result_dir / "optimized_lens.json"))
    if "zmx" in export_formats:
        try:
            lens.write_lens_zmx(str(result_dir / "optimized_lens.zmx"))
        except Exception:
            pass

    summary_payload = {
        "goal": goal or "optimize lens",
        "constraints": constraints or [],
        "excluded_objectives": excluded_objectives or [],
        "iterations": iterations,
        "checkpoint_every": checkpoint_every,
        "source": source,
    }
    summary_json_path = result_dir / "optimization_summary.json"
    summary_json_path.write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")

    artifacts = await persist_output_files(result_dir=result_dir, context=context, tag="optimize")
    await deliver_artifacts(artifacts=artifacts, context=context, limit=4)

    result_json_artifact = next(
        (artifact for artifact in artifacts if str(artifact.get("name")).endswith(".json")),
        None,
    )
    summary = f"Completed DeepLens optimization for `{source['name']}`."
    await chan.send_phase(
        phase="optimization",
        status="done",
        label="Optimization finished",
        detail=summary,
    )
    return {
        "summary": summary,
        "result_artifact_id": result_json_artifact.get("artifact_id") if result_json_artifact else "",
        "result_json": json.dumps(result_json_artifact or {}, ensure_ascii=False),
        "result_dir": str(result_dir),
    }
