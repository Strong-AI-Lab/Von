"""Seed-only startup publication support for the scholarly-paper workflow family.

The paper repo bundle remains a reviewable bootstrap fixture, not request-time
workflow authority. When the canonical Vontology materialisation is already
current this path is a no-op; when startup detects missing or drifted
materialisation it republishes through the shared seed-bootstrap helper and
returns explicit diagnostics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .workflow_repo_seed_bootstrap import (
    bootstrap_repo_seed_workflow_bundle,
)
from ..workflows.workflow_repo_seed_export_service import (
    diff_repo_seed_workflow_bundle_from_authority,
    write_repo_seed_workflow_bundle_from_authority,
)

SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID = (
    "#V#scholarly_article_metadata_representation_workflow"
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
    """Publish or repair the canonical scholarly-paper workflow family at startup."""

    return bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH
    )


def export_canonical_paper_representation_workflow_repo_seed_bundle(
    *,
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Refresh the paper workflow seed bundle from authoritative Vontology state."""

    return write_repo_seed_workflow_bundle_from_authority(
        asset_path=asset_path or _REPO_SEED_ASSET_PATH
    )


def diff_canonical_paper_representation_workflow_repo_seed_bundle(
    *,
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Diff the paper workflow seed bundle against authoritative Vontology state."""

    return diff_repo_seed_workflow_bundle_from_authority(
        asset_path=asset_path or _REPO_SEED_ASSET_PATH
    )


__all__ = [
    "ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID",
    "SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID",
    "SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID",
    "bootstrap_canonical_paper_representation_workflows",
    "diff_canonical_paper_representation_workflow_repo_seed_bundle",
    "export_canonical_paper_representation_workflow_repo_seed_bundle",
]
