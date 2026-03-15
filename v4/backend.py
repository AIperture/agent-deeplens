from __future__ import annotations

import json
import mimetypes
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


LENS_SUFFIXES = {".json", ".zmx"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif"}

def _artifact_to_ref(artifact: Any) -> dict[str, Any]:
    labels = getattr(artifact, "labels", None) or {}
    return {
        "artifact_id": getattr(artifact, "artifact_id", None),
        "uri": getattr(artifact, "uri", None),
        "name": labels.get("filename") or getattr(artifact, "name", None) or Path(str(getattr(artifact, "uri", "") or "artifact")).name,
        "kind": getattr(artifact, "kind", "file"),
        "mime": getattr(artifact, "mime", None),
    }



def _file_uri_to_path(uri: str) -> str:
    if uri.startswith("file:///"):
        parsed = urlparse(uri)
        local = unquote(parsed.path)
        if len(local) >= 3 and local[0] == "/" and local[2] == ":":
            local = local[1:]
        return local
    return uri


def _ensure_deeplens_importable() -> None:
    repo_pkg_root = Path(__file__).resolve().parents[1] / "deeplens"
    repo_pkg_root_str = str(repo_pkg_root)
    if repo_pkg_root.exists() and repo_pkg_root_str not in sys.path:
        sys.path.insert(0, repo_pkg_root_str)


def _import_deeplens() -> tuple[Any, Any]:
    _ensure_deeplens_importable()
    import matplotlib

    matplotlib.use("Agg")

    from deeplens import GeoLens  # type: ignore
    from deeplens.optics.geolens_pkg.utils import create_lens  # type: ignore

    return GeoLens, create_lens


def _is_lens_name(name: str | None) -> bool:
    return bool(name and Path(name).suffix.lower() in LENS_SUFFIXES)


def _candidate_name(ref: dict[str, Any]) -> str:
    return str(
        ref.get("name")
        or ref.get("filename")
        or Path(str(ref.get("uri") or ref.get("url") or "lens")).name
    )


def _normalize_upload_ref(ref: Any) -> dict[str, Any]:
    if isinstance(ref, dict):
        return ref
    value = getattr(ref, "value", None)
    if isinstance(value, dict):
        return value
    return {
        "name": getattr(ref, "name", None),
        "filename": getattr(ref, "filename", None),
        "artifact_id": getattr(ref, "artifact_id", None),
        "uri": getattr(ref, "uri", None),
        "url": getattr(ref, "url", None),
    }


def artifact_to_ref(artifact: Any) -> dict[str, Any]:
    labels = getattr(artifact, "labels", None) or {}
    return {
        "artifact_id": getattr(artifact, "artifact_id", None),
        "uri": getattr(artifact, "uri", None),
        "name": labels.get("filename")
        or getattr(artifact, "name", None)
        or Path(str(getattr(artifact, "uri", "") or "artifact")).name,
        "kind": getattr(artifact, "kind", "file"),
        "mime": getattr(artifact, "mime", None),
    }


async def resolve_lens_source(
    *,
    resolved_inputs: dict[str, Any],
    task: Any = None,
    state: Any = None,
    context: Any,
    attachments: list[dict[str, Any]] | None = None,
    active_source_ref: dict[str, Any] | None = None,
    active_lens_ref: str | None = None,
) -> dict[str, Any]:
    task_lens_source = getattr(task, "lens_source", {}) if task else {}
    task_sources = attachments or (getattr(task, "source_refs", None) or getattr(task, "attachments", None) or [])
    state_source = active_source_ref or (getattr(state, "active_source_ref", {}) if state else {})
    state_lens = active_lens_ref or (getattr(state, "active_lens_ref", None) if state else None)

    explicit = (
        resolved_inputs.get("lens_source")
        or resolved_inputs.get("source_ref")
        or resolved_inputs.get("attachment")
        or task_lens_source
        or {}
    )
    candidates: list[dict[str, Any]] = []
    if isinstance(explicit, dict) and explicit:
        candidates.append(explicit)
    for item in task_sources or []:
        if isinstance(item, dict):
            candidates.append(item)
    if state_source:
        candidates.append(state_source)
    if state_lens:
        candidates.append({"artifact_id": state_lens})

    for candidate in candidates:
        artifact_id = candidate.get("artifact_id")
        uri = candidate.get("uri") or candidate.get("url") or candidate.get("path")
        name = _candidate_name(candidate)
        if artifact_id:
            path = str(await context.artifacts().as_local_file_by_id(str(artifact_id)))
            return {"artifact_id": artifact_id, "uri": uri, "path": path, "name": name}
        if uri:
            uri_str = str(uri)
            if Path(uri_str).exists() and (_is_lens_name(name) or _is_lens_name(uri_str)):
                return {"artifact_id": artifact_id, "uri": uri_str, "path": uri_str, "name": name}
            try:
                path = str(await context.artifacts().as_local_file(uri_str))
                if _is_lens_name(name) or _is_lens_name(path):
                    return {"artifact_id": artifact_id, "uri": uri_str, "path": path, "name": name}
            except Exception:
                continue

    uploads = await context.channel("ui:session").get_latest_uploads(clear=False)
    for upload in uploads:
        upload_ref = _normalize_upload_ref(upload)
        name = _candidate_name(upload_ref)
        if not _is_lens_name(name):
            continue
        artifact_id = upload_ref.get("artifact_id") or upload_ref.get("uri")
        if artifact_id:
            try:
                path = str(await context.artifacts().as_local_file_by_id(str(artifact_id)))
                return {"artifact_id": artifact_id, "uri": upload_ref.get("uri"), "path": path, "name": name}
            except Exception:
                uri = upload_ref.get("uri")
                if uri and Path(str(uri)).exists():
                    return {"artifact_id": artifact_id, "uri": uri, "path": str(uri), "name": name}

    raise FileNotFoundError("No DeepLens .json or .zmx lens source is available.")


async def load_lens(
    *,
    resolved_inputs: dict[str, Any],
    task: Any = None,
    state: Any = None,
    context: Any,
    active_source_ref: dict[str, Any] | None = None,
    active_lens_ref: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    source = await resolve_lens_source(
        resolved_inputs=resolved_inputs,
        task=task,
        state=state,
        context=context,
        active_source_ref=active_source_ref,
        active_lens_ref=active_lens_ref,
    )
    geo_lens, _create_lens = _import_deeplens()
    lens = geo_lens(filename=source["path"])
    return lens, source


from .extraction import missing_design_fields  # noqa: E402


def normalize_design_spec(payload: dict[str, Any]) -> dict[str, Any]:
    spec = dict(payload or {})
    spec.setdefault("save_name", "deeplens_design")
    spec.setdefault("baseline_analysis", True)
    return spec


async def create_lens_design(
    *,
    resolved_inputs: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    spec = normalize_design_spec(resolved_inputs.get("design_spec") or {})
    _, create_lens = _import_deeplens()
    result_dir = Path(await context.artifacts().stage_dir("_deeplens_design"))
    result_dir.mkdir(parents=True, exist_ok=True)
    lens = create_lens(
        fov=spec["fov"],
        fnum=spec["fnum"],
        bfl=spec.get("bfl", 3.0),
        foclen=spec.get("foclen"),
        imgh=spec.get("imgh"),
        thickness=spec.get("thickness"),
        surf_list=spec.get("surf_list")
        if spec.get("surf_list") is not None
        else [["Aspheric", "Aspheric"], ["Aperture"], ["Aspheric", "Aspheric"], ["Aspheric", "Aspheric"]],
        save_dir=str(result_dir),
    )
    return {"lens": lens, "spec": spec, "result_dir": result_dir}


def _kind_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix in {".txt", ".log"}:
        return "text"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    return "file"


async def persist_output_files(
    *,
    result_dir: Path,
    context: Any,
    tag: str,
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for path in sorted(result_dir.rglob("*")):
        if not path.is_file():
            continue
        artifact = await context.artifacts().save_file(
            path=str(path),
            kind=_kind_for_path(path),
            name=path.name,
            labels={"tag": tag},
        )
        artifacts.append(artifact_to_ref(artifact))
    return artifacts


async def deliver_artifacts(
    *,
    artifacts: list[dict[str, Any]],
    context: Any,
    limit: int = 6,
) -> None:
    for artifact in artifacts[:limit]:
        uri = artifact.get("uri")
        if not uri:
            continue
        local_path = _file_uri_to_path(str(uri))
        name = artifact.get("name") or Path(local_path).name
        if Path(str(name)).suffix.lower() in IMAGE_SUFFIXES:
            await context.channel("ui:session").send_image(
                url=local_path,
                alt=str(name),
                title=str(name),
                memory_log=False,
            )
        else:
            await context.channel("ui:session").send_file(
                url=local_path,
                filename=str(name),
                title=str(name),
                memory_log=False,
            )


def summarize_artifacts(artifacts: list[dict[str, Any]]) -> list[str]:
    return [str(a.get("name") or a.get("artifact_id") or "artifact") for a in artifacts[:6]]


def analysis_mode_from_request(analysis_request: dict[str, Any]) -> str:
    mode = str(analysis_request.get("mode") or "full").lower()
    return mode if mode in {"full", "spot", "mtf", "rms"} else "full"


async def run_analysis(
    *,
    resolved_inputs: dict[str, Any],
    task: Any,
    state: Any,
    context: Any,
) -> dict[str, Any]:
    use_stub = bool(resolved_inputs.get("use_stub"))
    analysis_request = dict(getattr(task, "analysis_request", {}) or {})
    analysis_request.update(resolved_inputs.get("analysis_request") or {})
    if resolved_inputs.get("mode"):
        analysis_request["mode"] = resolved_inputs["mode"]
    mode = analysis_mode_from_request(analysis_request)

    if use_stub:
        summary_artifact = await context.artifacts().save_text(
            "Stub DeepLens analysis completed.",
            name="deeplens-analysis-summary.txt",
            labels={"mode": mode, "stub": True},
        )
        artifacts = [artifact_to_ref(summary_artifact)]
        await deliver_artifacts(artifacts=artifacts, context=context)
        return {
            "summary": "Completed stub DeepLens analysis.",
            "artifacts": artifacts,
            "metrics": {"mode": mode, "stub": True},
            "active_lens_ref": state.active_lens_ref,
            "active_source_ref": state.active_source_ref,
        }

    source: dict[str, Any] | None = None
    try:
        lens, source = await load_lens(
            resolved_inputs=resolved_inputs,
            task=task,
            state=state,
            context=context,
        )
    except FileNotFoundError:
        design_spec = normalize_design_spec(resolved_inputs.get("design_spec") or {})
        if missing_design_fields(design_spec):
            raise
        created = await create_lens_design(
            resolved_inputs={**resolved_inputs, "design_spec": design_spec},
            context=context,
        )
        lens = created["lens"]
        created_artifacts = await persist_output_files(
            result_dir=created["result_dir"],
            context=context,
            tag="design",
        )
        source = next(
            (artifact for artifact in created_artifacts if str(artifact.get("name")).endswith(".json")),
            {"name": design_spec.get("save_name") or "created_lens"},
        )
        await deliver_artifacts(artifacts=created_artifacts, context=context)

    result_dir = Path(await context.artifacts().stage_dir("_deeplens_analysis"))
    result_dir.mkdir(parents=True, exist_ok=True)
    base = result_dir / "analysis"
    metrics = lens.analysis_spot()

    if mode == "spot":
        lens.draw_spot_radial(save_name=str(result_dir / "analysis_spot.png"))
        lens.draw_spot_map(save_name=str(result_dir / "analysis_spot_map.png"))
    elif mode == "mtf":
        lens.draw_mtf(save_name=str(result_dir / "analysis_mtf.png"))
    elif mode == "rms":
        rms_r, rms_g, rms_b = lens.rms_map_rgb()
        rms_payload = {
            "rms_r_shape": list(rms_r.shape),
            "rms_g_shape": list(rms_g.shape),
            "rms_b_shape": list(rms_b.shape),
        }
        (result_dir / "analysis_rms.json").write_text(json.dumps(rms_payload, indent=2), encoding="utf-8")
    else:
        lens.analysis(save_name=str(base), full_eval=True)

    export_json = result_dir / "active_lens.json"
    export_zmx = result_dir / "active_lens.zmx"
    lens.write_lens_json(str(export_json))
    try:
        lens.write_lens_zmx(str(export_zmx))
    except Exception:
        pass

    metrics_path = result_dir / "analysis_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    artifacts = await persist_output_files(result_dir=result_dir, context=context, tag="analysis")
    await deliver_artifacts(artifacts=artifacts, context=context)
    active_lens_ref = next(
        (artifact.get("artifact_id") for artifact in artifacts if str(artifact.get("name")).endswith(".json")),
        source.get("artifact_id") if source else state.active_lens_ref,
    )
    return {
        "summary": f"Completed DeepLens {mode} analysis for `{source['name']}`.",
        "artifacts": artifacts,
        "metrics": metrics,
        "active_lens_ref": active_lens_ref,
        "active_source_ref": source,
    }


async def export_lens(
    *,
    resolved_inputs: dict[str, Any],
    task: Any,
    state: Any,
    context: Any,
) -> dict[str, Any]:
    use_stub = bool(resolved_inputs.get("use_stub"))
    formats = resolved_inputs.get("formats") or ["json", "zmx"]
    if use_stub:
        summary_artifact = await context.artifacts().save_text(
            f"Stub export completed for formats: {formats}",
            name="deeplens-export.txt",
            labels={"stub": True},
        )
        artifacts = [artifact_to_ref(summary_artifact)]
        await deliver_artifacts(artifacts=artifacts, context=context)
        return {
            "summary": "Completed stub DeepLens export.",
            "artifacts": artifacts,
            "active_lens_ref": state.active_lens_ref,
            "active_source_ref": state.active_source_ref,
        }

    lens, source = await load_lens(
        resolved_inputs=resolved_inputs,
        task=task,
        state=state,
        context=context,
    )
    result_dir = Path(await context.artifacts().stage_dir("_deeplens_export"))
    result_dir.mkdir(parents=True, exist_ok=True)
    if "json" in formats:
        lens.write_lens_json(str(result_dir / "exported_lens.json"))
    if "zmx" in formats:
        try:
            lens.write_lens_zmx(str(result_dir / "exported_lens.zmx"))
        except Exception:
            pass
    artifacts = await persist_output_files(result_dir=result_dir, context=context, tag="export")
    await deliver_artifacts(artifacts=artifacts, context=context)
    active_lens_ref = next(
        (artifact.get("artifact_id") for artifact in artifacts if str(artifact.get("name")).endswith(".json")),
        source.get("artifact_id") or state.active_lens_ref,
    )
    return {
        "summary": f"Exported lens `{source['name']}`.",
        "artifacts": artifacts,
        "active_lens_ref": active_lens_ref,
        "active_source_ref": source,
    }


def build_optimization_inputs(
    *,
    resolved_inputs: dict[str, Any],
    task: Any,
    source: dict[str, Any] | None,
    state: Any,
) -> dict[str, Any]:
    run_request = dict(getattr(task, "run_request", {}) or {})
    run_request.update(resolved_inputs.get("run_request") or {})
    return {
        "lens_source": source or getattr(task, "lens_source", {}) or state.active_source_ref,
        "goal": run_request.get("goal") or getattr(task, "user_goal", ""),
        "constraints": run_request.get("constraints") or [],
        "excluded_objectives": run_request.get("excluded_objectives") or [],
        "iterations": int(run_request.get("iterations") or 500),
        "checkpoint_every": int(run_request.get("checkpoint_every") or 100),
        "export_formats": run_request.get("export_formats") or ["json", "zmx"],
        "use_stub": bool(run_request.get("use_stub") or resolved_inputs.get("use_stub")),
    }


def summarize_status(record: Any, outputs: dict[str, Any] | None = None) -> str:
    status = str(getattr(record, "status", "unknown"))
    summary = f"Run status: `{status}`."
    if outputs:
        if outputs.get("summary"):
            summary += f"\n{outputs['summary']}"
        if outputs.get("result_artifact_id"):
            summary += f"\nResult artifact: `{outputs['result_artifact_id']}`."
    return summary


def maybe_extract_filename(uri: str | None) -> str | None:
    return Path(uri).name if uri else None


def load_text_or_json_payload(raw_text: str) -> Any:
    try:
        return json.loads(raw_text)
    except Exception:
        return raw_text


def guess_mime(name: str) -> str | None:
    return mimetypes.guess_type(name)[0]
