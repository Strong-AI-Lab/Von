"""Materialise canonical scholarly-paper and arXiv workflows from repo seed bundles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .workflow_repo_seed_bootstrap import (
    bootstrap_repo_seed_workflow_bundle,
)

SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID = "#V#scholarly_paper_representation_workflow"
ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID = "#V#arxiv_paper_representation_workflow"

_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "paper_representation_workflow_seed_bundle.json"
)


def bootstrap_canonical_paper_representation_workflows() -> dict[str, Any]:
    """Publish and validate the canonical scholarly-paper workflow family."""

    return bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH
    )


__all__ = [
    "ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID",
    "SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID",
    "bootstrap_canonical_paper_representation_workflows",
]
