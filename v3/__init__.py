from pathlib import Path

from .agent import deeplens_agent
from .workflows import deeplens_v3_optimize_workflow

try:
    from aethergraph.core.runtime.runtime_services import register_skills_from_path
except Exception:
    register_skills_from_path = None

if register_skills_from_path is not None:
    register_skills_from_path(Path(__file__).parent / "skills")

__all__ = ["deeplens_agent", "deeplens_v3_optimize_workflow"]
