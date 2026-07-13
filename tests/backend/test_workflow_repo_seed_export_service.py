from __future__ import annotations

import json
from pathlib import Path

from src.backend.workflows import workflow_repo_seed_export_service as export_service


def test_build_repo_seed_workflow_bundle_from_authority_uses_raw_bundle_scaffold(
    monkeypatch,
    tmp_path: Path,
) -> None:
    asset_path = tmp_path / "bundle.json"
    asset_path.write_text(
        json.dumps(
            {
                "family_id": "episode_evaluation_workflow_seed_bundle",
                "managed_by": "episode_evaluation_workflow_vontology_service",
                "schema_version": "repo_seed_workflow_bundle.v1",
                "seed_version": "3",
                "known_legacy_authority_payload_sha256_by_seed_version": {
                    "#V#episode_evaluation_workflow": {"2": ["a" * 64]}
                },
                "source_tag": "JVNAUTOSCI-1665",
                "supported_action_ids": ["episode_critic.build_evidence_bundle"],
                "workflows": [
                    {
                        "workflow_id": "#V#episode_evaluation_workflow",
                        "publication_spec": {
                            "initial_state": "build_evidence",
                            "steps": [],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def _unexpected_loader(_asset_path):
        raise AssertionError("export path must not depend on repo-seed authority loader")

    monkeypatch.setattr(
        export_service.authority_service,
        "load_repo_seed_workflow_bundle",
        _unexpected_loader,
    )
    monkeypatch.setattr(
        export_service,
        "_build_workflow_entry",
        lambda workflow_id, raw_workflow_entry: (
            {
                "workflow_id": workflow_id,
                "display_name": raw_workflow_entry.get("workflow_id"),
                "publication_spec": raw_workflow_entry.get("publication_spec"),
            },
            {"episode_critic.build_evidence_bundle"},
        ),
    )

    payload = export_service.build_repo_seed_workflow_bundle_from_authority(
        asset_path=asset_path
    )

    assert payload == {
        "family_id": "episode_evaluation_workflow_seed_bundle",
        "managed_by": "episode_evaluation_workflow_vontology_service",
        "schema_version": "repo_seed_workflow_bundle.v1",
        "seed_version": "3",
        "known_legacy_authority_payload_sha256_by_seed_version": {
            "#V#episode_evaluation_workflow": {"2": ["a" * 64]}
        },
        "source_tag": "JVNAUTOSCI-1665",
        "supported_action_ids": ["episode_critic.build_evidence_bundle"],
        "workflows": [
            {
                "workflow_id": "#V#episode_evaluation_workflow",
                "display_name": "#V#episode_evaluation_workflow",
                "publication_spec": {
                    "initial_state": "build_evidence",
                    "steps": [],
                },
            }
        ],
    }
