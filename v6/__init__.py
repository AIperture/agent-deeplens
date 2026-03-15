from pathlib import Path

from aethergraph.core.runtime.runtime_services import register_skills_from_path

from .agent import deeplens_agent
from .workflows import deeplens_v6_optimize_workflow

register_skills_from_path(Path(__file__).parent / "skills")

__all__ = ["deeplens_agent", "deeplens_v6_optimize_workflow"]
