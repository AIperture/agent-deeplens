from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from aethergraph import NodeContext, graph_fn, graphify, tool
from aethergraph.core.graph.graph_builder import graph
from aethergraph.core.graph.task_graph import TaskGraph

from .backend import _artifact_to_ref, _import_deeplens, resolve_lens_source

DEFAULT_EXPORT_FORMATS = ["json", "zmx"]
OPTIMIZE_VIZ_FIGURE = "deeplens_optimize"
OPTIMIZE_LENS_FIGURE = "deeplens_lens"
OPTIMIZE_IMAGE_TRACK = "optimization_preview"
OPTIMIZE_RESUME_KEY_PREFIX = "deeplens_v7_optimize_resume"
OPTIMIZE_LAUNCHER_GRAPH_ID = "deeplens_v7_optimize_launcher"
OPTIMIZE_LEGACY_GRAPH_ID = "deeplens_v7_optimize_workflow"
OPTIMIZE_GENERATED_GRAPH_PREFIX = "deeplens_v7_optimize_run"


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _json_loads(payload: str | None, fallback: Any) -> Any:
    if not payload:
        return fallback
    try:
        return json.loads(payload)
    except Exception:
        return fallback


def _normalize_request(
    *,
    lens_source: dict[str, Any] | None,
    goal: str | None,
    constraints: list[Any] | None,
    excluded_objectives: list[Any] | None,
    iterations: int,
    checkpoint_every: int,
    export_formats: list[str] | None,
    use_stub: bool,
) -> dict[str, Any]:
    return {
        "lens_source": lens_source or {},
        "goal": goal or "optimize lens",
        "constraints": constraints or [],
        "excluded_objectives": excluded_objectives or [],
        "iterations": max(0, int(iterations or 0)),
        "checkpoint_every": max(1, int(checkpoint_every or 1)),
        "export_formats": export_formats or list(DEFAULT_EXPORT_FORMATS),
        "use_stub": bool(use_stub),
    }


def _plan_optimization_intervals(*, iterations: int, checkpoint_every: int) -> list[tuple[int, int]]:
    total_iterations = max(0, int(iterations or 0))
    interval_size = max(1, int(checkpoint_every or 1))
    if total_iterations <= 0:
        return [(0, 0)]

    intervals: list[tuple[int, int]] = []
    start_iteration = 0
    while start_iteration < total_iterations:
        end_iteration = min(total_iterations, start_iteration + interval_size)
        intervals.append((start_iteration, end_iteration))
        start_iteration = end_iteration
    return intervals


def _generated_graph_id(*, request: dict[str, Any], parent_run_id: str | None = None) -> str:
    request_hash = hashlib.sha1(_json_dumps(request).encode("utf-8")).hexdigest()[:10]
    run_suffix = (str(parent_run_id or "local").replace("-", "_"))[-12:]
    return f"{OPTIMIZE_GENERATED_GRAPH_PREFIX}_{run_suffix}_{request_hash}"


def _resume_key(context: NodeContext) -> str:
    return f"{OPTIMIZE_RESUME_KEY_PREFIX}:{context.run_id}:{context.node_id}"


def _result_dir_from_state(state: dict[str, Any]) -> str:
    return str(state.get("result_dir") or "")


def _relative_file_key(result_dir: Path, path: Path) -> str:
    try:
        return str(path.relative_to(result_dir)).replace("\\", "/")
    except Exception:
        return path.name


def _kind_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix in {".txt", ".log"}:
        return "text"
    if suffix in {".png", ".jpg", ".jpeg", ".gif"}:
        return "image"
    return "file"


async def _persist_new_output_files(
    *,
    result_dir: Path,
    context: NodeContext,
    tag: str,
    persisted_files: set[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    artifacts: list[dict[str, Any]] = []
    updated = set(persisted_files)
    for path in sorted(result_dir.rglob("*")):
        if not path.is_file():
            continue
        rel_key = _relative_file_key(result_dir, path)
        if rel_key in updated:
            continue
        artifact = await context.artifacts().save_file(
            path=str(path),
            kind=_kind_for_path(path),
            name=path.name,
            labels={"tag": tag, "filename": path.name, "relative_path": rel_key},
        )
        artifacts.append(_artifact_to_ref(artifact))
        updated.add(rel_key)
    return artifacts, updated


async def _emit_checkpoint_viz(
    *,
    context: NodeContext,
    step: int,
    total_iterations: int,
    metrics: dict[str, float] | None,
    new_artifacts: list[dict[str, Any]],
) -> None:
    try:
        progress = 1.0 if total_iterations <= 0 else min(1.0, max(0.0, step / float(total_iterations)))
        await context.viz().scalar(
            "progress",
            step=step,
            value=progress,
            figure_id=OPTIMIZE_VIZ_FIGURE,
            mode="replace",
            meta={"iterations": total_iterations},
        )
        if metrics:
            for key, value in metrics.items():
                await context.viz().scalar(
                    key,
                    step=step,
                    value=float(value),
                    figure_id=OPTIMIZE_VIZ_FIGURE,
                    mode="replace",
                )
        latest_image = next((artifact for artifact in reversed(new_artifacts) if str(artifact.get("kind")) == "image"), None)
        if latest_image and latest_image.get("artifact_id"):
            image_artifact = type(
                "ArtifactRef",
                (),
                {"artifact_id": str(latest_image["artifact_id"])},
            )()
            await context.viz().image_from_artifact(
                OPTIMIZE_IMAGE_TRACK,
                step=step,
                artifact=image_artifact,
                figure_id=OPTIMIZE_LENS_FIGURE,
                mode="replace",
                meta={
                    "filename": latest_image.get("name"),
                    "label": "Optimization preview",
                },
            )
        await asyncio.sleep(0)
    except Exception:
        return


async def _load_resume_state(context: NodeContext) -> dict[str, Any]:
    return dict(await context.memory().latest_state(_resume_key(context)) or {})


async def _save_resume_state(context: NodeContext, state: dict[str, Any]) -> None:
    await context.memory().record_state(
        key=_resume_key(context),
        value=state,
        tags=["ag.deeplens.v7", "optimize", "resume"],
        meta={"graph_id": context.graph_id, "node_id": context.node_id},
        severity=1,
    )


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _tool_phase_name(tool_name: str) -> str:
    return f"tool.{tool_name}"


async def _send_tool_phase(
    context: NodeContext,
    *,
    tool_name: str,
    status: str,
    label: str,
    detail: str,
) -> None:
    text = f"{label}: {detail}"
    await context.channel("ui:run").send_text(text)
    # await context.channel("ui:run").send_phase(
    #     phase=_tool_phase_name(tool_name),
    #     status=status,  # type: ignore[arg-type]
    #     label=label,
    #     detail=detail,
    # )
    # await asyncio.sleep(3)  # yield control to ensure the message is sent promptly
    print(f"🍎 [{context.run_id}] {label} - {detail}")


def _import_optimization_dependencies() -> tuple[Any, Any, float, list[float], Any]:
    _import_deeplens()
    import torch  # type: ignore
    from deeplens.optics.config import DEPTH, EPSILON, WAVE_RGB  # type: ignore
    from transformers import get_cosine_schedule_with_warmup  # type: ignore

    return torch, get_cosine_schedule_with_warmup, DEPTH, list(WAVE_RGB), EPSILON


def _prepare_checkpoint_bundle(
    *,
    lens: Any,
    checkpoint_iteration: int,
    result_dir: str,
    shape_control: bool = True,
    centroid: bool = False,
) -> dict[str, Any]:
    torch, _scheduler_factory, depth, wave_rgb, _epsilon = _import_optimization_dependencies()

    result_dir_path = Path(result_dir)
    if shape_control and checkpoint_iteration > 0:
        lens.correct_shape()

    lens.write_lens_json(str(result_dir_path / f"iter{checkpoint_iteration}.json"))
    lens.analysis(str(result_dir_path / f"iter{checkpoint_iteration}"))

    lens.calc_pupil()
    rays_backup = []
    ray = None
    for wavelength in wave_rgb:
        ray = lens.sample_ring_arm_rays(
            num_ring=32,
            num_arm=8,
            spp=2048,
            depth=depth,
            wvln=wavelength,
            scale_pupil=1.05,
            sample_more_off_axis=False,
        )
        rays_backup.append(ray)

    if ray is None:
        raise RuntimeError("DeepLens checkpoint preparation did not generate any rays.")

    center_method = "chief_ray" if centroid else "pinhole"
    center_ref = -lens.psf_center(points_obj=ray.o[:, :, 0, :], method=center_method)
    center_ref = center_ref.unsqueeze(-2).repeat(1, 1, 2048, 1)
    return {
        "iteration": checkpoint_iteration,
        "rays_backup": rays_backup,
        "center_ref": center_ref,
    }


def _build_optimizer_state(*, lens: Any, iterations: int, start_iteration: int) -> tuple[Any, Any]:
    _torch, scheduler_factory, _depth, _wave_rgb, _epsilon = _import_optimization_dependencies()
    optimizer = lens.get_optimizer([1e-3, 1e-4, 1e-1, 1e-4], optim_mat=False)
    training_steps = max(1, int(iterations or 1))
    warmup_steps = min(100, training_steps)
    scheduler = scheduler_factory(
        optimizer,
        num_warmup_steps=warmup_steps,
        num_training_steps=training_steps,
    )
    for _ in range(max(0, int(start_iteration))):
        scheduler.step()
    return optimizer, scheduler


def _load_lens_sync(source: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    GeoLens, _create_lens = _import_deeplens()
    source_ref = dict(source or {})
    lens = GeoLens(filename=str(source_ref["path"]))
    return lens, source_ref


def _write_export_files(*, lens: Any, result_dir: str, export_formats: list[str]) -> None:
    result_dir_path = Path(result_dir)
    if "json" in export_formats:
        lens.write_lens_json(str(result_dir_path / "optimized_lens.json"))
    if "zmx" in export_formats:
        try:
            lens.write_lens_zmx(str(result_dir_path / "optimized_lens.zmx"))
        except Exception:
            pass


def _run_training_step(
    *,
    lens: Any,
    optimizer: Any,
    scheduler: Any,
    rays_backup: list[Any],
    center_ref: Any,
) -> dict[str, float]:
    torch, _scheduler_factory, _depth, wave_rgb, epsilon = _import_optimization_dependencies()

    loss_rms_ls = []
    weight_mask = None
    for wv_idx, _wavelength in enumerate(wave_rgb):
        ray = rays_backup[wv_idx].clone()
        ray = lens.trace2sensor(ray)

        ray_xy = ray.o[..., :2]
        ray_valid = ray.is_valid
        ray_err = ray_xy - center_ref
        ray_err = torch.where(
            ray_valid.bool().unsqueeze(-1),
            ray_err,
            torch.zeros_like(ray_err),
        )

        if wv_idx == 0:
            with torch.no_grad():
                weight_mask = (ray_err**2).sum(-1).sum(-1)
                weight_mask /= ray_valid.sum(-1) + epsilon
                weight_mask /= weight_mask.mean() + epsilon

        l_rms = (ray_err**2).sum(-1).sum(-1)
        l_rms /= ray_valid.sum(-1) + epsilon
        l_rms = (l_rms + epsilon).sqrt()

        if weight_mask is None:
            raise RuntimeError("Weight mask initialization failed during optimization.")
        l_rms_weighted = (l_rms * weight_mask).sum()
        l_rms_weighted /= weight_mask.sum() + epsilon
        loss_rms_ls.append(l_rms_weighted)

    loss_rms = sum(loss_rms_ls) / len(loss_rms_ls)
    loss_focus = lens.loss_infocus()
    loss_reg, loss_dict = lens.loss_reg()
    total_loss = loss_rms + loss_focus + 0.1 * loss_reg

    optimizer.zero_grad()
    total_loss.backward()
    optimizer.step()
    scheduler.step()

    metrics = {
        "loss_total": float(total_loss.item()),
        "loss_rms": float(loss_rms.item()),
        "loss_focus": float(loss_focus.item()),
        "loss_reg": float(loss_reg.item()),
    }
    for key, value in loss_dict.items():
        try:
            metrics[str(key)] = float(value)
        except Exception:
            continue
    return metrics


@tool(outputs=["request_json", "is_stub"])
async def prepare_optimize_request(
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
    tool_name = "prepare_optimize_request"
    await _send_tool_phase(
        context,
        tool_name=tool_name,
        status="active",
        label="Preparing request",
        detail="Normalizing optimization inputs.",
    )
    try:
        request = _normalize_request(
            lens_source=lens_source,
            goal=goal,
            constraints=constraints,
            excluded_objectives=excluded_objectives,
            iterations=iterations,
            checkpoint_every=checkpoint_every,
            export_formats=export_formats,
            use_stub=use_stub,
        )
        # await asyncio.sleep(2)  # simulate some processing time for the preparation step
        # request = {
        #     "lens_source": lens_source or {},
        #     "goal": goal or "optimize lens",
        #     "use_stub": bool(use_stub),
        # }
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="done",
            label="Request prepared",
            detail="Optimization inputs are ready.",
        )
        return {
            "request_json": _json_dumps(request),
            "is_stub": bool(request["use_stub"]),
        }
    except Exception as exc:
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="failed",
            label="Preparation failed",
            detail=str(exc),
        )
        raise


@tool(outputs=["source_json"])
async def load_lens_for_optimization(
    lens_source: dict[str, Any] | None = None,
    request_json: str = "",
    is_stub: bool = False,
    *,
    context: NodeContext,
) -> dict[str, Any]:
    tool_name = "load_lens_for_optimization"
    request = _json_loads(request_json, {})
    print(f"🍎 [{context.run_id}] Starting lens load for optimization with request: {request}")
    await _send_tool_phase(
        context,
        tool_name=tool_name,
        status="active",
        label="Loading lens",
        detail="Resolving the lens source for optimization.",
    )
    if is_stub:
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="done",
            label="Loading skipped",
            detail="Stub mode does not load a physical lens.",
        )
        return {"source_json": _json_dumps({"source": lens_source or request.get("lens_source") or {}})}

    try:
        source = await resolve_lens_source(
            resolved_inputs={"lens_source": lens_source or request.get("lens_source") or {}},
            context=context,
            active_source_ref=lens_source or request.get("lens_source") or {},
        )
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="done",
            label="Lens loaded",
            detail=f"Loaded `{source.get('name') or 'lens'}`.",
        )
        return {"source_json": _json_dumps({"source": source})}
    except Exception as exc:
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="failed",
            label="Lens load failed",
            detail=str(exc),
        )
        raise


@tool(
    outputs=[
        "run_state_json",
        "latest_checkpoint_artifact",
        "latest_iteration",
        "result_dir",
        "metrics_json",
    ]
)
async def run_optimization_interval(
    source_json: str,
    request_json: str,
    prior_run_state_json: str = "",
    start_iteration: int = 0,
    end_iteration: int = 0,
    is_stub: bool = False,
    *,
    context: NodeContext,
) -> dict[str, Any]:
    tool_name = "run_optimization_interval"
    request = _json_loads(request_json, {})
    source_payload = _json_loads(source_json, {})
    prior_state = _json_loads(prior_run_state_json, {})
    total_iterations = int(request.get("iterations") or 0)
    start_iteration = max(0, int(start_iteration or 0))
    end_iteration = max(start_iteration, int(end_iteration or 0))
    goal = str(request.get("goal") or "optimize lens")

    await _send_tool_phase(
        context,
        tool_name=tool_name,
        status="active",
        label="Optimizing lens",
        detail=f"Running interval {start_iteration}-{end_iteration} for goal: {goal}.",
    )

    try:
        if is_stub:
            run_state = dict(prior_state)
            run_state.setdefault("mode", "stub")
            run_state.setdefault(
                "summary",
                "Completed stub DeepLens optimization workflow.",
            )
            run_state["latest_iteration"] = end_iteration
            run_state.setdefault("result_dir", "")
            run_state.setdefault("persisted_files", [])
            run_state.setdefault("source", source_payload.get("source") or {})
            run_state.setdefault("latest_metrics", {})
            if end_iteration >= total_iterations:
                summary_artifact = await context.artifacts().save_text(
                    f"Stub optimization finished for goal: {goal}",
                    name="deeplens-optimize-summary.txt",
                    labels={"workflow": "optimize", "stub": True},
                )
                run_state["result_artifact"] = _artifact_to_ref(summary_artifact)
                run_state["preferred_result_artifact"] = _artifact_to_ref(summary_artifact)
            await _send_tool_phase(
                context,
                tool_name=tool_name,
                status="done",
                label="Interval finished",
                detail=f"Stub interval completed at iteration {end_iteration}.",
            )
            return {
                "run_state_json": _json_dumps(run_state),
                "latest_checkpoint_artifact": _json_dumps(
                    run_state.get("latest_checkpoint_artifact") or {}
                ),
                "latest_iteration": end_iteration,
                "result_dir": str(run_state.get("result_dir") or ""),
                "metrics_json": _json_dumps(run_state.get("latest_metrics") or {}),
            }

        base_source = dict(source_payload.get("source") or {})
        active_source = dict(prior_state.get("latest_checkpoint_artifact") or base_source)
        resolved_source = await resolve_lens_source(
            resolved_inputs={"lens_source": active_source},
            context=context,
            active_source_ref=active_source,
        )
        lens, resolved_source = await asyncio.to_thread(_load_lens_sync, resolved_source)

        result_dir_text = str(
            prior_state.get("result_dir") or await context.artifacts().stage_dir("_deeplens_optimize")
        )
        result_dir = Path(result_dir_text)
        result_dir.mkdir(parents=True, exist_ok=True)

        persisted_files = set(str(item) for item in (prior_state.get("persisted_files") or []))
        latest_metrics: dict[str, float] = dict(prior_state.get("latest_metrics") or {})
        latest_checkpoint_artifact = dict(prior_state.get("latest_checkpoint_artifact") or {})

        if start_iteration <= 0 and not latest_checkpoint_artifact:
            checkpoint_bundle = await asyncio.to_thread(
                _prepare_checkpoint_bundle,
                lens=lens,
                checkpoint_iteration=0,
                result_dir=str(result_dir),
            )
            new_artifacts, persisted_files = await _persist_new_output_files(
                result_dir=result_dir,
                context=context,
                tag="optimize",
                persisted_files=persisted_files,
            )
            await _emit_checkpoint_viz(
                context=context,
                step=0,
                total_iterations=total_iterations,
                metrics=None,
                new_artifacts=new_artifacts,
            )
            latest_checkpoint_artifact = next(
                (artifact for artifact in new_artifacts if str(artifact.get("name")) == "iter0.json"),
                latest_checkpoint_artifact,
            )
        else:
            checkpoint_bundle = await asyncio.to_thread(
                _prepare_checkpoint_bundle,
                lens=lens,
                checkpoint_iteration=start_iteration,
                result_dir=str(result_dir),
            )

        optimizer, scheduler = await asyncio.to_thread(
            _build_optimizer_state,
            lens=lens,
            iterations=total_iterations,
            start_iteration=start_iteration,
        )

        for iteration in range(max(1, start_iteration + 1), end_iteration + 1):
            latest_metrics = await asyncio.to_thread(
                _run_training_step,
                lens=lens,
                optimizer=optimizer,
                scheduler=scheduler,
                rays_backup=checkpoint_bundle["rays_backup"],
                center_ref=checkpoint_bundle["center_ref"],
            )

        checkpoint_bundle = await asyncio.to_thread(
            _prepare_checkpoint_bundle,
            lens=lens,
            checkpoint_iteration=end_iteration,
            result_dir=str(result_dir),
        )
        new_artifacts, persisted_files = await _persist_new_output_files(
            result_dir=result_dir,
            context=context,
            tag="optimize",
            persisted_files=persisted_files,
        )
        latest_checkpoint_artifact = next(
            (
                artifact
                for artifact in new_artifacts
                if str(artifact.get("name")) == f"iter{end_iteration}.json"
            ),
            latest_checkpoint_artifact,
        )
        await _emit_checkpoint_viz(
            context=context,
            step=end_iteration,
            total_iterations=total_iterations,
            metrics=latest_metrics,
            new_artifacts=new_artifacts,
        )

        progress_payload = {
            "goal": goal,
            "iterations": total_iterations,
            "checkpoint_every": int(request.get("checkpoint_every") or 1),
            "latest_iteration": end_iteration,
            "metrics": latest_metrics,
            "source": resolved_source,
        }
        await asyncio.to_thread(_write_json, result_dir / "optimization_progress.json", progress_payload)

        run_state = {
            "result_dir": str(result_dir),
            "latest_iteration": end_iteration,
            "latest_checkpoint_artifact": latest_checkpoint_artifact,
            "preferred_result_artifact": latest_checkpoint_artifact,
            "persisted_files": sorted(persisted_files),
            "latest_metrics": latest_metrics,
            "source": resolved_source,
        }
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="done",
            label="Interval finished",
            detail=f"Completed optimization interval {start_iteration}-{end_iteration}.",
        )
        return {
            "run_state_json": _json_dumps(run_state),
            "latest_checkpoint_artifact": _json_dumps(latest_checkpoint_artifact),
            "latest_iteration": end_iteration,
            "result_dir": str(result_dir),
            "metrics_json": _json_dumps(latest_metrics),
        }
    except Exception as exc:
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="failed",
            label="Interval failed",
            detail=str(exc),
        )
        raise


@tool(outputs=["run_state_json"])
async def run_optimization_with_checkpoints(
    source_json: str,
    request_json: str,
    is_stub: bool,
    *,
    context: NodeContext,
) -> dict[str, Any]:
    tool_name = "run_optimization_with_checkpoints"
    request = _json_loads(request_json, {})
    source_payload = _json_loads(source_json, {})
    goal = str(request.get("goal") or "optimize lens")
    await _send_tool_phase(
        context,
        tool_name=tool_name,
        status="active",
        label="Optimizing lens",
        detail=f"Starting optimization for goal: {goal}.",
    )

    # await asyncio.sleep(2)  # simulate some initial processing time before the optimization loop starts
    # print(f"🍎 [{context.run_id}] Starting optimization with request: {request} and source: {source_payload}")
    # return {
    #     "run_state_json": _json_dumps({
    #         "mode": "stub" if is_stub else "full",
    #         "summary": "This is a placeholder summary. The full optimization workflow is currently disabled.",
    #     }
    #     )
    # }

    try:
        if is_stub:
            summary_artifact = await context.artifacts().save_text(
                f"Stub optimization finished for goal: {goal}",
                name="deeplens-optimize-summary.txt",
                labels={"workflow": "optimize", "stub": True},
            )
            await _send_tool_phase(
                context,
                tool_name=tool_name,
                status="done",
                label="Optimization finished",
                detail="Stub optimization completed.",
            )
            return {
                "run_state_json": _json_dumps(
                    {
                        "mode": "stub",
                        "summary": "Completed stub DeepLens optimization workflow.",
                        "result_artifact": _artifact_to_ref(summary_artifact),
                        "preferred_result_artifact": _artifact_to_ref(summary_artifact),
                        "result_dir": "",
                        "persisted_files": [],
                        "source": source_payload.get("source") or {},
                    }
                )
            }

        resume_state = await _load_resume_state(context)
        base_source = dict(source_payload.get("source") or {})
        resume_source = resume_state.get("latest_checkpoint_artifact") or {}
        active_source = resume_source or base_source

        resolved_source = await resolve_lens_source(
            resolved_inputs={"lens_source": active_source},
            context=context,
            active_source_ref=active_source,
        )
        lens, resolved_source = await asyncio.to_thread(_load_lens_sync, resolved_source)

        result_dir_text = str(resume_state.get("result_dir") or await context.artifacts().stage_dir("_deeplens_optimize"))
        result_dir = Path(result_dir_text)
        result_dir.mkdir(parents=True, exist_ok=True)

        iterations = int(request.get("iterations") or 0)
        checkpoint_every = max(1, int(request.get("checkpoint_every") or 1))
        persisted_files = set(str(item) for item in (resume_state.get("persisted_files") or []))
        latest_iteration = int(resume_state.get("latest_iteration") or 0)
        last_checkpoint = latest_iteration if latest_iteration > 0 else 0

        if resume_source:
            await _send_tool_phase(
                context,
                tool_name=tool_name,
                status="active",
                label="Resuming optimization",
                detail=f"Reloading checkpoint at iteration {last_checkpoint}.",
            )
            checkpoint_bundle = await asyncio.to_thread(
                _prepare_checkpoint_bundle,
                lens=lens,
                checkpoint_iteration=last_checkpoint,
                result_dir=str(result_dir),
            )
            loop_start = last_checkpoint + 1
        else:
            checkpoint_bundle = await asyncio.to_thread(
                _prepare_checkpoint_bundle,
                lens=lens,
                checkpoint_iteration=0,
                result_dir=str(result_dir),
            )
            new_artifacts, persisted_files = await _persist_new_output_files(
                result_dir=result_dir,
                context=context,
                tag="optimize",
                persisted_files=persisted_files,
            )
            await _emit_checkpoint_viz(
                context=context,
                step=0,
                total_iterations=iterations,
                metrics=None,
                new_artifacts=new_artifacts,
            )
            latest_checkpoint_artifact = next(
                (artifact for artifact in new_artifacts if str(artifact.get("name")) == "iter0.json"),
                resume_state.get("latest_checkpoint_artifact") or {},
            )
            resume_state = {
                "result_dir": str(result_dir),
                "latest_iteration": 0,
                "latest_checkpoint_artifact": latest_checkpoint_artifact,
                "preferred_result_artifact": latest_checkpoint_artifact,
                "persisted_files": sorted(persisted_files),
                "source": base_source,
            }
            await _save_resume_state(context, resume_state)
            loop_start = 0

        optimizer, scheduler = await asyncio.to_thread(
            _build_optimizer_state,
            lens=lens,
            iterations=iterations,
            start_iteration=loop_start,
        )

        latest_metrics: dict[str, float] = dict(resume_state.get("latest_metrics") or {})
        latest_checkpoint_artifact = dict(resume_state.get("latest_checkpoint_artifact") or {})

        for iteration in range(loop_start, iterations + 1):
            if iteration > 0 and iteration % checkpoint_every == 0:
                checkpoint_bundle = await asyncio.to_thread(
                    _prepare_checkpoint_bundle,
                    lens=lens,
                    checkpoint_iteration=iteration,
                    result_dir=str(result_dir),
                )
                new_artifacts, persisted_files = await _persist_new_output_files(
                    result_dir=result_dir,
                    context=context,
                    tag="optimize",
                    persisted_files=persisted_files,
                )
                latest_checkpoint_artifact = next(
                    (artifact for artifact in new_artifacts if str(artifact.get("name")) == f"iter{iteration}.json"),
                    latest_checkpoint_artifact,
                )
                await _emit_checkpoint_viz(
                    context=context,
                    step=iteration,
                    total_iterations=iterations,
                    metrics=latest_metrics,
                    new_artifacts=new_artifacts,
                )
                await _send_tool_phase(
                    context,
                    tool_name=tool_name,
                    status="active",
                    label="Optimizing lens",
                    detail=f"Checkpoint saved at iteration {iteration}/{iterations}.",
                )
                resume_state = {
                    "result_dir": str(result_dir),
                    "latest_iteration": iteration,
                    "latest_checkpoint_artifact": latest_checkpoint_artifact,
                    "preferred_result_artifact": latest_checkpoint_artifact,
                    "persisted_files": sorted(persisted_files),
                    "latest_metrics": latest_metrics,
                    "source": base_source,
                }
                await _save_resume_state(context, resume_state)

            latest_metrics = await asyncio.to_thread(
                _run_training_step,
                lens=lens,
                optimizer=optimizer,
                scheduler=scheduler,
                rays_backup=checkpoint_bundle["rays_backup"],
                center_ref=checkpoint_bundle["center_ref"],
            )

            if iteration == iterations:
                await _emit_checkpoint_viz(
                    context=context,
                    step=iteration,
                    total_iterations=iterations,
                    metrics=latest_metrics,
                    new_artifacts=[],
                )

        if iterations % checkpoint_every != 0:
            checkpoint_bundle = await asyncio.to_thread(
                _prepare_checkpoint_bundle,
                lens=lens,
                checkpoint_iteration=iterations,
                result_dir=str(result_dir),
            )
            new_artifacts, persisted_files = await _persist_new_output_files(
                result_dir=result_dir,
                context=context,
                tag="optimize",
                persisted_files=persisted_files,
            )
            latest_checkpoint_artifact = next(
                (artifact for artifact in new_artifacts if str(artifact.get("name")) == f"iter{iterations}.json"),
                latest_checkpoint_artifact,
            )
            await _emit_checkpoint_viz(
                context=context,
                step=iterations,
                total_iterations=iterations,
                metrics=latest_metrics,
                new_artifacts=new_artifacts,
            )

        progress_payload = {
            "goal": goal,
            "iterations": iterations,
            "checkpoint_every": checkpoint_every,
            "latest_iteration": iterations,
            "metrics": latest_metrics,
            "source": resolved_source,
        }
        await asyncio.to_thread(_write_json, result_dir / "optimization_progress.json", progress_payload)

        resume_state = {
            "result_dir": str(result_dir),
            "latest_iteration": iterations,
            "latest_checkpoint_artifact": latest_checkpoint_artifact,
            "preferred_result_artifact": latest_checkpoint_artifact,
            "persisted_files": sorted(persisted_files),
            "latest_metrics": latest_metrics,
            "source": resolved_source,
        }
        await _save_resume_state(context, resume_state)
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="done",
            label="Optimization finished",
            detail=f"Completed {iterations} optimization iterations.",
        )
        return {"run_state_json": _json_dumps(resume_state)}
    except Exception as exc:
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="failed",
            label="Optimization failed",
            detail=str(exc),
        )
        raise


@tool(outputs=["run_state_json"])
async def export_optimized_lens(
    source_json: str,
    run_state_json: str,
    request_json: str,
    is_stub: bool,
    *,
    context: NodeContext,
) -> dict[str, Any]:
    tool_name = "export_optimized_lens"
    await _send_tool_phase(
        context,
        tool_name=tool_name,
        status="active",
        label="Exporting lens",
        detail="Writing final optimization outputs.",
    )
    if is_stub:
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="done",
            label="Export skipped",
            detail="Stub mode has no export step.",
        )
        return {"run_state_json": run_state_json}

    try:
        request = _json_loads(request_json, {})
        source_payload = _json_loads(source_json, {})
        run_state = _json_loads(run_state_json, {})
        checkpoint_source = dict(run_state.get("latest_checkpoint_artifact") or source_payload.get("source") or {})

        source = await resolve_lens_source(
            resolved_inputs={"lens_source": checkpoint_source},
            context=context,
            active_source_ref=checkpoint_source,
        )
        lens, _source = await asyncio.to_thread(_load_lens_sync, source)
        result_dir = Path(_result_dir_from_state(run_state))
        result_dir.mkdir(parents=True, exist_ok=True)

        export_formats = list(request.get("export_formats") or list(DEFAULT_EXPORT_FORMATS))
        await asyncio.to_thread(
            _write_export_files,
            lens=lens,
            result_dir=str(result_dir),
            export_formats=export_formats,
        )

        persisted_files = set(str(item) for item in (run_state.get("persisted_files") or []))
        new_artifacts, persisted_files = await _persist_new_output_files(
            result_dir=result_dir,
            context=context,
            tag="optimize",
            persisted_files=persisted_files,
        )
        await _emit_checkpoint_viz(
            context=context,
            step=int(run_state.get("latest_iteration") or request.get("iterations") or 0),
            total_iterations=int(request.get("iterations") or 0),
            metrics=dict(run_state.get("latest_metrics") or {}),
            new_artifacts=new_artifacts,
        )

        preferred_result_artifact = next(
            (artifact for artifact in new_artifacts if str(artifact.get("name")) == "optimized_lens.json"),
            run_state.get("preferred_result_artifact") or run_state.get("latest_checkpoint_artifact") or {},
        )
        run_state["preferred_result_artifact"] = preferred_result_artifact
        run_state["persisted_files"] = sorted(persisted_files)
        await _save_resume_state(context, run_state)
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="done",
            label="Export finished",
            detail="Final lens export artifacts are ready.",
        )
        return {"run_state_json": _json_dumps(run_state)}
    except Exception as exc:
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="failed",
            label="Export failed",
            detail=str(exc),
        )
        raise


@tool(outputs=["summary", "result_artifact_id", "result_json", "result_dir"])
async def finalize_optimization_result(
    source_json: str,
    run_state_json: str,
    request_json: str,
    is_stub: bool,
    *,
    context: NodeContext,
) -> dict[str, Any]:
    tool_name = "finalize_optimization_result"
    await _send_tool_phase(
        context,
        tool_name=tool_name,
        status="active",
        label="Finalizing result",
        detail="Persisting summary and selecting the final artifact.",
    )
    request = _json_loads(request_json, {})
    source_payload = _json_loads(source_json, {})
    run_state = _json_loads(run_state_json, {})

    if is_stub:
        result_artifact = dict(run_state.get("preferred_result_artifact") or run_state.get("result_artifact") or {})
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="done",
            label="Finalization finished",
            detail="Stub result finalized.",
        )
        return {
            "summary": str(run_state.get("summary") or "Completed stub DeepLens optimization workflow."),
            "result_artifact_id": str(result_artifact.get("artifact_id") or ""),
            "result_json": "",
            "result_dir": "",
        }

    try:
        result_dir = Path(_result_dir_from_state(run_state))
        result_dir.mkdir(parents=True, exist_ok=True)

        summary_payload = {
            "goal": request.get("goal") or "optimize lens",
            "constraints": request.get("constraints") or [],
            "excluded_objectives": request.get("excluded_objectives") or [],
            "iterations": int(request.get("iterations") or 0),
            "checkpoint_every": int(request.get("checkpoint_every") or 1),
            "source": source_payload.get("source") or run_state.get("source") or {},
            "latest_metrics": run_state.get("latest_metrics") or {},
            "latest_checkpoint_artifact": run_state.get("latest_checkpoint_artifact") or {},
        }
        await asyncio.to_thread(_write_json, result_dir / "optimization_summary.json", summary_payload)

        persisted_files = set(str(item) for item in (run_state.get("persisted_files") or []))
        new_artifacts, persisted_files = await _persist_new_output_files(
            result_dir=result_dir,
            context=context,
            tag="optimize",
            persisted_files=persisted_files,
        )
        run_state["persisted_files"] = sorted(persisted_files)
        await _save_resume_state(context, run_state)

        artifact_pool = list(new_artifacts)
        latest_checkpoint_artifact = run_state.get("latest_checkpoint_artifact")
        if latest_checkpoint_artifact:
            artifact_pool.append(dict(latest_checkpoint_artifact))

        result_json_artifact = dict(run_state.get("preferred_result_artifact") or {})
        if not result_json_artifact:
            result_json_artifact = next(
                (artifact for artifact in artifact_pool if str(artifact.get("name")) == "optimized_lens.json"),
                None,
            )
        if not result_json_artifact:
            result_json_artifact = next(
                (artifact for artifact in artifact_pool if str(artifact.get("name")) == "optimization_summary.json"),
                None,
            )
        if not result_json_artifact and latest_checkpoint_artifact:
            result_json_artifact = dict(latest_checkpoint_artifact)

        source = source_payload.get("source") or run_state.get("source") or {}
        summary = f"Completed DeepLens optimization for `{source.get('name') or 'lens'}`."
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="done",
            label="Finalization finished",
            detail="Optimization outputs are ready.",
        )
        return {
            "summary": summary,
            "result_artifact_id": str((result_json_artifact or {}).get("artifact_id") or ""),
            "result_json": _json_dumps(result_json_artifact or {}),
            "result_dir": str(result_dir),
        }
    except Exception as exc:
        await _send_tool_phase(
            context,
            tool_name=tool_name,
            status="failed",
            label="Finalization failed",
            detail=str(exc),
        )
        raise


def build_generated_optimize_graph(
    *,
    graph_id: str,
    request: dict[str, Any],
) -> TaskGraph:
    intervals = _plan_optimization_intervals(
        iterations=int(request.get("iterations") or 0),
        checkpoint_every=int(request.get("checkpoint_every") or 1),
    )

    with graph(name=graph_id) as g:
        prepared = prepare_optimize_request(
            lens_source=request.get("lens_source"),
            goal=request.get("goal"),
            constraints=request.get("constraints"),
            excluded_objectives=request.get("excluded_objectives"),
            iterations=int(request.get("iterations") or 0),
            checkpoint_every=int(request.get("checkpoint_every") or 1),
            export_formats=list(request.get("export_formats") or list(DEFAULT_EXPORT_FORMATS)),
            use_stub=bool(request.get("use_stub")),
            _id="prepare_optimize_request",
        )
        loaded = load_lens_for_optimization(
            lens_source=request.get("lens_source"),
            request_json=prepared.request_json,
            is_stub=prepared.is_stub,
            _after=[prepared],
            _id="load_lens_for_optimization",
        )

        previous = None
        for index, (start_iteration, end_iteration) in enumerate(intervals):
            interval_node = run_optimization_interval(
                source_json=loaded.source_json,
                request_json=prepared.request_json,
                prior_run_state_json="" if previous is None else previous.run_state_json,
                start_iteration=start_iteration,
                end_iteration=end_iteration,
                is_stub=prepared.is_stub,
                _after=[loaded] if previous is None else [previous],
                _id=f"optimize_interval_{index:03d}",
            )
            previous = interval_node

        if previous is None:
            raise RuntimeError("Generated optimization graph requires at least one interval node.")

        exported = export_optimized_lens(
            source_json=loaded.source_json,
            run_state_json=previous.run_state_json,
            request_json=prepared.request_json,
            is_stub=prepared.is_stub,
            _after=[previous],
            _id="export_optimized_lens",
        )
        finalized = finalize_optimization_result(
            source_json=loaded.source_json,
            run_state_json=exported.run_state_json,
            request_json=prepared.request_json,
            is_stub=prepared.is_stub,
            _after=[exported],
            _id="finalize_optimization_result",
        )
        g.expose("summary", finalized.summary)
        g.expose("result_artifact_id", finalized.result_artifact_id)
        g.expose("result_json", finalized.result_json)
        g.expose("result_dir", finalized.result_dir)
        g.spec.meta.update(
            {
                "generated_from": OPTIMIZE_LAUNCHER_GRAPH_ID,
                "generated_request": copy.deepcopy(request),
                "interval_count": len(intervals),
                "iterations": int(request.get("iterations") or 0),
                "checkpoint_every": int(request.get("checkpoint_every") or 1),
            }
        )
        generated_graph = g

    return generated_graph


@graph_fn(
    name=OPTIMIZE_LAUNCHER_GRAPH_ID,
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
async def deeplens_v7_optimize_launcher(
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
    request = _normalize_request(
        lens_source=lens_source,
        goal=goal,
        constraints=constraints,
        excluded_objectives=excluded_objectives,
        iterations=iterations,
        checkpoint_every=checkpoint_every,
        export_formats=export_formats,
        use_stub=use_stub,
    )
    child_graph_id = _generated_graph_id(request=request, parent_run_id=context.run_id)
    child_graph = build_generated_optimize_graph(graph_id=child_graph_id, request=request)
    child_spec = copy.deepcopy(child_graph.spec)

    def _build() -> TaskGraph:
        return TaskGraph.from_spec(copy.deepcopy(child_spec), state=None)

    _build.__ag_builder__ = True
    _build.build = _build
    _build.graph_name = child_graph_id
    _build.version = "0.1.0"

    context.registry().register(
        nspace="graph",
        name=child_graph_id,
        version="0.1.0",
        obj=_build,
        meta={
            "kind": "graph",
            "flow_id": OPTIMIZE_LAUNCHER_GRAPH_ID,
            "tags": ["ag.deeplens.v7", "workflow:optimization", "generated"],
            "description": "Per-request generated DeepLens optimization graph.",
            "inputs": [],
            "outputs": ["summary", "result_artifact_id", "result_json", "result_dir"],
            "interval_count": len(
                _plan_optimization_intervals(
                    iterations=int(request.get("iterations") or 0),
                    checkpoint_every=int(request.get("checkpoint_every") or 1),
                )
            ),
            "generated_request": copy.deepcopy(request),
        },
    )
    child_run_id, outputs, has_waits, continuations = await context.runner().run_and_wait(
        child_graph_id,
        inputs={},
        tags=["ag.deeplens.v7", "workflow:optimization", "generated"],
    )
    if has_waits:
        wait_summary = (
            f"Optimization child run `{child_run_id}` is waiting in `{child_graph_id}`."
        )
        return {
            "summary": wait_summary,
            "result_artifact_id": "",
            "result_json": _json_dumps(
                {
                    "child_run_id": child_run_id,
                    "child_graph_id": child_graph_id,
                    "continuations": continuations,
                }
            ),
            "result_dir": "",
        }
    if not outputs:
        return {
            "summary": f"Optimization child run `{child_run_id}` completed without outputs.",
            "result_artifact_id": "",
            "result_json": _json_dumps(
                {"child_run_id": child_run_id, "child_graph_id": child_graph_id}
            ),
            "result_dir": "",
        }
    result = dict(outputs)
    result.setdefault("result_json", "")
    result["result_json"] = _json_dumps(
        {
            "child_run_id": child_run_id,
            "child_graph_id": child_graph_id,
            "result": _json_loads(str(result.get("result_json") or ""), result.get("result_json") or {}),
        }
    )
    return {
        "summary": str(result.get("summary") or ""),
        "result_artifact_id": str(result.get("result_artifact_id") or ""),
        "result_json": str(result.get("result_json") or ""),
        "result_dir": str(result.get("result_dir") or ""),
    }


@graphify(
    name="deeplens_v7_optimize_workflow",
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
def deeplens_v7_optimize_workflow(
    lens_source: dict[str, Any] | None = None,
    goal: str | None = None,
    constraints: list[Any] | None = None,
    excluded_objectives: list[Any] | None = None,
    iterations: int = 500,
    checkpoint_every: int = 100,
    export_formats: list[str] | None = None,
    use_stub: bool = False,
):
    prepared = prepare_optimize_request(
        lens_source=lens_source,
        goal=goal,
        constraints=constraints,
        excluded_objectives=excluded_objectives,
        iterations=iterations,
        checkpoint_every=checkpoint_every,
        export_formats=export_formats,
        use_stub=use_stub,
        _id="prepare_optimize_request",
    )
    loaded = load_lens_for_optimization(
        lens_source=lens_source,
        request_json=prepared.request_json,
        is_stub=prepared.is_stub,
        _after=[prepared],
        _id="load_lens_for_optimization",
    )

    optimized = run_optimization_with_checkpoints(
        source_json=loaded.source_json,
        request_json=prepared.request_json,
        is_stub=prepared.is_stub,
        _after=[loaded],
        _id="run_optimization_with_checkpoints",
    )

    exported = export_optimized_lens(
        source_json=loaded.source_json,
        run_state_json=optimized.run_state_json,
        request_json=prepared.request_json,
        is_stub=prepared.is_stub,
        _after=[optimized],
        _id="export_optimized_lens",
    )
    
    finalized = finalize_optimization_result(
        source_json=loaded.source_json,
        run_state_json=exported.run_state_json,
        request_json=prepared.request_json,
        is_stub=prepared.is_stub,
        _after=[exported],
        _id="finalize_optimization_result",
    )
    return {
        "summary": finalized.summary,
        "result_artifact_id": finalized.result_artifact_id,
        "result_json": finalized.result_json,
        "result_dir": finalized.result_dir,
    }
