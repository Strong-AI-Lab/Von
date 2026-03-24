"""Materialise canonical talk workflows from repo seed bundles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .talk_representation_service import ensure_talk_representation_primitives
from .workflow_repo_seed_bootstrap import (
    bootstrap_repo_seed_workflow_bundle,
)

TALK_REPRESENTATION_WORKFLOW_ID = "#V#talk_representation_workflow"
TECHNICAL_SCIENTIFIC_TALK_REPRESENTATION_WORKFLOW_ID = (
    "#V#technical_scientific_talk_representation_workflow"
)
ACADEMIC_PRESENTATION_INSTANCE_WORKFLOW_ID = "#V#academic_presentation_instance_workflow"

_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "talk_representation_workflow_seed_bundle.json"
)


def bootstrap_canonical_talk_representation_workflows() -> dict[str, Any]:
    """Publish and validate the canonical talk workflow family."""

    ensure_talk_representation_primitives()
    return bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH
    )


__all__ = [
    "ACADEMIC_PRESENTATION_INSTANCE_WORKFLOW_ID",
    "TALK_REPRESENTATION_WORKFLOW_ID",
    "TECHNICAL_SCIENTIFIC_TALK_REPRESENTATION_WORKFLOW_ID",
    "bootstrap_canonical_talk_representation_workflows",
]
