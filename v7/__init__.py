from __future__ import annotations

from pathlib import Path

deeplens_agent = None
deeplens_v7_optimize_launcher = None
deeplens_v7_optimize_workflow = None

try:
    from .agent import deeplens_agent
except Exception:
    deeplens_agent = None

try:
    from .workflows import deeplens_v7_optimize_launcher, deeplens_v7_optimize_workflow
except Exception:
    deeplens_v7_optimize_launcher = None
    deeplens_v7_optimize_workflow = None

try:
    from aethergraph.core.runtime.runtime_services import register_skills_from_path
except Exception:
    register_skills_from_path = None

if register_skills_from_path is not None:
    register_skills_from_path(Path(__file__).parent / "skills")

__all__ = ["deeplens_agent", "deeplens_v7_optimize_launcher", "deeplens_v7_optimize_workflow"]
