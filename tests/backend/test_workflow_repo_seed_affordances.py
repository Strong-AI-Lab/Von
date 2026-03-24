from __future__ import annotations

from pathlib import Path

from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows import workflow_template_profile_service as template_service


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_repo_seed_layout_replaces_old_authored_source_paths() -> None:
    assert not (
        PROJECT_ROOT / "src/backend/workflows/authored_sources"
    ).exists()
    assert (
        PROJECT_ROOT / "src/backend/workflows/repo_seed_bundles"
    ).is_dir()
    assert not (
        PROJECT_ROOT / "src/backend/services/workflow_authored_source_bootstrap.py"
    ).exists()
    assert (
        PROJECT_ROOT / "src/backend/services/workflow_repo_seed_bootstrap.py"
    ).is_file()


def test_workflow_seed_helpers_no_longer_export_authoritative_sounding_names() -> None:
    assert not hasattr(authority_service, "load_authored_workflow_source_bundle")
    assert not hasattr(authority_service, "clear_authored_workflow_source_bundle_cache")
    assert not hasattr(authority_service, "upsert_authored_text_relations")
    assert not hasattr(template_service, "load_authored_workflow_template_bundle")
    assert not hasattr(template_service, "clear_authored_workflow_template_bundle_cache")
    assert not hasattr(template_service, "ensure_seeded_workflow_template_bundle")
