from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from src.backend.services import workflow_repo_seed_bootstrap as seed_bootstrap
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


def test_repo_workflow_seed_bundles_declare_seed_version() -> None:
    bundle_dir = PROJECT_ROOT / "src/backend/workflows/repo_seed_bundles"
    missing: list[str] = []
    for path in sorted(bundle_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "repo_seed_workflow_bundle.v1":
            continue
        if not str(payload.get("seed_version") or "").strip():
            missing.append(path.name)

    assert missing == []


def test_repo_seed_version_gate_skips_equal_vontology_version_without_republishing(
    monkeypatch,
) -> None:
    bundle = {
        "asset_path": "seed_bundle.json",
        "family_id": "test_family",
        "managed_by": "test",
        "seed_version": "2",
        "source_tag": "test-source",
        "supported_action_ids": (),
        "publication_specs": {"#V#test_workflow": object()},
        "publication_purposes": {},
        "workflow_type_ids": {},
        "workflow_text_relations": {},
        "workflow_launch_input_contracts": {},
        "step_text_relations": {},
    }
    invalidations: list[bool] = []
    validation_calls: list[dict] = []

    monkeypatch.setattr(
        authority_service,
        "load_repo_seed_workflow_bundle",
        lambda _asset_path: bundle,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_workflow_repo_seed_version_marker",
        lambda _workflow_id: {"seed_version": "2"},
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_validate_existing_materialisation",
        lambda **kwargs: validation_calls.append(kwargs)
        or (
            True,
            {"#V#test_workflow": {"valid": True}},
            {
                "already_current": True,
                "drift_detected": False,
                "current_workflow_ids": ["#V#test_workflow"],
                "drift_workflow_ids": [],
                "issue_codes": [],
                "workflow_status_by_id": {
                    "#V#test_workflow": {"status": "current"}
                },
                "bundle_snapshot_drift_detected": False,
                "bundle_snapshot_drift_workflow_ids": [],
                "bundle_snapshot_issue_codes": [],
                "bundle_snapshot_status_by_id": {},
            },
        ),
    )
    monkeypatch.setattr(
        authority_service,
        "publish_canonical_chat_workflow_graphs",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("version gate should skip publication")
        ),
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "invalidate_workflow_discovery_executability_caches",
        lambda: invalidations.append(True),
    )

    result = seed_bootstrap.bootstrap_repo_seed_workflow_bundle(
        asset_path="seed_bundle.json"
    )

    assert result["publication"]["skip_reason"] == "existing_materialisation_valid"
    assert result["publication"]["counts"]["workflows_published"] == 0
    assert result["publication"]["skipped_due_to_seed_version_not_newer"] == [
        "#V#test_workflow"
    ]
    assert result["repo_seed_version_gate"]["blocked_workflow_ids"] == [
        "#V#test_workflow"
    ]
    assert validation_calls
    assert invalidations == []


def test_repo_seed_newer_version_refreshes_current_materialisation_metadata(
    monkeypatch,
) -> None:
    bundle = {
        "asset_path": "seed_bundle.json",
        "family_id": "test_family",
        "managed_by": "test",
        "seed_version": "2",
        "source_tag": "test-source",
        "supported_action_ids": (),
        "publication_specs": {"#V#test_workflow": SimpleNamespace(steps=())},
        "publication_purposes": {},
        "workflow_type_ids": {},
        "workflow_text_relations": {
            "#V#test_workflow": [
                {"predicate": "#V#hasWorkflowDescription", "text": "new routing text"}
            ]
        },
        "workflow_launch_input_contracts": {},
        "step_text_relations": {},
    }
    marker_updates: list[dict] = []
    invalidations: list[bool] = []
    publications: list[dict] = []
    text_updates: list[dict] = []

    monkeypatch.setattr(
        authority_service,
        "load_repo_seed_workflow_bundle",
        lambda _asset_path: bundle,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_workflow_repo_seed_version_marker",
        lambda _workflow_id: {"seed_version": "1"},
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_validate_existing_materialisation",
        lambda **_kwargs: (
            True,
            {"#V#test_workflow": {"valid": True}},
            {
                "already_current": True,
                "drift_detected": False,
                "current_workflow_ids": ["#V#test_workflow"],
                "drift_workflow_ids": [],
                "issue_codes": [],
                "workflow_status_by_id": {
                    "#V#test_workflow": {"status": "current"}
                },
                "bundle_snapshot_drift_detected": False,
                "bundle_snapshot_drift_workflow_ids": [],
                "bundle_snapshot_issue_codes": [],
                "bundle_snapshot_status_by_id": {},
            },
        ),
    )
    monkeypatch.setattr(
        authority_service,
        "publish_canonical_chat_workflow_graphs",
        lambda **kwargs: publications.append(kwargs)
        or {
            "counts": {
                "workflows_targeted": 1,
                "workflows_published": 1,
                "workflows_skipped_missing_registration": 0,
                "workflows_skipped_missing_concept": 0,
                "step_concepts_created": 0,
                "action_concepts_created": 0,
                "mapping_concepts_created": 0,
                "validation_failures": 0,
                "errors": 0,
            },
            "published_workflow_ids": ["#V#test_workflow"],
        },
    )
    monkeypatch.setattr(
        authority_service,
        "_build_definition_map_from_publication_specs",
        lambda _specs: {"#V#test_workflow": object()},
    )
    monkeypatch.setattr(
        authority_service,
        "upsert_seed_bundle_text_relations",
        lambda **kwargs: text_updates.append(kwargs),
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "build_workflow_process_graph",
        lambda _workflow_id: ({}, []),
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "load_workflow_definition_from_vontology",
        lambda _workflow_id: object(),
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "validate_workflow_definition_contract",
        lambda **_kwargs: {"valid": True, "errors": []},
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_upsert_workflow_repo_seed_version_marker",
        lambda **kwargs: marker_updates.append(kwargs)
        or {"relation_created": True, "replaced_count": 0},
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "invalidate_workflow_discovery_executability_caches",
        lambda: invalidations.append(True),
    )

    result = seed_bootstrap.bootstrap_repo_seed_workflow_bundle(
        asset_path="seed_bundle.json"
    )

    assert result["publication"].get("skip_reason") is None
    assert result["publication"]["materialisation_status"] == "repo_seed_version_refresh"
    assert result["publication"]["counts"]["workflows_published"] == 1
    assert result["publication"]["repo_seed_version_refresh_workflow_ids"] == [
        "#V#test_workflow"
    ]
    assert result["repo_seed_version_gate"]["refresh_workflow_ids"] == [
        "#V#test_workflow"
    ]
    assert publications
    assert text_updates and text_updates[0]["relation_specs"] == tuple(
        bundle["workflow_text_relations"]["#V#test_workflow"]
    )
    assert marker_updates == [
        {
            "workflow_id": "#V#test_workflow",
            "seed_version": "2",
            "family_id": "test_family",
            "source_tag": "test-source",
            "managed_by": "test",
            "asset_path": "seed_bundle.json",
        }
    ]
    assert invalidations == [True]
