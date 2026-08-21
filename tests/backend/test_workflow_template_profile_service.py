"""Tests for workflow-template profile selection and repo-seed affordances."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.backend.workflows import workflow_template_profile_service as service
from src.backend.workflows.workflow_template_profile_service import (
    WORKFLOW_CREATION_COMPANY_TEMPLATE_ID,
    DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH,
    WORKFLOW_CREATION_DEFAULT_TEMPLATE_ID,
    WORKFLOW_CREATION_EVENT_TEMPLATE_ID,
    WORKFLOW_CREATION_PHD_STUDENT_TEMPLATE_ID,
    WORKFLOW_CREATION_PERSON_TEMPLATE_ID,
    WORKFLOW_CREATION_PLACE_TEMPLATE_ID,
    WORKFLOW_CREATION_SCHOLARLY_TEMPLATE_ID,
    WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
    WORKFLOW_TEMPLATE_REPO_SEED_VERSION_FIELD,
    WORKFLOW_TEMPLATE_SPEC_PREDICATE,
    WORKFLOW_TEMPLATE_TYPE_ID,
    clear_workflow_template_bundle_cache,
    ensure_repo_seeded_workflow_template_bundle,
    resolve_workflow_spec_template,
    select_workflow_template,
)
from src.backend.services import concept_service
from src.backend.services.text_value_service import (
    get_texts_for_concept,
    upsert_singleton_text_relation,
)

_ENTITY_TEMPLATE_IDS = (
    WORKFLOW_CREATION_PERSON_TEMPLATE_ID,
    WORKFLOW_CREATION_COMPANY_TEMPLATE_ID,
    WORKFLOW_CREATION_EVENT_TEMPLATE_ID,
    WORKFLOW_CREATION_PLACE_TEMPLATE_ID,
)


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    clear_workflow_template_bundle_cache()

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass

    yield

    clear_workflow_template_bundle_cache()


def _prepare_single_template_migration(
    *,
    tmp_path: Path,
    legacy_seed_version: str | None = None,
    change_target_spec: bool = False,
) -> tuple[str, Path, dict[str, Any]]:
    """Materialise one legacy template and return its reviewed migration seed."""

    current_payload = json.loads(
        DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH.read_text(encoding="utf-8")
    )
    current_payload["templates"] = [
        template
        for template in current_payload["templates"]
        if template["template_id"] == WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]
    legacy_payload = json.loads(json.dumps(current_payload))
    legacy_payload.pop(
        "known_legacy_authority_payload_sha256_by_seed_version",
        None,
    )
    if legacy_seed_version is None:
        legacy_payload.pop("seed_version", None)
        legacy_version_key = "unversioned"
    else:
        legacy_payload["seed_version"] = legacy_seed_version
        legacy_version_key = legacy_seed_version
    legacy_path = tmp_path / f"legacy-template-{legacy_version_key}.json"
    legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    first_report = ensure_repo_seeded_workflow_template_bundle(
        asset_path=legacy_path,
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
    )
    assert first_report["success"] is True

    concept_id = service._template_concept_id(
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    )
    source_payload = service._live_template_authority_payload(
        concept_id=concept_id,
        template_id=WORKFLOW_CREATION_PERSON_TEMPLATE_ID,
    )
    source_sha256 = service._stable_payload_sha256(source_payload)

    migration_payload = json.loads(json.dumps(current_payload))
    if change_target_spec:
        migration_payload["templates"][0]["workflow_spec_template"][
            "migration_test_marker"
        ] = "target"
    migration_payload[
        "known_legacy_authority_payload_sha256_by_seed_version"
    ] = {
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID: {
            legacy_version_key: [source_sha256]
        }
    }
    migration_path = tmp_path / f"migration-template-{legacy_version_key}.json"
    migration_path.write_text(json.dumps(migration_payload), encoding="utf-8")
    clear_workflow_template_bundle_cache()
    return concept_id, migration_path, source_payload


def test_workflow_template_repo_seed_path_is_named_honestly() -> None:
    seed_path = str(DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH).replace("\\", "/")
    assert "repo_seed_bundles" in seed_path
    assert "workflow_template_seed_bundle.json" in seed_path
    assert "authored_sources" not in seed_path


def test_entity_template_seed_pins_origin_main_unversioned_authority_digests() -> None:
    clear_workflow_template_bundle_cache()
    bundle = service._load_repo_seed_workflow_template_bundle_cached(
        str(DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH.resolve())
    )

    assert bundle["known_legacy_authority_payload_sha256_by_seed_version"] == {
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID: {
            "unversioned": [
                "c05bf1c197105f1b827e6425ec65c9969d56e3167a9cab467b8950b0877cb72e"
            ],
            "4": [
                "bebaf90d6603fa63acdd1dcb363f8cb932c61d5ed45270064a7401c1f1cef23b"
            ],
            "5": [
                "33321addf574eeed20ea11bb0aa879d31cbfdf821ff1b12bbc2e46284b3d41a7"
            ],
        },
        WORKFLOW_CREATION_COMPANY_TEMPLATE_ID: {
            "unversioned": [
                "0d9410537439ef85891992a5a243460ea41e464773dd93d3da11343bcce7be90"
            ],
            "4": [
                "1b567d6c833201a3e48953e6c8df416913c118dcc31b4b446f1a2f316658aeef"
            ],
            "5": [
                "39336dd71db1c92a53506d7a9ae8b863109cc39d2aaced06a9fdbaa80d50c7a9"
            ],
        },
        WORKFLOW_CREATION_EVENT_TEMPLATE_ID: {
            "unversioned": [
                "1e959db513067495d7330da9615cfdc96ebb21313098150f471f715a36c118ae"
            ],
            "4": [
                "5c13822c0a2bc0f782dfda7b50966a2173029cf18797af64816479bc43360623"
            ],
            "5": [
                "ee854744c22e256a8757061f5d549c0bff1b6ce1fc96e18111d9cffcd4f91a73"
            ],
        },
        WORKFLOW_CREATION_PLACE_TEMPLATE_ID: {
            "unversioned": [
                "706e021bfd841e3faa7b8214a06492ee8eadc74bea122a71ef31baaee191410c"
            ],
            "4": [
                "7eee095c5dd2d63ed60f5500350fdba48dc3c5fd87c16a1300a7e94ae411140d"
            ],
            "5": [
                "eb5380690cddfbfea3b405c673d36665bbe0017cdcea61542fcdd9dc6828c181"
            ],
        },
    }


def test_select_workflow_template_prefers_scholarly_profile(
    _reset_mock_db: Any,
) -> None:
    selection = select_workflow_template(
        request_text=(
            "Create a workflow from this description request: represent scholarly "
            "works from uploaded PDFs in Vontology."
        )
    )

    assert selection["template_id"] == WORKFLOW_CREATION_SCHOLARLY_TEMPLATE_ID
    assert selection["selection_source"] == "automatic"
    assert selection["profile"]["requires_synthesis_policy"] is True
    assert str(selection["template"]["concept_id"]).startswith("#V#workflow_template_")


def test_select_workflow_template_prefers_phd_student_profile(
    _reset_mock_db: Any,
) -> None:
    selection = select_workflow_template(
        request_text=(
            "Create a workflow from this description request: represent a PhD "
            "student from text description in Vontology."
        )
    )

    assert selection["template_id"] == WORKFLOW_CREATION_PHD_STUDENT_TEMPLATE_ID
    assert selection["selection_source"] == "automatic"
    assert selection["profile"]["requires_synthesis_policy"] is True


@pytest.mark.parametrize(
    ("request_text", "expected_template_id"),
    [
        (
            "Create a workflow from this description request: represent a person from text description in Vontology.",
            WORKFLOW_CREATION_PERSON_TEMPLATE_ID,
        ),
        (
            "Create a workflow from this description request: represent a company from text description in Vontology.",
            WORKFLOW_CREATION_COMPANY_TEMPLATE_ID,
        ),
        (
            "Create a workflow from this description request: represent an event from text description in Vontology.",
            WORKFLOW_CREATION_EVENT_TEMPLATE_ID,
        ),
        (
            "Create a workflow from this description request: represent a place from text description in Vontology.",
            WORKFLOW_CREATION_PLACE_TEMPLATE_ID,
        ),
    ],
)
def test_select_workflow_template_prefers_generic_entity_profiles(
    _reset_mock_db: Any,
    request_text: str,
    expected_template_id: str,
) -> None:
    selection = select_workflow_template(request_text=request_text)

    assert selection["template_id"] == expected_template_id
    assert selection["selection_source"] == "automatic"
    assert selection["profile"]["requires_synthesis_policy"] is True


def test_select_workflow_template_uses_fallback_for_generic_request(
    _reset_mock_db: Any,
) -> None:
    selection = select_workflow_template(
        request_text="Create a workflow from this description request."
    )

    assert selection["template_id"] == WORKFLOW_CREATION_DEFAULT_TEMPLATE_ID
    assert selection["selection_source"] == "fallback"


def test_resolve_workflow_spec_template_renders_person_representation_template(
    _reset_mock_db: Any,
) -> None:
    rendered_spec, diagnostics = resolve_workflow_spec_template(
        request_text=(
            "Create a workflow from this description request: represent a person "
            "from text description in Vontology."
        ),
        explicit_template_id=WORKFLOW_CREATION_PERSON_TEMPLATE_ID,
        variables={
            "workflow_id": "#V#person_representation_workflow",
            "workflow_name": "Person Representation Workflow",
            "workflow_description": "Represent people from conversational text.",
            "request_summary": "represent person from text",
        },
    )

    assert diagnostics["template_id"] == WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    assert rendered_spec["workflow_id"] == "#V#person_representation_workflow"
    steps_by_id = {
        str(step.get("state_id") or ""): step
        for step in rendered_spec["steps"]
        if isinstance(step, dict)
    }
    assert steps_by_id["extract_entity_payload"]["action_id"] == "llm.action"
    assert steps_by_id["resolve_existing_entity"]["action_id"] == (
        "workflow_mcp.invoke_tool"
    )
    assert steps_by_id["resolve_existing_entity"]["inputs"]["tool_name"] == (
        "resolve_concept_by_name"
    )
    assert steps_by_id["materialise_entity"]["action_id"] == (
        "entity_representation.materialise_from_payload"
    )
    assert steps_by_id["materialise_entity"]["inputs"]["requested_facts"] == {
        "$context_key": "requested_facts"
    }
    materialise_transitions = steps_by_id["materialise_entity"][
        "conditional_transitions"
    ]
    assert materialise_transitions[0]["condition_spec"] == {
        "kind": "context_value_equals",
        "key": "entity_resolution_status",
        "value": "ambiguous",
    }
    assert materialise_transitions[0]["to_state"] == "render_existing_ambiguity"
    assert materialise_transitions[1]["condition_spec"] == {"kind": "always"}
    assert materialise_transitions[1]["to_state"] == "read_back_concept"
    assert steps_by_id["read_back_concept"]["inputs"] == {
        "tool_name": "fetch_concept",
        "concept_id": {"$context_key": "entity_representation_concept_id"},
    }
    assert steps_by_id["read_back_has_names"]["inputs"] == {
        "tool_name": "get_text_relations",
        "concept_id": {"$context_key": "entity_representation_concept_id"},
        "predicate": "hasName",
        "limit": 200,
    }
    reuse_assignment = steps_by_id["render_reuse_response"]["inputs"][
        "assignments"
    ][0]
    assert "Verified type/parent relationships" in reuse_assignment["template"]
    assert reuse_assignment["variables"]["relationships"] == {
        "value_from_context": "entity_representation_readback_relationships",
        "transform": "json",
    }
    assert (
        "context:entity_core_representation_verified=True"
        in rendered_spec["required_effects"]
    )
    assert (
        "context:entity_representation_verified=True"
        not in rendered_spec["required_effects"]
    )
    finalise_assignments = {
        assignment["key"]: assignment
        for assignment in steps_by_id["finalise_readback"]["inputs"][
            "assignments"
        ]
    }
    assert finalise_assignments["entity_representation_verified"] == {
        "key": "entity_representation_verified",
        "value_from_context": "entity_representation_requested_facts_complete",
    }
    core_only_step = steps_by_id["render_core_only_response"]
    assert core_only_step["next_state"] == "mark_core_only_follow_up"
    follow_up_step = steps_by_id["mark_core_only_follow_up"]
    follow_up_assignments = {
        assignment["key"]: assignment
        for assignment in follow_up_step["inputs"]["assignments"]
    }
    assert follow_up_assignments["follow_up_required"] == {
        "key": "follow_up_required",
        "value": True,
    }
    assert follow_up_assignments["semantic_outcome"] == {
        "key": "semantic_outcome",
        "value": "follow_up_required",
    }
    assert {"follow_up_required", "semantic_outcome"}.issubset(
        follow_up_step["writes_context_keys"]
    )
    assert follow_up_step["next_state"] == "completed"
    text_relations = rendered_spec.get("text_relations") or []
    assert any(
        isinstance(item, dict)
        and item.get("predicate") == "#V#hasWorkflowRoutingProfileJson"
        for item in text_relations
    )
    assert any(
        isinstance(item, dict)
        and item.get("predicate") == "#V#hasWorkflowDiscoveryExemplarsJson"
        for item in text_relations
    )
    discovery_payload = next(
        json.loads(str(item["text"]))
        for item in text_relations
        if isinstance(item, dict)
        and item.get("predicate") == "#V#hasWorkflowDiscoveryExemplarsJson"
    )
    assert any(
        "smallest adequate read-only capability plan" in str(note)
        for note in discovery_payload.get("routing_notes") or []
    )


@pytest.mark.parametrize(
    ("template_id", "entity_domain"),
    [
        (WORKFLOW_CREATION_COMPANY_TEMPLATE_ID, "company"),
        (WORKFLOW_CREATION_EVENT_TEMPLATE_ID, "event"),
        (WORKFLOW_CREATION_PLACE_TEMPLATE_ID, "place"),
    ],
)
def test_nonperson_templates_preserve_requested_fact_coverage_until_complete(
    _reset_mock_db: Any,
    template_id: str,
    entity_domain: str,
) -> None:
    rendered_spec, diagnostics = resolve_workflow_spec_template(
        request_text=f"Represent this {entity_domain} and its grounded facts.",
        explicit_template_id=template_id,
        variables={
            "workflow_id": f"#V#{entity_domain}_representation_workflow",
            "workflow_name": f"{entity_domain.title()} Representation Workflow",
            "workflow_description": (
                f"Represent {entity_domain} entities and their grounded facts."
            ),
            "request_summary": f"represent {entity_domain} and facts",
        },
    )

    assert diagnostics["template_id"] == template_id
    assert (
        "context:entity_core_representation_verified=True"
        in rendered_spec["required_effects"]
    )
    assert (
        "context:entity_representation_verified=True"
        not in rendered_spec["required_effects"]
    )
    assert rendered_spec["postcondition_probe"][
        "entity_core_representation_verified"
    ] is True

    steps_by_id = {
        str(step.get("state_id") or ""): step
        for step in rendered_spec["steps"]
        if isinstance(step, dict)
    }
    extract_step = steps_by_id["extract_entity_payload"]
    assert "requested_facts" in extract_step["llm_policy"][
        "response_contract_text"
    ]
    assert {
        "tool_output_field": "validated_json.requested_facts",
        "context_key": "requested_facts",
    } in extract_step["tool_output_context_mappings"]
    assert "requested_facts" in extract_step["writes_context_keys"]

    resolve_transitions = steps_by_id["resolve_existing_entity"][
        "conditional_transitions"
    ]
    resolved_transition = next(
        transition
        for transition in resolve_transitions
        if transition["condition_spec"].get("value") == "resolved"
    )
    assert resolved_transition["to_state"] == "materialise_entity"

    materialise_step = steps_by_id["materialise_entity"]
    assert materialise_step["inputs"]["requested_facts"] == {
        "$context_key": "requested_facts"
    }
    materialise_outputs = {
        mapping["context_key"]
        for mapping in materialise_step["tool_output_context_mappings"]
    }
    expected_coverage_outputs = {
        "entity_core_representation_verified",
        "entity_representation_requested_facts_complete",
        "entity_representation_unresolved_requested_facts",
        "entity_representation_requested_fact_count",
        "entity_representation_requested_facts_payload_valid",
        "entity_representation_coverage",
        "entity_resolution_status",
        "entity_resolution_candidates",
    }
    assert expected_coverage_outputs.issubset(materialise_outputs)
    assert expected_coverage_outputs.issubset(
        set(materialise_step["writes_context_keys"])
    )
    materialise_transitions = materialise_step["conditional_transitions"]
    assert materialise_transitions[0]["condition_spec"] == {
        "kind": "context_value_equals",
        "key": "entity_resolution_status",
        "value": "ambiguous",
    }
    assert materialise_transitions[0]["to_state"] == "render_existing_ambiguity"
    assert materialise_transitions[1]["condition_spec"] == {"kind": "always"}
    assert materialise_transitions[1]["to_state"] == "read_back_concept"
    assert steps_by_id["read_back_concept"]["inputs"] == {
        "tool_name": "fetch_concept",
        "concept_id": {"$context_key": "entity_representation_concept_id"},
    }

    finalise_assignments = {
        assignment["key"]: assignment
        for assignment in steps_by_id["finalise_readback"]["inputs"][
            "assignments"
        ]
    }
    assert finalise_assignments["entity_representation_verified"] == {
        "key": "entity_representation_verified",
        "value_from_context": "entity_representation_requested_facts_complete",
    }
    assert steps_by_id["finalise_readback"]["conditional_transitions"][0][
        "to_state"
    ] == "render_core_only_response"
    core_only_step = steps_by_id["render_core_only_response"]
    assert "richer requested representation is not complete" in core_only_step[
        "inputs"
    ]["assignments"][0]["template"]
    assert core_only_step["next_state"] == "mark_core_only_follow_up"
    follow_up_assignments = {
        assignment["key"]: assignment
        for assignment in steps_by_id["mark_core_only_follow_up"]["inputs"][
            "assignments"
        ]
    }
    assert follow_up_assignments["follow_up_required"]["value"] is True
    assert follow_up_assignments["semantic_outcome"]["value"] == (
        "follow_up_required"
    )


def test_select_workflow_template_materialises_first_class_template_concepts(
    _reset_mock_db: Any,
) -> None:
    selection = select_workflow_template(
        request_text="Create a workflow from this description request."
    )

    concept_id = str(selection["template"]["concept_id"] or "").strip()
    assert concept_id

    concept_doc = concept_service.get_concept_by_concept_id(concept_id)
    assert concept_doc is not None
    instance_of = (concept_doc.get("relationships") or {}).get(
        "is_an_instance_of"
    ) or []
    assert WORKFLOW_TEMPLATE_TYPE_ID in instance_of

    profile_rows = get_texts_for_concept(
        concept_id,
        predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
        limit=5,
    )
    assert profile_rows

    spec_rows = get_texts_for_concept(
        concept_id,
        predicate=WORKFLOW_TEMPLATE_SPEC_PREDICATE,
        limit=5,
    )
    assert spec_rows


def test_entity_templates_migrate_only_exact_known_unversioned_authority(
    _reset_mock_db: Any,
    tmp_path: Path,
) -> None:
    current_payload = json.loads(
        DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH.read_text(encoding="utf-8")
    )
    current_payload["templates"] = [
        template
        for template in current_payload["templates"]
        if template["template_id"] in _ENTITY_TEMPLATE_IDS
    ]

    legacy_payload = json.loads(json.dumps(current_payload))
    legacy_payload.pop("seed_version", None)
    legacy_payload.pop(
        "known_legacy_authority_payload_sha256_by_seed_version",
        None,
    )
    legacy_path = tmp_path / "legacy_templates.json"
    legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    first_report = ensure_repo_seeded_workflow_template_bundle(
        asset_path=legacy_path,
        template_ids=_ENTITY_TEMPLATE_IDS,
    )
    assert first_report["success"] is True

    known_digests: dict[str, list[str]] = {}
    for template_id in _ENTITY_TEMPLATE_IDS:
        concept_id = service._template_concept_id(template_id)
        live_payload = service._live_template_authority_payload(
            concept_id=concept_id,
            template_id=template_id,
        )
        known_digests[template_id] = [
            service._stable_payload_sha256(live_payload)
        ]

    migration_payload = json.loads(json.dumps(current_payload))
    migration_payload[
        "known_legacy_authority_payload_sha256_by_seed_version"
    ] = {
        template_id: {"unversioned": digests}
        for template_id, digests in known_digests.items()
    }
    migration_path = tmp_path / "migration_templates.json"
    migration_path.write_text(json.dumps(migration_payload), encoding="utf-8")
    clear_workflow_template_bundle_cache()

    migration_report = ensure_repo_seeded_workflow_template_bundle(
        asset_path=migration_path,
        template_ids=_ENTITY_TEMPLATE_IDS,
        migrate_older_seed_versions=True,
    )

    assert migration_report["success"] is True
    assert migration_report["migrated_template_ids"] == list(_ENTITY_TEMPLATE_IDS)
    assert migration_report["migrated_known_legacy_template_ids"] == list(
        _ENTITY_TEMPLATE_IDS
    )
    assert all(
        readback["verified"] is True
        for readback in migration_report["migration_readback_by_template_id"].values()
    )
    for template_id in _ENTITY_TEMPLATE_IDS:
        concept_id = service._template_concept_id(template_id)
        profile_rows = get_texts_for_concept(
            concept_id,
            predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
            limit=5,
        )
        profile = json.loads(str(profile_rows[0]["text"]))
        assert profile[WORKFLOW_TEMPLATE_REPO_SEED_VERSION_FIELD] == "6"


def test_template_migration_accepts_an_exact_registered_numeric_legacy_payload(
    _reset_mock_db: Any,
    tmp_path: Path,
) -> None:
    concept_id, migration_path, _source_payload = (
        _prepare_single_template_migration(
            tmp_path=tmp_path,
            legacy_seed_version="1",
        )
    )

    migration = ensure_repo_seeded_workflow_template_bundle(
        asset_path=migration_path,
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )

    assert migration["success"] is True
    assert migration["migrated_template_ids"] == [
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]
    assert migration["migrated_known_legacy_template_ids"] == [
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]
    assert migration["migration_readback_by_template_id"][
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]["verified"] is True
    profile_row = get_texts_for_concept(
        concept_id,
        predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
        limit=5,
    )[0]
    profile = json.loads(str(profile_row["text"]))
    assert profile[WORKFLOW_TEMPLATE_REPO_SEED_VERSION_FIELD] == "6"
    receipt = dict(profile_row.get("context") or {}).get(
        service._SEED_MIGRATION_RECEIPT_CONTEXT_KEY
    )
    assert isinstance(receipt, dict)
    assert receipt["status"] == "verified"
    assert receipt["source_seed_version_key"] == "1"


def test_numeric_older_template_version_does_not_prove_repo_ownership(
    _reset_mock_db: Any,
) -> None:
    first_report = ensure_repo_seeded_workflow_template_bundle(
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )
    assert first_report["success"] is True

    concept_id = service._template_concept_id(
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    )
    profile_row = get_texts_for_concept(
        concept_id,
        predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
        limit=5,
    )[0]
    profile = json.loads(str(profile_row["text"]))
    profile[WORKFLOW_TEMPLATE_REPO_SEED_VERSION_FIELD] = "1"
    upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
        text=json.dumps(profile, sort_keys=True),
        lang="en-NZ",
        garbage_collect=True,
    )
    upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate=WORKFLOW_TEMPLATE_SPEC_PREDICATE,
        text=json.dumps({"sentinel": "human-authored-numeric-old"}),
        lang="en-NZ",
        garbage_collect=True,
    )
    clear_workflow_template_bundle_cache()

    migration = ensure_repo_seeded_workflow_template_bundle(
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )

    assert migration["success"] is False
    assert migration["migrated_template_ids"] == []
    assert migration["skipped_current_template_ids"] == [
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]
    blocker = migration["migration_blockers_by_template_id"][
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]
    assert blocker["error_code"] == "legacy_authority_requires_explicit_migration"
    assert blocker["observed_seed_version_key"] == "1"
    assert blocker["known_legacy_authority_payload_sha256"] == []
    current_spec = json.loads(
        str(
            get_texts_for_concept(
                concept_id,
                predicate=WORKFLOW_TEMPLATE_SPEC_PREDICATE,
                limit=5,
            )[0]["text"]
        )
    )
    assert current_spec == {"sentinel": "human-authored-numeric-old"}


def test_equal_template_seed_version_with_altered_authority_is_blocked(
    _reset_mock_db: Any,
) -> None:
    first_report = ensure_repo_seeded_workflow_template_bundle(
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )
    assert first_report["success"] is True
    concept_id = service._template_concept_id(
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    )
    upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate=WORKFLOW_TEMPLATE_SPEC_PREDICATE,
        text=json.dumps({"sentinel": "human-authored-current-version"}),
        lang="en-NZ",
        garbage_collect=True,
    )
    clear_workflow_template_bundle_cache()

    report = ensure_repo_seeded_workflow_template_bundle(
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )

    assert report["success"] is False
    blocker = report["seed_version_authority_mismatches_by_template_id"][
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]
    assert blocker["error_code"] == "legacy_authority_requires_explicit_migration"
    current_spec_rows = get_texts_for_concept(
        concept_id,
        predicate=WORKFLOW_TEMPLATE_SPEC_PREDICATE,
        limit=5,
    )
    assert json.loads(str(current_spec_rows[0]["text"])) == {
        "sentinel": "human-authored-current-version"
    }


def test_versioned_template_migration_preserves_altered_unversioned_authority_with_blocker(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_report = ensure_repo_seeded_workflow_template_bundle(
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )
    assert first_report["success"] is True

    concept_id = "#V#workflow_template_workflow_creation_person_representation"
    profile_rows = get_texts_for_concept(
        concept_id,
        predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
        limit=5,
    )
    profile = json.loads(str(profile_rows[0]["text"]))
    profile.pop(WORKFLOW_TEMPLATE_REPO_SEED_VERSION_FIELD, None)
    upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
        text=json.dumps(profile, sort_keys=True),
        lang="en-NZ",
        context={"source": "human_vontology_author"},
        garbage_collect=True,
    )
    upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate=WORKFLOW_TEMPLATE_SPEC_PREDICATE,
        text=json.dumps({"sentinel": "unversioned-human-authority"}),
        lang="en-NZ",
        context={"source": "human_vontology_author"},
        garbage_collect=True,
    )
    clear_workflow_template_bundle_cache()
    support_mutations: list[str] = []
    relation_upserts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        service,
        "_ensure_template_type_surface",
        lambda: support_mutations.append("type_surface"),
    )
    monkeypatch.setattr(
        service,
        "_ensure_template_instance_typing",
        lambda _concept_id: support_mutations.append("instance_typing"),
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: relation_upserts.append(dict(kwargs)),
    )

    report = ensure_repo_seeded_workflow_template_bundle(
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )

    assert report["success"] is False
    assert report["migrated_template_ids"] == []
    assert report["skipped_current_template_ids"] == [
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]
    blocker = report["unversioned_migration_blockers_by_template_id"][
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]
    assert blocker["error_code"] == (
        "unversioned_authority_requires_explicit_migration"
    )
    assert report["errors_by_template_id"][WORKFLOW_CREATION_PERSON_TEMPLATE_ID] == (
        blocker["error_code"]
    )
    spec_rows = get_texts_for_concept(
        concept_id,
        predicate=WORKFLOW_TEMPLATE_SPEC_PREDICATE,
        limit=5,
    )
    assert json.loads(str(spec_rows[0]["text"])) == {
        "sentinel": "unversioned-human-authority"
    }
    assert support_mutations == []
    assert relation_upserts == []


def test_template_id_relation_is_part_of_legacy_authority_adjudication(
    _reset_mock_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    concept_id, migration_path, _source_payload = (
        _prepare_single_template_migration(tmp_path=tmp_path)
    )
    upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate=service.WORKFLOW_TEMPLATE_ID_PREDICATE,
        text="workflow_creation.human_custom_person",
        lang="en-NZ",
        context={"source": "human_vontology_author"},
        garbage_collect=True,
    )
    clear_workflow_template_bundle_cache()
    relation_upserts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: relation_upserts.append(dict(kwargs)),
    )

    report = ensure_repo_seeded_workflow_template_bundle(
        asset_path=migration_path,
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )

    assert report["success"] is False
    blocker = report["unversioned_migration_blockers_by_template_id"][
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]
    assert blocker["error_code"] == (
        "unversioned_authority_requires_explicit_migration"
    )
    assert service._text_value_for_predicates(
        concept_id,
        service._TEMPLATE_ID_PREDICATES,
    ) == "workflow_creation.human_custom_person"
    assert relation_upserts == []


def test_interrupted_template_migration_resumes_only_from_recomputed_safe_state(
    _reset_mock_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    concept_id, migration_path, _source_payload = (
        _prepare_single_template_migration(
            tmp_path=tmp_path,
            change_target_spec=True,
        )
    )
    real_upsert = service.upsert_singleton_text_relation
    failure = {"armed": True}

    def _interrupt_after_spec_write(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("predicate") == "hasDescription" and failure["armed"]:
            failure["armed"] = False
            raise RuntimeError("simulated_interrupted_migration")
        return real_upsert(**kwargs)

    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _interrupt_after_spec_write,
    )
    first_report = ensure_repo_seeded_workflow_template_bundle(
        asset_path=migration_path,
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )

    assert first_report["success"] is False
    assert "simulated_interrupted_migration" in first_report[
        "errors_by_template_id"
    ][WORKFLOW_CREATION_PERSON_TEMPLATE_ID]
    partial_payload = service._live_template_authority_payload(
        concept_id=concept_id,
        template_id=WORKFLOW_CREATION_PERSON_TEMPLATE_ID,
    )
    assert partial_payload["workflow_spec_template"]["migration_test_marker"] == (
        "target"
    )
    assert WORKFLOW_TEMPLATE_REPO_SEED_VERSION_FIELD not in partial_payload["profile"]
    pending_profile_row = get_texts_for_concept(
        concept_id,
        predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
        limit=5,
    )[0]
    pending_receipt = dict(pending_profile_row.get("context") or {})[
        service._SEED_MIGRATION_RECEIPT_CONTEXT_KEY
    ]
    assert pending_receipt["status"] == "pending"

    monkeypatch.setattr(service, "upsert_singleton_text_relation", real_upsert)
    clear_workflow_template_bundle_cache()
    retry_report = ensure_repo_seeded_workflow_template_bundle(
        asset_path=migration_path,
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )

    assert retry_report["success"] is True
    assert retry_report["migrated_template_ids"] == [
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]
    assert retry_report["migration_readback_by_template_id"][
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]["verified"] is True
    verified_profile_row = get_texts_for_concept(
        concept_id,
        predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
        limit=5,
    )[0]
    verified_receipt = dict(verified_profile_row.get("context") or {})[
        service._SEED_MIGRATION_RECEIPT_CONTEXT_KEY
    ]
    assert verified_receipt["status"] == "verified"


def test_interrupted_initial_template_materialisation_resumes_from_creation_receipt(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_upsert = service.upsert_singleton_text_relation
    failure = {"armed": True}

    def _interrupt_initial_materialisation(**kwargs: Any) -> dict[str, Any]:
        if (
            kwargs.get("predicate") == WORKFLOW_TEMPLATE_SPEC_PREDICATE
            and failure["armed"]
        ):
            failure["armed"] = False
            raise RuntimeError("simulated_initial_materialisation_interruption")
        return real_upsert(**kwargs)

    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _interrupt_initial_materialisation,
    )
    first_report = ensure_repo_seeded_workflow_template_bundle(
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )

    assert first_report["success"] is False
    assert "simulated_initial_materialisation_interruption" in first_report[
        "errors_by_template_id"
    ][WORKFLOW_CREATION_PERSON_TEMPLATE_ID]
    concept_id = service._template_concept_id(
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    )
    partial_concept = service._safe_get_concept(concept_id)
    assert partial_concept is not None
    initial_receipt = dict(partial_concept.get("attributes") or {})[
        service._SEED_INITIAL_MATERIALISATION_RECEIPT_ATTRIBUTE
    ]
    assert initial_receipt["status"] == "pending"
    assert initial_receipt["template_id"] == WORKFLOW_CREATION_PERSON_TEMPLATE_ID

    monkeypatch.setattr(service, "upsert_singleton_text_relation", real_upsert)
    clear_workflow_template_bundle_cache()
    retry_report = ensure_repo_seeded_workflow_template_bundle(
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )

    assert retry_report["success"] is True
    assert retry_report["migrated_template_ids"] == []
    assert retry_report["resumed_initial_materialisation_template_ids"] == [
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]
    assert retry_report["migration_readback_by_template_id"][
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]["verified"] is True
    live_payload = service._live_template_authority_payload(
        concept_id=concept_id,
        template_id=WORKFLOW_CREATION_PERSON_TEMPLATE_ID,
    )
    assert service._stable_payload_sha256(live_payload) == initial_receipt[
        "target_authority_payload_sha256"
    ]


def test_initial_template_receipt_does_not_authorise_post_interruption_human_edit(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_upsert = service.upsert_singleton_text_relation
    failure = {"armed": True}

    def _interrupt_initial_materialisation(**kwargs: Any) -> dict[str, Any]:
        if (
            kwargs.get("predicate") == WORKFLOW_TEMPLATE_SPEC_PREDICATE
            and failure["armed"]
        ):
            failure["armed"] = False
            raise RuntimeError("simulated_initial_materialisation_interruption")
        return real_upsert(**kwargs)

    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _interrupt_initial_materialisation,
    )
    first_report = ensure_repo_seeded_workflow_template_bundle(
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )
    assert first_report["success"] is False

    concept_id = service._template_concept_id(
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    )
    real_upsert(
        subject_concept_id=concept_id,
        predicate=WORKFLOW_TEMPLATE_SPEC_PREDICATE,
        text=json.dumps({"sentinel": "human-edit-after-initial-interruption"}),
        lang="en-NZ",
        context={"source": "human_vontology_author"},
        garbage_collect=True,
    )
    monkeypatch.setattr(service, "upsert_singleton_text_relation", real_upsert)
    clear_workflow_template_bundle_cache()

    retry_report = ensure_repo_seeded_workflow_template_bundle(
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )

    assert retry_report["success"] is False
    assert retry_report["resumed_initial_materialisation_template_ids"] == []
    assert "workflow_template_profile_missing_or_invalid" in retry_report[
        "errors_by_template_id"
    ][WORKFLOW_CREATION_PERSON_TEMPLATE_ID]
    spec_rows = get_texts_for_concept(
        concept_id,
        predicate=WORKFLOW_TEMPLATE_SPEC_PREDICATE,
        limit=5,
    )
    assert json.loads(str(spec_rows[0]["text"])) == {
        "sentinel": "human-edit-after-initial-interruption"
    }


def test_pending_template_receipt_cannot_whitelist_a_human_edit(
    _reset_mock_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    concept_id, migration_path, _source_payload = (
        _prepare_single_template_migration(
            tmp_path=tmp_path,
            change_target_spec=True,
        )
    )
    real_upsert = service.upsert_singleton_text_relation
    failure = {"armed": True}

    def _interrupt_after_spec_write(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("predicate") == "hasDescription" and failure["armed"]:
            failure["armed"] = False
            raise RuntimeError("simulated_interrupted_migration")
        return real_upsert(**kwargs)

    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _interrupt_after_spec_write,
    )
    first_report = ensure_repo_seeded_workflow_template_bundle(
        asset_path=migration_path,
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )
    assert first_report["success"] is False

    real_upsert(
        subject_concept_id=concept_id,
        predicate=WORKFLOW_TEMPLATE_SPEC_PREDICATE,
        text=json.dumps({"sentinel": "human-edit-after-interruption"}),
        lang="en-NZ",
        garbage_collect=True,
    )
    human_payload = service._live_template_authority_payload(
        concept_id=concept_id,
        template_id=WORKFLOW_CREATION_PERSON_TEMPLATE_ID,
    )
    human_sha256 = service._stable_payload_sha256(human_payload)
    profile_row = get_texts_for_concept(
        concept_id,
        predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
        limit=5,
    )[0]
    profile_context = dict(profile_row.get("context") or {})
    tampered_receipt = dict(
        profile_context[service._SEED_MIGRATION_RECEIPT_CONTEXT_KEY]
    )
    tampered_receipt["safe_partial_authority_payload_sha256"] = [
        *tampered_receipt["safe_partial_authority_payload_sha256"],
        human_sha256,
    ]
    profile_context[service._SEED_MIGRATION_RECEIPT_CONTEXT_KEY] = tampered_receipt
    real_upsert(
        subject_concept_id=concept_id,
        predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
        text=str(profile_row["text"]),
        lang="en-NZ",
        context=profile_context,
        garbage_collect=True,
    )

    attempted_writes: list[dict[str, Any]] = []
    support_mutations: list[str] = []
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: attempted_writes.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        service,
        "_ensure_template_type_surface",
        lambda: support_mutations.append("type_surface"),
    )
    monkeypatch.setattr(
        service,
        "_ensure_template_instance_typing",
        lambda _concept_id: support_mutations.append("instance_typing"),
    )
    clear_workflow_template_bundle_cache()
    retry_report = ensure_repo_seeded_workflow_template_bundle(
        asset_path=migration_path,
        template_ids=[WORKFLOW_CREATION_PERSON_TEMPLATE_ID],
        migrate_older_seed_versions=True,
    )

    assert retry_report["success"] is False
    blocker = retry_report["unversioned_migration_blockers_by_template_id"][
        WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    ]
    assert blocker["pending_migration_receipt_present"] is True
    assert blocker["pending_migration_receipt_valid"] is False
    assert attempted_writes == []
    assert support_mutations == []
    persisted_spec = json.loads(
        str(
            get_texts_for_concept(
                concept_id,
                predicate=WORKFLOW_TEMPLATE_SPEC_PREDICATE,
                limit=5,
            )[0]["text"]
        )
    )
    assert persisted_spec == {"sentinel": "human-edit-after-interruption"}
