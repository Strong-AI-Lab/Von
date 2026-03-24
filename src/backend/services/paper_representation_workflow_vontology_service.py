"""Materialise canonical scholarly-paper and arXiv workflows from authored sources."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .workflow_authored_source_bootstrap import (
    bootstrap_authored_workflow_source_bundle,
)

SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID = "#V#scholarly_paper_representation_workflow"
ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID = "#V#arxiv_paper_representation_workflow"

_AUTHORED_SOURCE_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "authored_sources"
    / "paper_representation_workflows.json"
)


def bootstrap_canonical_paper_representation_workflows() -> dict[str, Any]:
    """Publish and validate the canonical scholarly-paper workflow family."""

    return bootstrap_authored_workflow_source_bundle(
        asset_path=_AUTHORED_SOURCE_ASSET_PATH
    )


__all__ = [
    "ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID",
    "SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID",
    "bootstrap_canonical_paper_representation_workflows",
]
