from __future__ import annotations

import json

import pytest

from src.backend.integrations.internal_mcp import (
    InternalMCPGateway,
    InternalMCPTransport,
    build_default_catalogue,
)
from src.backend.services.publication_scope_profile_vontology_service import (
    bootstrap_canonical_publication_scope_profiles,
)
from src.backend.services.text_value_service import upsert_singleton_text_relation


@pytest.fixture
def publication_profile_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> InternalMCPGateway:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    from src.backend.db.mongo_client import get_db

    database = get_db()
    assert database is not None
    for collection_name in ("concepts", "text_relations", "text_values"):
        database.drop_collection(collection_name)
    report = bootstrap_canonical_publication_scope_profiles()
    assert report["success"] is True, report
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def test_profile_resolver_is_an_ordinary_non_mutating_read() -> None:
    definition = build_default_catalogue().get("resolve_publication_scope_profile")

    assert definition.category == "read"
    assert definition.hard_timeout_enabled is False
    assert definition.ordinary_turn_excluded_reason is None
    assert definition.ordinary_turn_effect is False


def test_gateway_returns_live_profile_advice_and_required_authority(
    publication_profile_gateway: InternalMCPGateway,
) -> None:
    result = publication_profile_gateway.invoke(
        "resolve_publication_scope_profile",
        {
            "plane": "instance",
            "type_concept_ids": ["#V#scholarly_article"],
            "source_kind": "public_scholarly_metadata",
            "producer": "#V#scholarly_article_metadata_representation_workflow",
            # Payload identity must not be promoted into access or authority.
            "user_concept_id": "#V#payload_claimed_user",
            "namespace": "#V#payload_claimed_user@payload_claimed_org",
        },
    )

    payload = result.payload
    assert payload["status"] == "resolved"
    assert payload["selected_scope_mode"] == "global_general"
    assert payload["mutation_performed"] is False
    assert payload["required_authority"] == {
        "status": "deferred_to_effect_boundary",
        "authority_kind": "global_ontology_administrator",
        "enforced_by": "create_concepts",
        "profile_is_authority_grant": False,
    }
    assert payload["decision_evidence"]["profile_concept_ids"] == [
        "#V#publication_profile_scientific_paper_global"
    ]
    assert payload["decision_evidence"]["profile_lineage"]["#V#scholarly_article"][
        0
    ] == {"concept_id": "#V#scholarly_article", "depth": 0}
    assert (
        payload["decision_evidence"]["actor_authority_decision"]
        == payload["required_authority"]
    )
    assert payload["decision_evidence"]["canonical_read_back"] == {
        "required": True,
        "status": "required_after_governed_effect",
    }


def test_gateway_returns_actionable_protected_carrier_refusal(
    publication_profile_gateway: InternalMCPGateway,
) -> None:
    result = publication_profile_gateway.invoke(
        "resolve_publication_scope_profile",
        {
            "plane": "assertion",
            "predicate_concept_id": "#V#has_secret_value",
            "selected_scope_mode": "user_only_default",
            "selection_reason": "Attempt an ordinary private fallback.",
        },
    )

    payload = result.payload
    assert payload["success"] is False
    assert payload["status"] == "unsupported_carrier"
    assert payload["selected_scope_mode"] == "external_secure_storage"
    assert payload["carrier"] == {
        "supported": False,
        "carrier": "external_secure_storage",
        "error_code": "ordinary_vontology_storage_prohibited",
    }
    assert payload["required_authority"] == {
        "status": "carrier_required",
        "profile_is_authority_grant": False,
    }
    assert payload["mutation_performed"] is False


def test_live_predicate_edit_changes_fresh_durable_workflow_decision(
    publication_profile_gateway: InternalMCPGateway,
) -> None:
    from src.backend.workflows.action_registry import WorkflowEnvironment
    from src.backend.workflows.durable.registry_factory import (
        build_durable_action_registry,
    )

    registry = build_durable_action_registry()
    environment = WorkflowEnvironment(
        llm_client=None,
        gateway=publication_profile_gateway,
    )
    inputs = {
        "plane": "assertion",
        "predicate_concept_id": "#V#has_read",
        "producer": "#V#test_publication_scope_workflow",
    }
    before = registry.execute(
        "resolve_publication_scope_profile",
        inputs=inputs,
        context={},
        env=environment,
    )
    assert before.status == "success"
    assert before.outputs["result"]["selected_scope_mode"] == "user_only_default"

    profile_id = "#V#publication_profile_private_context_assertion_user"
    edited_payload = {
        "schema_version": "publication_scope_profile.v1",
        "profile_id": "private_context_assertion_organisation_exception",
        "version": "2",
        "status": "active",
        "plane": "assertion",
        "recommended_scope_mode": "organisation_general",
        "source": "live_vontology_edit",
    }
    upsert_singleton_text_relation(
        subject_concept_id=profile_id,
        predicate="#V#has_publication_scope_profile_json",
        text=json.dumps(edited_payload, sort_keys=True),
        lang="en-NZ",
        context={"source": "test_live_predicate_edit"},
        garbage_collect=True,
    )

    after = registry.execute(
        "resolve_publication_scope_profile",
        inputs=inputs,
        context={},
        env=environment,
    )
    assert after.status == "success"
    assert after.outputs["result"]["selected_scope_mode"] == "organisation_general"
    assert after.outputs["result"]["decision_evidence"]["profile_versions"] == {
        profile_id: "2"
    }
