from __future__ import annotations


def fake_heavy_run_reply(*, branch: object, message: str) -> str:
    branch_name = getattr(branch, "value", str(branch))
    return (
        f"Queued heavy `{branch_name}` work is still scaffolded in this agent.\n\n"
        f"Request: {message.strip() or '(empty request)'}\n"
        "The next implementation step is to replace this placeholder with the real DeepLens workflow."
    )


def heavy_pathway_placeholder(*, branch: object) -> str:
    branch_name = getattr(branch, "value", str(branch))
    return (
        f"Heavy `{branch_name}` execution is enabled in principle, but this scaffold still needs "
        "the concrete DeepLens workflow wiring."
    )
