from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.backend.services import workflow_repo_seed_bootstrap as seed_bootstrap
from src.backend.workflows import (
    workflow_concept_authority_service as authority_service,
)
from src.backend.workflows import workflow_template_profile_service as template_service

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_repo_seed_layout_replaces_old_authored_source_paths() -> None:
    assert not (PROJECT_ROOT / "src/backend/workflows/authored_sources").exists()
    assert (PROJECT_ROOT / "src/backend/workflows/repo_seed_bundles").is_dir()
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
    assert not hasattr(
        template_service, "clear_authored_workflow_template_bundle_cache"
    )
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


def test_support_concepts_materialise_relationships_and_text_relations(
    monkeypatch,
) -> None:
    docs = {
        "#V#policy": {
            "concept_id": "#V#policy",
            "relationships": {"#V#has_stage_configuration": ["#V#existing_config"]},
        }
    }
    created = []
    updates = []
    text_updates = []

    def _get_concept(concept_id):
        return docs.get(concept_id)

    def _create_concept(**kwargs):
        concept_id = kwargs["concept_id"]
        doc = {
            "concept_id": concept_id,
            "relationships": {
                "is_an_instance_of": list(kwargs.get("parent_concept_ids") or [])
            },
        }
        docs[concept_id] = doc
        created.append(kwargs)
        return doc

    def _update_concept(concept_id, update_data, **kwargs):
        updates.append((concept_id, update_data, kwargs))
        doc = docs[concept_id]
        doc["relationships"] = dict(update_data["relationships"])
        return doc

    monkeypatch.setattr(
        seed_bootstrap.concept_service,
        "get_concept_by_concept_id_exact",
        _get_concept,
    )
    monkeypatch.setattr(
        seed_bootstrap.concept_service,
        "create_concept",
        _create_concept,
    )
    monkeypatch.setattr(
        seed_bootstrap.concept_service,
        "update_concept",
        _update_concept,
    )
    monkeypatch.setattr(
        seed_bootstrap.authority_service,
        "upsert_seed_bundle_text_relations",
        lambda **kwargs: text_updates.append(kwargs),
    )

    report = seed_bootstrap._materialise_support_concepts(
        [
            {
                "concept_id": "#V#policy",
                "name": "Policy",
                "relationships": {"#V#has_stage_configuration": ["#V#mail_config"]},
                "text_relations": [
                    {"predicate": "#V#has_max_fallback_hops", "text": "2"}
                ],
            },
            {
                "concept_id": "#V#mail_config",
                "name": "Mail Config",
                "parent_concept_ids": ["#V#workflow_stage_configuration"],
                "create_as_instance": True,
                "relationships": {"#V#applies_to_workflow_stage": "#V#mail_stage"},
            },
        ],
        source_tag="test-source",
        managed_by="test-managed",
    )

    assert report["errors"] == []
    assert report["existing_concept_ids"] == ["#V#policy"]
    assert report["created_concept_ids"] == ["#V#mail_config"]
    assert created[0]["defer_text_relations"] is True
    assert docs["#V#policy"]["relationships"]["#V#has_stage_configuration"] == [
        "#V#existing_config",
        "#V#mail_config",
    ]
    assert docs["#V#mail_config"]["relationships"]["#V#applies_to_workflow_stage"] == [
        "#V#mail_stage"
    ]
    assert report["relationship_updated_concept_ids"] == [
        "#V#policy",
        "#V#mail_config",
    ]
    assert report["text_relation_updated_concept_ids"] == ["#V#policy"]
    assert text_updates[0]["subject_concept_id"] == "#V#policy"
    assert text_updates[0]["source_tag"] == "test-source"
    assert text_updates[0]["managed_by"] == "test-managed"
    assert all(update[2]["defer_side_effects"] is True for update in updates)


def test_support_concept_bootstrap_preserves_existing_represented_authority(
    monkeypatch,
) -> None:
    existing = {
        "concept_id": "#V#represented_prompt",
        "relationships": {"#V#hasPolicy": ["#V#human_policy"]},
    }
    monkeypatch.setattr(
        seed_bootstrap.concept_service,
        "get_concept_by_concept_id_exact",
        lambda _concept_id: existing,
    )
    monkeypatch.setattr(
        seed_bootstrap.concept_service,
        "update_concept",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing represented support authority must not change")
        ),
    )
    monkeypatch.setattr(
        seed_bootstrap.authority_service,
        "upsert_seed_bundle_text_relations",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing represented support text must not change")
        ),
    )

    report = seed_bootstrap._materialise_support_concepts(
        [
            {
                "concept_id": "#V#represented_prompt",
                "name": "Represented Prompt",
                "relationships": {"#V#hasPolicy": ["#V#repo_policy"]},
                "text_relations": [
                    {"predicate": "#V#hasPromptText", "text": "repo prompt"}
                ],
            }
        ],
        source_tag="test-source",
        managed_by="test-managed",
        update_existing=False,
    )

    assert report["existing_concept_ids"] == ["#V#represented_prompt"]
    assert report["relationship_updated_concept_ids"] == []
    assert report["text_relation_updated_concept_ids"] == []
    assert existing["relationships"] == {"#V#hasPolicy": ["#V#human_policy"]}


def test_support_concept_receipt_rejects_human_edit_after_interruption(
    monkeypatch,
) -> None:
    concept_id = "#V#represented_prompt"
    docs: dict[str, dict[str, Any]] = {}
    live_text: dict[str, str | None] = {"value": None}

    def _get_concept(requested_id: str) -> dict[str, Any]:
        if requested_id not in docs:
            raise seed_bootstrap.concept_service.ConceptNotFoundError(requested_id)
        return docs[requested_id]

    def _create_concept(**kwargs: Any) -> dict[str, Any]:
        doc = {
            "concept_id": kwargs["concept_id"],
            "relationships": {},
            "attributes": dict(kwargs.get("attributes") or {}),
        }
        docs[kwargs["concept_id"]] = doc
        return doc

    def _update_concept(
        requested_id: str,
        update_data: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        docs[requested_id].update(update_data)
        return docs[requested_id]

    monkeypatch.setattr(
        seed_bootstrap.concept_service,
        "get_concept_by_concept_id_exact",
        _get_concept,
    )
    monkeypatch.setattr(
        seed_bootstrap.concept_service,
        "create_concept",
        _create_concept,
    )
    monkeypatch.setattr(
        seed_bootstrap.concept_service,
        "update_concept",
        _update_concept,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: (
            [
                {
                    "predicate": "#V#hasPromptText",
                    "lang": "en-NZ",
                    "text": live_text["value"],
                }
            ]
            if live_text["value"] is not None
            else []
        ),
    )
    monkeypatch.setattr(
        seed_bootstrap.authority_service,
        "upsert_seed_bundle_text_relations",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated support write interruption")
        ),
    )
    spec = {
        "concept_id": concept_id,
        "name": "Represented Prompt",
        "text_relations": [
            {
                "predicate": "#V#hasPromptText",
                "lang": "en-NZ",
                "text": "repo prompt",
            }
        ],
    }

    first_report = seed_bootstrap._materialise_support_concepts(
        [spec],
        source_tag="test-source",
        managed_by="test-managed",
        update_existing=False,
    )
    assert first_report["errors"]
    receipt = docs[concept_id]["attributes"][
        seed_bootstrap._SUPPORT_CONCEPT_MATERIALISATION_RECEIPT_ATTRIBUTE
    ]
    assert receipt["status"] == "pending"

    live_text["value"] = "human edit after interruption"
    second_report = seed_bootstrap._materialise_support_concepts(
        [spec],
        source_tag="test-source",
        managed_by="test-managed",
        update_existing=False,
    )

    assert second_report["errors"][0]["reason_code"] == (
        "support_concept_existing_authority_requires_explicit_migration"
    )
    assert live_text["value"] == "human edit after interruption"


def test_support_concept_authority_requires_declared_ontology_typing(
    monkeypatch,
) -> None:
    spec = {
        "concept_id": "#V#has_doi",
        "name": "Has DOI",
        "parent_concept_ids": ["#V#predicate"],
        "create_as_instance": True,
    }
    target_payload = seed_bootstrap._support_concept_target_authority_payload(spec)
    monkeypatch.setattr(
        seed_bootstrap,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: [],
    )

    exact, resumable = seed_bootstrap._support_concept_authority_status(
        concept_doc={
            "concept_id": "#V#has_doi",
            "relationships": {"is_a_type_of": ["#V#unrelated"]},
            "attributes": {},
            "system_tags": [],
        },
        spec=spec,
        target_payload=target_payload,
    )

    assert exact is False
    assert resumable is False


def test_explicit_workflow_target_scopes_support_concepts_transitively() -> None:
    support_specs = [
        {
            "concept_id": "#V#target_support",
            "relationships": {"#V#usesSupport": ["#V#transitive_support"]},
        },
        {"concept_id": "#V#transitive_support"},
        {"concept_id": "#V#unrelated_support"},
    ]

    scoped = seed_bootstrap._scope_support_concepts_to_authority_payloads(
        support_specs,
        {
            "#V#target_workflow": {
                "workflow_text_relations": [
                    {
                        "predicate": "#V#hasPolicy",
                        "text": "Use #V#target_support for this workflow.",
                    }
                ]
            }
        },
    )

    assert [spec["concept_id"] for spec in scoped] == [
        "#V#target_support",
        "#V#transitive_support",
    ]


def test_repo_seed_workflow_definitions_include_gmail_arxiv_discovery_metadata() -> (
    None
):
    definitions = authority_service.build_repo_seed_workflow_definitions(
        target_workflow_ids=["#V#zhan_gmail_arxiv_ingestion_workflow"]
    )

    definition = definitions["#V#zhan_gmail_arxiv_ingestion_workflow"]

    assert "Gmail-to-arXiv" in str(definition.purpose)
    metadata = dict(definition.metadata)
    assert metadata["description_source"].startswith("repo_seed_text_relation:")
    assert metadata["discovery_exemplars_source"].startswith("repo_seed_text_relation:")
    exemplars = metadata["discovery_exemplars"]
    assert "recent email messages about arxiv papers" in exemplars["keywords"]
    assert any("recent email messages" in item for item in exemplars["examples"])
    assert metadata["routing_profile"]["execution_mode"] == "tool_pipeline"
    assert metadata["launch_input_contract"]["schema_version"] == (
        "workflow_launch_input_contract.v1"
    )


def test_repo_seed_workflow_definitions_include_gmail_arxiv_progress_projection_metadata() -> (
    None
):
    definitions = authority_service.build_repo_seed_workflow_definitions(
        target_workflow_ids=[
            "#V#zhan_gmail_arxiv_ingestion_workflow",
            "#V#email_arxiv_ingestion_from_message_workflow",
            "#V#arxiv_resource_ingestion_from_email_reference_workflow",
            "#V#arxiv_paper_representation_workflow",
        ]
    )

    parent = definitions["#V#zhan_gmail_arxiv_ingestion_workflow"]
    parent_projection = parent.metadata["progress_projection"]
    assert parent.metadata["progress_projection_source"].startswith(
        "repo_seed_text_relation:"
    )
    assert {fact["contract_id"] for fact in parent_projection["facts"]} >= {
        "#V#gmail_arxiv_messages_scanned_progress_fact",
        "#V#gmail_arxiv_messages_completed_progress_fact",
        "#V#gmail_arxiv_messages_failed_progress_fact",
    }
    assert {
        fact["contract_id"]
        for fact in parent.states["process_messages"].metadata["progress_projection"][
            "facts"
        ]
    } >= {"#V#gmail_arxiv_batch_result_progress_fact"}

    message = definitions["#V#email_arxiv_ingestion_from_message_workflow"]
    message_normalise_facts = message.states["normalise_message"].metadata[
        "progress_projection"
    ]["facts"]
    assert {fact["contract_id"] for fact in message_normalise_facts} >= {
        "#V#gmail_arxiv_email_message_subject_progress_fact"
    }
    assert {
        fact["source_path"]
        for fact in message_normalise_facts
        if fact["contract_id"] == "#V#gmail_arxiv_email_message_subject_progress_fact"
    } == {"context.message_subject"}
    assert {
        fact["contract_id"]
        for fact in message.states["ingest_arxiv_resources"].metadata[
            "progress_projection"
        ]["facts"]
    } >= {"#V#gmail_arxiv_message_result_progress_fact"}

    resource = definitions["#V#arxiv_resource_ingestion_from_email_reference_workflow"]
    assert {
        fact["contract_id"]
        for fact in resource.states["normalise_reference"].metadata[
            "progress_projection"
        ]["facts"]
    } >= {"#V#gmail_arxiv_paper_id_progress_fact"}
    assert {
        fact["contract_id"]
        for fact in resource.states["represent_arxiv_paper"].metadata[
            "progress_projection"
        ]["facts"]
    } >= {"#V#gmail_arxiv_resource_result_progress_fact"}

    paper = definitions["#V#arxiv_paper_representation_workflow"]
    assert {
        fact["contract_id"]
        for fact in paper.states["fetch_arxiv_metadata"].metadata[
            "progress_projection"
        ]["facts"]
    } >= {"#V#gmail_arxiv_paper_title_progress_fact"}


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
        "publication_specs": {
            "#V#test_workflow": SimpleNamespace(steps=(), initial_state="done")
        },
        "publication_purposes": {},
        "workflow_type_ids": {},
        "workflow_text_relations": {},
        "workflow_launch_input_contracts": {},
        "step_text_relations": {},
        "support_concepts": [
            {"concept_id": "#V#represented_prompt", "name": "Represented Prompt"}
        ],
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
                "workflow_status_by_id": {"#V#test_workflow": {"status": "current"}},
                "bundle_snapshot_drift_detected": False,
                "bundle_snapshot_drift_workflow_ids": [],
                "bundle_snapshot_issue_codes": [],
                "bundle_snapshot_status_by_id": {},
            },
        ),
    )
    target_payload = {"authority": "target"}
    monkeypatch.setattr(
        seed_bootstrap,
        "_workflow_seed_authority_payload_from_bundle_surfaces",
        lambda **_kwargs: target_payload,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_live_workflow_seed_authority_payload",
        lambda **_kwargs: target_payload,
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

    assert (
        result["publication"]["skip_reason"] == "existing_materialisation_valid"
    ), result["support_concepts"]
    assert result["publication"]["counts"]["workflows_published"] == 0
    assert result["publication"]["skipped_due_to_seed_version_not_newer"] == [
        "#V#test_workflow"
    ]
    assert result["repo_seed_version_gate"]["blocked_workflow_ids"] == [
        "#V#test_workflow"
    ]
    assert validation_calls
    assert invalidations == []
    assert result["support_concepts"]["skipped"] is False
    assert "#V#represented_prompt" in (
        result["support_concepts"]["created_concept_ids"]
        + result["support_concepts"]["existing_concept_ids"]
    )
    assert any(
        row.get("text") == "Represented Prompt"
        for row in seed_bootstrap.get_texts_for_concept(
            "#V#represented_prompt",
            predicate="hasName",
            limit=20,
        )
    )

    bundle["support_concepts"] = [
        {"concept_id": "#V#stale_repo_support", "name": "Stale Repo Support"}
    ]
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_workflow_repo_seed_version_marker",
        lambda _workflow_id: {"seed_version": "3"},
    )
    newer_authority_result = seed_bootstrap.bootstrap_repo_seed_workflow_bundle(
        asset_path="seed_bundle.json"
    )

    assert newer_authority_result["support_concepts"]["skipped"] is True
    assert newer_authority_result["support_concepts"]["created_concept_ids"] == []


def test_equal_repo_seed_version_preserves_altered_live_authority(
    monkeypatch,
) -> None:
    bundle = {
        "asset_path": "seed_bundle.json",
        "family_id": "test_family",
        "managed_by": "test",
        "seed_version": "2",
        "source_tag": "test-source",
        "supported_action_ids": (),
        "publication_specs": {
            "#V#test_workflow": SimpleNamespace(steps=(), initial_state="done")
        },
        "publication_purposes": {},
        "workflow_type_ids": {},
        "workflow_text_relations": {},
        "workflow_launch_input_contracts": {},
        "step_text_relations": {},
    }
    publications: list[dict] = []
    invalidations: list[bool] = []

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
        lambda **_kwargs: (
            False,
            {"#V#test_workflow": {"valid": True}},
            {
                "already_current": False,
                "drift_detected": True,
                "current_workflow_ids": ["#V#test_workflow"],
                "drift_workflow_ids": [],
                "issue_codes": [],
                "workflow_status_by_id": {"#V#test_workflow": {"status": "current"}},
                "bundle_snapshot_drift_detected": True,
                "bundle_snapshot_drift_workflow_ids": ["#V#test_workflow"],
                "bundle_snapshot_issue_codes": ["definition_mismatch"],
                "bundle_snapshot_status_by_id": {
                    "#V#test_workflow": {
                        "status": "definition_mismatch",
                        "issue_code": "definition_mismatch",
                    }
                },
            },
        ),
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_workflow_seed_authority_payload_from_bundle_surfaces",
        lambda **_kwargs: {"authority": "target"},
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_live_workflow_seed_authority_payload",
        lambda **_kwargs: {"authority": "human-edit"},
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
        "invalidate_workflow_discovery_executability_caches",
        lambda: invalidations.append(True),
    )

    result = seed_bootstrap.bootstrap_repo_seed_workflow_bundle(
        asset_path="seed_bundle.json"
    )

    assert result["publication"].get("skipped") is True
    assert result["publication"]["skip_reason"] == "workflow_seed_authority_blocked"
    assert result["publication"]["bundle_snapshot_drift_detected"] is True
    assert result["publication"]["bundle_snapshot_drift_workflow_ids"] == [
        "#V#test_workflow"
    ]
    assert result["publication"]["counts"]["workflows_published"] == 0
    assert result["publication"]["counts"]["errors"] == 1
    assert result["repo_seed_version_gate"]["authority_blockers_by_workflow"][
        "#V#test_workflow"
    ]["error_code"] == "live_authority_requires_explicit_migration"
    assert publications == []
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
        "publication_specs": {
            "#V#test_workflow": SimpleNamespace(steps=(), initial_state="done")
        },
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
    legacy_payload = {"authority": "reviewed-v1"}
    target_payload = {"authority": "target-v2"}
    bundle["known_legacy_authority_payload_sha256_by_seed_version"] = {
        "#V#test_workflow": {
            "1": [seed_bootstrap._stable_payload_sha256(legacy_payload)]
        }
    }
    live_state = {"payload": legacy_payload}
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
                "workflow_status_by_id": {"#V#test_workflow": {"status": "current"}},
                "bundle_snapshot_drift_detected": False,
                "bundle_snapshot_drift_workflow_ids": [],
                "bundle_snapshot_issue_codes": [],
                "bundle_snapshot_status_by_id": {},
            },
        ),
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_workflow_seed_authority_payload_from_bundle_surfaces",
        lambda **_kwargs: target_payload,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_live_workflow_seed_authority_payload",
        lambda **_kwargs: live_state["payload"],
    )

    def _publish_with_context_capture(**kwargs):
        publications.append(
            {
                **kwargs,
                "event_workflow_integration": os.environ.get(
                    "VON_EVENT_WORKFLOW_INTEGRATION_ENABLE"
                ),
                "discovery_cache_invalidation": os.environ.get(
                    "VON_WORKFLOW_DISCOVERY_CACHE_INVALIDATION_ENABLE"
                ),
            }
        )
        live_state["payload"] = target_payload
        return {
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
        }

    monkeypatch.setattr(
        authority_service,
        "publish_canonical_chat_workflow_graphs",
        _publish_with_context_capture,
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
        "_upsert_workflow_repo_seed_pending_migration_receipt",
        lambda **_kwargs: {"relation_created": True, "replaced_count": 0},
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
    assert (
        result["publication"]["materialisation_status"] == "repo_seed_version_refresh"
    )
    assert result["publication"]["counts"]["workflows_published"] == 1
    assert result["publication"]["repo_seed_version_refresh_workflow_ids"] == [
        "#V#test_workflow"
    ]
    assert result["repo_seed_version_gate"]["refresh_workflow_ids"] == [
        "#V#test_workflow"
    ]
    assert publications
    assert publications[0]["validate_after_publish"] is False
    assert publications[0]["event_workflow_integration"] == "0"
    assert publications[0]["discovery_cache_invalidation"] == "0"
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
            "authority_payload_sha256": seed_bootstrap._stable_payload_sha256(
                target_payload
            ),
        }
    ]
    assert invalidations == [True]


def test_sibling_migration_does_not_republish_altered_equal_version_workflow(
    monkeypatch,
) -> None:
    workflow_ids = ("#V#protected_workflow", "#V#migrating_workflow")
    target_payloads = {
        workflow_ids[0]: {"authority": "protected-target"},
        workflow_ids[1]: {"authority": "migrating-target"},
    }
    live_payloads = {
        workflow_ids[0]: {"authority": "protected-human-edit"},
        workflow_ids[1]: {"authority": "reviewed-v1"},
    }
    bundle = {
        "asset_path": "seed_bundle.json",
        "family_id": "test_family",
        "managed_by": "test",
        "seed_version": "2",
        "source_tag": "test-source",
        "supported_action_ids": (),
        "publication_specs": {
            workflow_id: SimpleNamespace(steps=(), initial_state="done")
            for workflow_id in workflow_ids
        },
        "publication_purposes": {},
        "workflow_type_ids": {},
        "workflow_text_relations": {},
        "workflow_launch_input_contracts": {},
        "step_text_relations": {},
        "known_legacy_authority_payload_sha256_by_seed_version": {
            workflow_ids[1]: {
                "1": [
                    seed_bootstrap._stable_payload_sha256(
                        live_payloads[workflow_ids[1]]
                    )
                ]
            }
        },
    }
    publications: list[dict] = []
    marker_updates: list[dict] = []
    monkeypatch.setattr(
        authority_service,
        "load_repo_seed_workflow_bundle",
        lambda _asset_path: bundle,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_workflow_repo_seed_version_marker",
        lambda workflow_id: {
            "seed_version": "2" if workflow_id == workflow_ids[0] else "1"
        },
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_validate_existing_materialisation",
        lambda **_kwargs: (
            False,
            {workflow_id: {"valid": True} for workflow_id in workflow_ids},
            {
                "already_current": False,
                "drift_detected": True,
                "current_workflow_ids": list(workflow_ids),
                "drift_workflow_ids": [],
                "issue_codes": [],
                "workflow_status_by_id": {
                    workflow_id: {"status": "current"}
                    for workflow_id in workflow_ids
                },
                "bundle_snapshot_drift_detected": True,
                "bundle_snapshot_drift_workflow_ids": list(workflow_ids),
                "bundle_snapshot_issue_codes": ["definition_mismatch"],
                "bundle_snapshot_status_by_id": {},
            },
        ),
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_workflow_seed_authority_payload_from_bundle_surfaces",
        lambda **kwargs: target_payloads[kwargs["workflow_id"]],
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_live_workflow_seed_authority_payload",
        lambda **kwargs: live_payloads[kwargs["workflow_id"]],
    )

    def _publish(**kwargs):
        publications.append(kwargs)
        assert kwargs["target_workflow_ids"] == (workflow_ids[1],)
        live_payloads[workflow_ids[1]] = target_payloads[workflow_ids[1]]
        return {
            "counts": {"workflows_targeted": 1, "workflows_published": 1, "errors": 0},
            "published_workflow_ids": [workflow_ids[1]],
        }

    monkeypatch.setattr(
        authority_service,
        "publish_canonical_chat_workflow_graphs",
        _publish,
    )
    monkeypatch.setattr(
        authority_service,
        "_build_definition_map_from_publication_specs",
        lambda specs: {workflow_id: object() for workflow_id in specs},
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
        "_upsert_workflow_repo_seed_pending_migration_receipt",
        lambda **_kwargs: {"relation_created": True, "replaced_count": 0},
    )

    result = seed_bootstrap.bootstrap_repo_seed_workflow_bundle(
        asset_path="seed_bundle.json"
    )

    assert publications
    assert result["publication"]["counts"]["errors"] == 1
    assert result["repo_seed_version_gate"]["publication_workflow_ids"] == [
        workflow_ids[1]
    ]
    assert workflow_ids[0] in result["repo_seed_version_gate"][
        "authority_blockers_by_workflow"
    ]
    assert {item["workflow_id"] for item in marker_updates} == {workflow_ids[1]}
    assert live_payloads[workflow_ids[0]] == {"authority": "protected-human-edit"}


def test_exact_legacy_publication_readback_mismatch_fails_closed_and_resumes(
    monkeypatch,
) -> None:
    workflow_id = "#V#test_workflow"
    legacy_payload = {"graph": {"first": "reviewed-v1", "second": "reviewed-v1"}}
    target_payload = {"graph": {"first": "target-v2", "second": "target-v2"}}
    partial_payload = {"graph": {"first": "target-v2", "second": "reviewed-v1"}}
    live_state = {"payload": legacy_payload}
    marker_state: dict[str, dict] = {"value": {"seed_version": "1"}}
    publication_count = {"value": 0}
    bundle = {
        "asset_path": "seed_bundle.json",
        "family_id": "test_family",
        "managed_by": "test",
        "seed_version": "2",
        "source_tag": "test-source",
        "supported_action_ids": (),
        "publication_specs": {
            workflow_id: SimpleNamespace(steps=(), initial_state="done")
        },
        "publication_purposes": {},
        "workflow_type_ids": {},
        "workflow_text_relations": {},
        "workflow_launch_input_contracts": {},
        "step_text_relations": {},
        "known_legacy_authority_payload_sha256_by_seed_version": {
            workflow_id: {
                "1": [seed_bootstrap._stable_payload_sha256(legacy_payload)]
            }
        },
    }
    marker_updates: list[dict] = []
    monkeypatch.setattr(
        authority_service,
        "load_repo_seed_workflow_bundle",
        lambda _asset_path: bundle,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_workflow_repo_seed_version_marker",
        lambda _workflow_id: marker_state["value"],
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_validate_existing_materialisation",
        lambda **_kwargs: (
            False,
            {workflow_id: {"valid": True}},
            {
                "already_current": False,
                "drift_detected": True,
                "current_workflow_ids": [workflow_id],
                "drift_workflow_ids": [],
                "issue_codes": [],
                "workflow_status_by_id": {workflow_id: {"status": "current"}},
                "bundle_snapshot_drift_detected": True,
                "bundle_snapshot_drift_workflow_ids": [workflow_id],
                "bundle_snapshot_issue_codes": ["definition_mismatch"],
                "bundle_snapshot_status_by_id": {},
            },
        ),
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_workflow_seed_authority_payload_from_bundle_surfaces",
        lambda **_kwargs: target_payload,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_live_workflow_seed_authority_payload",
        lambda **_kwargs: live_state["payload"],
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_live_workflow_seed_partial_authority_payload",
        lambda **_kwargs: None,
    )

    def _publish(**_kwargs):
        publication_count["value"] += 1
        live_state["payload"] = (
            partial_payload if publication_count["value"] == 1 else target_payload
        )
        return {
            "counts": {
                "workflows_targeted": 1,
                "workflows_published": 1,
                "errors": 0,
            },
            "published_workflow_ids": [workflow_id],
        }

    monkeypatch.setattr(
        authority_service,
        "publish_canonical_chat_workflow_graphs",
        _publish,
    )
    monkeypatch.setattr(
        authority_service,
        "_build_definition_map_from_publication_specs",
        lambda _specs: {workflow_id: object()},
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
        or marker_state.update(
            {
                "value": {
                    "seed_version": kwargs["seed_version"],
                    "authority_payload_sha256": kwargs[
                        "authority_payload_sha256"
                    ],
                }
            }
        )
        or {"relation_created": True, "replaced_count": 0},
    )

    def _write_pending_receipt(**kwargs):
        marker_state["value"] = {
            "seed_version": kwargs["source_seed_version"] or "unversioned",
            "migration_receipt": {
                "schema_version": (
                    seed_bootstrap._REPO_SEED_MIGRATION_RECEIPT_SCHEMA_VERSION
                ),
                "status": "pending",
                "source_seed_version": (
                    kwargs["source_seed_version"] or "unversioned"
                ),
                "source_authority_payload": kwargs["source_authority_payload"],
                "source_authority_payload_sha256": kwargs[
                    "source_authority_payload_sha256"
                ],
                "target_seed_version": kwargs["target_seed_version"],
                "target_authority_payload_sha256": kwargs[
                    "target_authority_payload_sha256"
                ],
            },
        }
        return {"relation_created": True, "replaced_count": 0}

    monkeypatch.setattr(
        seed_bootstrap,
        "_upsert_workflow_repo_seed_pending_migration_receipt",
        _write_pending_receipt,
    )

    first_result = seed_bootstrap.bootstrap_repo_seed_workflow_bundle(
        asset_path="seed_bundle.json"
    )

    assert first_result["publication"]["counts"]["errors"] == 1
    assert first_result["publication"]["authority_readback_failures_by_workflow"][
        workflow_id
    ]["error_code"] == "workflow_seed_authority_readback_mismatch"
    assert marker_state["value"]["migration_receipt"]["status"] == "pending"
    assert marker_updates == []

    second_result = seed_bootstrap.bootstrap_repo_seed_workflow_bundle(
        asset_path="seed_bundle.json"
    )

    adjudication = second_result["repo_seed_version_gate"][
        "authority_adjudication_by_workflow"
    ][workflow_id]
    assert second_result["publication"]["counts"]["errors"] == 0
    assert adjudication["reason"] == "pending_reviewed_migration_resume"
    assert live_state["payload"] == target_payload
    assert len(marker_updates) == 1
    assert marker_state["value"] == {
        "seed_version": "2",
        "authority_payload_sha256": seed_bootstrap._stable_payload_sha256(
            target_payload
        ),
    }


def test_pending_migration_partial_rejects_non_source_non_target_edit() -> None:
    source_payload = {"graph": {"first": "source", "second": "source"}}
    target_payload = {"graph": {"first": "target", "second": "target"}}

    assert seed_bootstrap._authority_payload_is_source_target_partial(
        source_payload=source_payload,
        target_payload=target_payload,
        current_payload={"graph": {"first": "target", "second": "source"}},
    )
    assert not seed_bootstrap._authority_payload_is_source_target_partial(
        source_payload=source_payload,
        target_payload=target_payload,
        current_payload={"graph": {"first": "human-edit", "second": "source"}},
    )


def test_live_authority_read_failure_blocks_repo_seed_publication(monkeypatch) -> None:
    workflow_id = "#V#unreadable_workflow"
    target_payload = {"authority": "repo-target"}
    bundle = {
        "asset_path": "seed_bundle.json",
        "family_id": "test_family",
        "managed_by": "test",
        "seed_version": "1",
        "source_tag": "test-source",
        "supported_action_ids": (),
        "publication_specs": {
            workflow_id: SimpleNamespace(steps=(), initial_state="done")
        },
        "publication_purposes": {},
        "workflow_type_ids": {},
        "workflow_text_relations": {},
        "workflow_launch_input_contracts": {},
        "step_text_relations": {},
    }
    monkeypatch.setattr(
        authority_service,
        "load_repo_seed_workflow_bundle",
        lambda _asset_path: bundle,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_workflow_repo_seed_version_marker",
        lambda _workflow_id: None,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_validate_existing_materialisation",
        lambda **_kwargs: (
            False,
            {},
            {
                "already_current": False,
                "drift_detected": True,
                "current_workflow_ids": [],
                "drift_workflow_ids": [workflow_id],
                "issue_codes": ["graph_missing"],
                "workflow_status_by_id": {
                    workflow_id: {"status": "graph_missing"}
                },
                "bundle_snapshot_drift_detected": False,
                "bundle_snapshot_drift_workflow_ids": [],
                "bundle_snapshot_issue_codes": [],
                "bundle_snapshot_status_by_id": {},
            },
        ),
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_workflow_seed_authority_payload_from_bundle_surfaces",
        lambda **_kwargs: target_payload,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_live_workflow_seed_authority_payload",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("temporary read failure")),
    )
    monkeypatch.setattr(
        authority_service,
        "publish_canonical_chat_workflow_graphs",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unreadable live authority must not be overwritten")
        ),
    )

    result = seed_bootstrap.bootstrap_repo_seed_workflow_bundle(
        asset_path="seed_bundle.json"
    )

    adjudication = result["repo_seed_version_gate"][
        "authority_adjudication_by_workflow"
    ][workflow_id]
    assert adjudication["reason"] == "workflow_seed_live_authority_read_failed"
    assert adjudication["publication_authorised"] is False
    assert result["publication"]["skip_reason"] == "workflow_seed_authority_blocked"


def test_unreceipted_partial_first_publication_is_preserved(monkeypatch) -> None:
    workflow_id = "#V#partially_authored_workflow"
    target_payload = {"graph": {"first": "target", "second": "target"}}
    partial_payload = {"graph": {"first": "target"}}
    bundle = {
        "asset_path": "seed_bundle.json",
        "family_id": "test_family",
        "managed_by": "test",
        "seed_version": "1",
        "source_tag": "test-source",
        "supported_action_ids": (),
        "publication_specs": {
            workflow_id: SimpleNamespace(steps=(), initial_state="done")
        },
        "publication_purposes": {},
        "workflow_type_ids": {},
        "workflow_text_relations": {},
        "workflow_launch_input_contracts": {},
        "step_text_relations": {},
    }
    monkeypatch.setattr(
        authority_service,
        "load_repo_seed_workflow_bundle",
        lambda _asset_path: bundle,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_workflow_repo_seed_version_marker",
        lambda _workflow_id: None,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_validate_existing_materialisation",
        lambda **_kwargs: (
            False,
            {},
            {
                "already_current": False,
                "drift_detected": True,
                "current_workflow_ids": [],
                "drift_workflow_ids": [workflow_id],
                "issue_codes": ["definition_missing"],
                "workflow_status_by_id": {
                    workflow_id: {"status": "definition_missing"}
                },
                "bundle_snapshot_drift_detected": False,
                "bundle_snapshot_drift_workflow_ids": [],
                "bundle_snapshot_issue_codes": [],
                "bundle_snapshot_status_by_id": {},
            },
        ),
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_workflow_seed_authority_payload_from_bundle_surfaces",
        lambda **_kwargs: target_payload,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_live_workflow_seed_authority_payload",
        lambda **_kwargs: partial_payload,
    )
    monkeypatch.setattr(
        authority_service,
        "publish_canonical_chat_workflow_graphs",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unreceipted partial authority must not be overwritten")
        ),
    )

    result = seed_bootstrap.bootstrap_repo_seed_workflow_bundle(
        asset_path="seed_bundle.json"
    )

    adjudication = result["repo_seed_version_gate"][
        "authority_adjudication_by_workflow"
    ][workflow_id]
    assert adjudication["reason"] == "live_authority_requires_explicit_migration"
    assert adjudication["publication_authorised"] is False
    assert result["publication"]["skip_reason"] == "workflow_seed_authority_blocked"


def test_partial_authority_reader_observes_text_without_complete_graph(
    monkeypatch,
) -> None:
    workflow_id = "#V#incomplete_human_workflow"
    monkeypatch.setattr(
        seed_bootstrap,
        "load_workflow_definition_from_vontology",
        lambda _workflow_id: None,
    )
    monkeypatch.setattr(
        seed_bootstrap.concept_service,
        "get_concept_by_concept_id_exact",
        lambda _workflow_id: {
            "concept_id": workflow_id,
            "relationships": {},
        },
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_live_relation_authority",
        lambda **kwargs: (
            [
                {
                    "predicate": "#V#hasPromptText",
                    "lang": "en-NZ",
                    "text": "human-authored incomplete workflow prompt",
                }
            ]
            if kwargs["concept_id"] == workflow_id
            else []
        ),
    )

    payload = seed_bootstrap._load_live_workflow_seed_partial_authority_payload(
        workflow_id=workflow_id,
        scoped_workflow_type_ids=(),
        scoped_workflow_text_relations=(
            {
                "predicate": "#V#hasPromptText",
                "lang": "en-NZ",
                "text": "repo prompt",
            },
        ),
        scoped_launch_input_contract=None,
        scoped_step_text_relations={},
    )

    assert payload == {
        "workflow_id": workflow_id,
        "workflow_text_relations": [
            {
                "predicate": "#V#hasPromptText",
                "lang": "en-NZ",
                "text": "human-authored incomplete workflow prompt",
            }
        ],
    }


def test_missing_workflow_keeps_safe_first_publication_path(monkeypatch) -> None:
    workflow_id = "#V#new_workflow"
    target_payload = {"authority": "new-target"}
    live_state: dict[str, Any] = {"payload": None}
    bundle = {
        "asset_path": "seed_bundle.json",
        "family_id": "test_family",
        "managed_by": "test",
        "seed_version": "1",
        "source_tag": "test-source",
        "supported_action_ids": (),
        "publication_specs": {
            workflow_id: SimpleNamespace(steps=(), initial_state="done")
        },
        "publication_purposes": {},
        "workflow_type_ids": {},
        "workflow_text_relations": {},
        "workflow_launch_input_contracts": {},
        "step_text_relations": {},
    }
    marker_updates: list[dict] = []
    pending_receipts: list[dict] = []
    monkeypatch.setattr(
        authority_service,
        "load_repo_seed_workflow_bundle",
        lambda _asset_path: bundle,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_workflow_repo_seed_version_marker",
        lambda _workflow_id: None,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_validate_existing_materialisation",
        lambda **_kwargs: (
            False,
            {},
            {
                "already_current": False,
                "drift_detected": True,
                "current_workflow_ids": [],
                "drift_workflow_ids": [workflow_id],
                "issue_codes": ["graph_missing"],
                "workflow_status_by_id": {
                    workflow_id: {"status": "graph_missing"}
                },
                "bundle_snapshot_drift_detected": False,
                "bundle_snapshot_drift_workflow_ids": [],
                "bundle_snapshot_issue_codes": [],
                "bundle_snapshot_status_by_id": {},
            },
        ),
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_workflow_seed_authority_payload_from_bundle_surfaces",
        lambda **_kwargs: target_payload,
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_live_workflow_seed_authority_payload",
        lambda **_kwargs: live_state["payload"],
    )
    monkeypatch.setattr(
        seed_bootstrap,
        "_load_live_workflow_seed_partial_authority_payload",
        lambda **_kwargs: None,
    )

    def _publish(**_kwargs):
        live_state["payload"] = target_payload
        return {
            "counts": {"workflows_targeted": 1, "workflows_published": 1, "errors": 0},
            "published_workflow_ids": [workflow_id],
        }

    monkeypatch.setattr(
        authority_service,
        "publish_canonical_chat_workflow_graphs",
        _publish,
    )
    monkeypatch.setattr(
        authority_service,
        "_build_definition_map_from_publication_specs",
        lambda _specs: {workflow_id: object()},
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
        "_upsert_workflow_repo_seed_pending_migration_receipt",
        lambda **kwargs: pending_receipts.append(kwargs)
        or {"relation_created": True, "replaced_count": 0},
    )

    result = seed_bootstrap.bootstrap_repo_seed_workflow_bundle(
        asset_path="seed_bundle.json"
    )

    assert result["publication"]["counts"]["errors"] == 0
    assert result["repo_seed_version_gate"]["authority_adjudication_by_workflow"][
        workflow_id
    ]["reason"] == "safe_first_publication"
    assert len(pending_receipts) == 1
    assert pending_receipts[0]["source_authority_absent"] is True
    assert pending_receipts[0]["source_authority_payload"] == {}
    assert len(marker_updates) == 1
