import json

from src.backend.services import constitutive_relation_requirement_service as service
from src.backend.services.constitutive_relation_requirement_service import (
    CONSTITUTIVE_REQUIREMENT_STARTUP_FAMILY_ID,
    CONSTITUTIVE_REQUIREMENTS_PREDICATE,
    CONSTITUTIVE_REQUIREMENTS_SCHEMA_VERSION,
    MEMBER_OF_PREDICATE_ID,
    VON_USER_ORGANISATION_TYPE_ID,
    VON_USER_TYPE_ID,
    ConstitutiveRelationRequirementService,
    canonical_constitutive_requirement_profile_blueprints,
    ensure_canonical_constitutive_relation_requirement_profiles,
    ensure_constitutive_relation_requirement_profiles_current_for_startup,
    reconcile_canonical_constitutive_relation_requirement_profiles,
)


def _empty_text_reader(concept_ids, **_kwargs):
    return {concept_id: [] for concept_id in concept_ids}


def test_builtin_member_requirement_is_incoming_authority_relevant_and_not_visibility():
    queries = []

    def _find(query, _projection):
        queries.append(query)
        return []

    service = ConstitutiveRelationRequirementService(
        text_reader=_empty_text_reader,
        concept_finder=_find,
        descendant_resolver=lambda _type_id: [VON_USER_TYPE_ID],
    )
    missing, diagnostics = service.get_missing_requirements(
        instance={
            "concept_id": "#V#primary_labs",
            "name": "Primary Labs",
            "relationships": {
                "is_an_instance_of": [VON_USER_ORGANISATION_TYPE_ID],
                "#V#specific_to_user": ["#V#alice"],
            },
        },
        type_depth_by_id={VON_USER_ORGANISATION_TYPE_ID: 0},
    )

    assert len(missing) == 1
    requirement = missing[0]
    assert requirement["predicate_concept_id"] == MEMBER_OF_PREDICATE_ID
    assert requirement["focal_argument"] == "object"
    assert requirement["other_argument_type_concept_id"] == VON_USER_TYPE_ID
    assert requirement["current_cardinality"] == 0
    assert requirement["declaration_source"] == "builtin_compatibility"
    assert requirement["activation_relevance"] == "identity_namespace_activation"
    assert requirement["authority_relevance"] == "organisation_membership_authority"
    assert requirement["auto_formalisation_allowed"] is True
    assert requirement["auto_formalisation_target_status"] == "tentative"
    assert requirement["formalisation_mode"] == "tentative_candidate"
    assert requirement["activation_minimum_status"] == "asserted"
    assert requirement["activation_satisfying_statuses"] == [
        "asserted",
        "confirmed",
    ]
    assert requirement["confirmation_required_for_activation"] is True
    assert requirement["confirmation_elicitation_priority"] == "before_fresh_gap"
    assert requirement["confirmation_effect"] == "promote_existing_candidate"
    assert requirement["confirmation_creates_new_claim"] is False
    assert "{counterpart_name}" in requirement["confirmation_question_template"]
    assert requirement["tentative_counts_as_satisfied"] is False
    assert requirement["gap_status"] == "asserted_relation_missing"
    assert requirement["creation_blocking"] is False
    assert requirement["question"] == "Which Von user is a member of Primary Labs?"
    assert diagnostics["builtin_profile_type_ids"] == [VON_USER_ORGANISATION_TYPE_ID]
    assert "specific_to_user" not in json.dumps(queries)


def test_tentative_autoformalisation_is_advisory_and_non_mutating():
    instance = {
        "concept_id": "#V#new_org",
        "name": "New organisation",
        "relationships": {"is_an_instance_of": [VON_USER_ORGANISATION_TYPE_ID]},
    }
    before = json.loads(json.dumps(instance))
    service = ConstitutiveRelationRequirementService(
        text_reader=_empty_text_reader,
        concept_finder=lambda _query, _projection: [],
        descendant_resolver=lambda type_id: [type_id],
    )

    missing, _ = service.get_missing_requirements(
        instance=instance,
        type_depth_by_id={VON_USER_ORGANISATION_TYPE_ID: 0},
    )

    assert instance == before
    assert missing[0]["auto_formalisation_target_status"] == "tentative"
    assert missing[0]["activation_minimum_status"] == "asserted"
    assert missing[0]["confirmation_required_for_activation"] is True
    assert missing[0]["tentative_counts_as_satisfied"] is False
    assert missing[0]["gap_status"] == "asserted_relation_missing"
    assert missing[0]["current_cardinality"] == 0
    assert missing[0]["creation_blocking"] is False


def test_incoming_requirement_counts_only_a_qualified_other_argument_type():
    candidates = [
        {
            "concept_id": "#V#service_account",
            "relationships": {"is_an_instance_of": ["#V#software_agent"]},
        }
    ]

    service = ConstitutiveRelationRequirementService(
        text_reader=_empty_text_reader,
        concept_finder=lambda _query, _projection: list(candidates),
        descendant_resolver=lambda _type_id: [
            VON_USER_TYPE_ID,
            "#V#administrator_von_user",
        ],
    )
    instance = {
        "concept_id": "#V#primary_labs",
        "name": "Primary Labs",
        "relationships": {"is_an_instance_of": [VON_USER_ORGANISATION_TYPE_ID]},
    }

    missing, _ = service.get_missing_requirements(
        instance=instance,
        type_depth_by_id={VON_USER_ORGANISATION_TYPE_ID: 0},
    )
    assert missing[0]["current_cardinality"] == 0

    candidates.append(
        {
            "concept_id": "#V#alice",
            "relationships": {"is_an_instance_of": ["#V#administrator_von_user"]},
        }
    )
    missing, _ = service.get_missing_requirements(
        instance=instance,
        type_depth_by_id={VON_USER_ORGANISATION_TYPE_ID: 0},
    )
    assert missing == []


def test_represented_parent_requirement_is_inherited_and_supports_subject_focal():
    parent_type_id = "#V#governed_project"
    profile = {
        "schema_version": CONSTITUTIVE_REQUIREMENTS_SCHEMA_VERSION,
        "requirements": [
            {
                "requirement_id": "project_has_lead",
                "predicate_concept_id": "#V#has_lead",
                "focal_argument": "subject",
                "other_argument_type_concept_id": "#V#person",
                "minimum_cardinality": 1,
                "reason": "A governed project needs a responsible lead.",
                "activation_relevance": "project_activation",
                "authority_relevance": "project_governance",
                "auto_formalisation_allowed": False,
            }
        ],
    }

    def _texts(concept_ids, **_kwargs):
        return {
            concept_id: (
                [
                    {
                        "predicate": CONSTITUTIVE_REQUIREMENTS_PREDICATE,
                        "text": json.dumps(profile),
                    }
                ]
                if concept_id == parent_type_id
                else []
            )
            for concept_id in concept_ids
        }

    service = ConstitutiveRelationRequirementService(
        text_reader=_texts,
        concept_finder=lambda _query, _projection: [
            {
                "concept_id": "#V#alice",
                "relationships": {"is_an_instance_of": ["#V#academic"]},
            }
        ],
        descendant_resolver=lambda _type_id: ["#V#person", "#V#academic"],
    )
    type_depths = {"#V#research_project": 0, parent_type_id: 1}
    requirements, diagnostics = service.load_requirements(type_depth_by_id=type_depths)

    assert requirements[0]["declared_on_type_concept_id"] == parent_type_id
    assert requirements[0]["declaration_depth"] == 1
    assert requirements[0]["inherited"] is True
    assert diagnostics["represented_profile_type_ids"] == [parent_type_id]

    missing, _ = service.get_missing_requirements(
        instance={
            "concept_id": "#V#apollo",
            "name": "Apollo",
            "relationships": {
                "is_an_instance_of": ["#V#research_project"],
                "#V#has_lead": ["#V#alice"],
            },
        },
        type_depth_by_id=type_depths,
    )
    assert missing == []


def test_valid_live_profile_replaces_builtin_compatibility_profile():
    empty_live_profile = {
        "schema_version": CONSTITUTIVE_REQUIREMENTS_SCHEMA_VERSION,
        "requirements": [],
    }

    def _texts(concept_ids, **_kwargs):
        return {
            concept_id: [
                {
                    "predicate": CONSTITUTIVE_REQUIREMENTS_PREDICATE,
                    "text": json.dumps(empty_live_profile),
                }
            ]
            for concept_id in concept_ids
        }

    service = ConstitutiveRelationRequirementService(text_reader=_texts)
    requirements, diagnostics = service.load_requirements(
        type_depth_by_id={VON_USER_ORGANISATION_TYPE_ID: 0}
    )

    assert requirements == []
    assert diagnostics["represented_profile_type_ids"] == [
        VON_USER_ORGANISATION_TYPE_ID
    ]
    assert diagnostics["builtin_profile_type_ids"] == []


def test_malformed_live_profile_fails_soft_without_laundering_builtin_fallback():
    def _texts(concept_ids, **_kwargs):
        return {
            concept_id: [
                {
                    "predicate": CONSTITUTIVE_REQUIREMENTS_PREDICATE,
                    "text": "not-json",
                }
            ]
            for concept_id in concept_ids
        }

    service = ConstitutiveRelationRequirementService(text_reader=_texts)
    requirements, diagnostics = service.load_requirements(
        type_depth_by_id={VON_USER_ORGANISATION_TYPE_ID: 0}
    )

    assert requirements == []
    assert diagnostics["malformed_profile_type_ids"] == [VON_USER_ORGANISATION_TYPE_ID]
    assert diagnostics["builtin_profile_type_ids"] == []
    assert diagnostics["fallback_suppressed_type_ids"] == [
        VON_USER_ORGANISATION_TYPE_ID
    ]


def test_unreadable_live_profile_fails_soft_and_suppresses_unknown_fallback():
    def _failed_reader(_concept_ids, **_kwargs):
        raise RuntimeError("temporary read failure")

    service = ConstitutiveRelationRequirementService(text_reader=_failed_reader)
    requirements, diagnostics = service.load_requirements(
        type_depth_by_id={VON_USER_ORGANISATION_TYPE_ID: 0}
    )

    assert requirements == []
    assert diagnostics["profile_read_failed"] is True
    assert diagnostics["fallback_suppressed_type_ids"] == [
        VON_USER_ORGANISATION_TYPE_ID
    ]


def test_ensure_canonical_profile_writes_and_reads_back_exact_representation():
    stored = {}

    def _writer(**kwargs):
        stored[kwargs["subject_concept_id"]] = {
            "predicate": kwargs["predicate"],
            "text": kwargs["text"],
        }
        return {"success": True}

    def _reader(concept_ids, **_kwargs):
        return {
            concept_id: ([stored[concept_id]] if concept_id in stored else [])
            for concept_id in concept_ids
        }

    result = ensure_canonical_constitutive_relation_requirement_profiles(
        concept_getter=lambda concept_id: {"concept_id": concept_id},
        text_writer=_writer,
        text_reader=_reader,
    )

    assert result["success"] is True
    assert result["persisted_type_concept_ids"] == [VON_USER_ORGANISATION_TYPE_ID]
    assert result["read_back_type_concept_ids"] == [VON_USER_ORGANISATION_TYPE_ID]

    requirements, diagnostics = ConstitutiveRelationRequirementService(
        text_reader=_reader
    ).load_requirements(type_depth_by_id={VON_USER_ORGANISATION_TYPE_ID: 0})
    assert requirements[0]["declaration_source"] == "represented"
    assert diagnostics["builtin_profile_type_ids"] == []


def test_ensure_canonical_profile_reports_read_back_mismatch():
    represented_empty_profile = json.dumps(
        {
            "schema_version": CONSTITUTIVE_REQUIREMENTS_SCHEMA_VERSION,
            "requirements": [],
        }
    )

    reads = 0

    def _reader(concept_ids, **_kwargs):
        nonlocal reads
        reads += 1
        return {
            concept_id: (
                []
                if reads == 1
                else [
                    {
                        "predicate": CONSTITUTIVE_REQUIREMENTS_PREDICATE,
                        "text": represented_empty_profile,
                    }
                ]
            )
            for concept_id in concept_ids
        }

    result = ensure_canonical_constitutive_relation_requirement_profiles(
        concept_getter=lambda concept_id: {"concept_id": concept_id},
        text_writer=lambda **_kwargs: {"success": True},
        text_reader=_reader,
    )

    assert result["success"] is False
    assert (
        "profile_read_back_mismatch"
        in result["errors_by_type_concept_id"][VON_USER_ORGANISATION_TYPE_ID]
    )


def test_seed_bundle_is_the_single_repo_source_for_compatibility_profile():
    profiles = canonical_constitutive_requirement_profile_blueprints()

    profile = profiles[VON_USER_ORGANISATION_TYPE_ID]
    assert profile["schema_version"] == CONSTITUTIVE_REQUIREMENTS_SCHEMA_VERSION
    assert profile["requirements"][0]["predicate_concept_id"] == (
        MEMBER_OF_PREDICATE_ID
    )


def test_missing_live_profile_fails_soft_when_seed_fallback_cannot_load(monkeypatch):
    monkeypatch.setattr(
        service,
        "canonical_constitutive_requirement_profile_blueprints",
        lambda: (_ for _ in ()).throw(ValueError("bad seed")),
    )

    requirements, diagnostics = ConstitutiveRelationRequirementService(
        text_reader=_empty_text_reader
    ).load_requirements(type_depth_by_id={VON_USER_ORGANISATION_TYPE_ID: 0})

    assert requirements == []
    assert diagnostics["compatibility_profile_load_failed"] is True
    assert diagnostics["compatibility_profile_load_error_type"] == "ValueError"


def test_ensure_preserves_valid_live_profile_as_represented_authority():
    live_profile = {
        "schema_version": CONSTITUTIVE_REQUIREMENTS_SCHEMA_VERSION,
        "requirements": [],
    }
    writes = []

    result = ensure_canonical_constitutive_relation_requirement_profiles(
        concept_getter=lambda concept_id: {"concept_id": concept_id},
        text_writer=lambda **kwargs: writes.append(kwargs) or {"success": True},
        text_reader=lambda concept_ids, **_kwargs: {
            concept_id: [
                {
                    "predicate": CONSTITUTIVE_REQUIREMENTS_PREDICATE,
                    "text": json.dumps(live_profile),
                }
            ]
            for concept_id in concept_ids
        },
    )

    assert result["success"] is True
    assert result["changed"] is False
    assert result["represented_override_type_concept_ids"] == [
        VON_USER_ORGANISATION_TYPE_ID
    ]
    assert writes == []


def test_ensure_does_not_overwrite_malformed_live_profile():
    writes = []

    result = ensure_canonical_constitutive_relation_requirement_profiles(
        concept_getter=lambda concept_id: {"concept_id": concept_id},
        text_writer=lambda **kwargs: writes.append(kwargs) or {"success": True},
        text_reader=lambda concept_ids, **_kwargs: {
            concept_id: [
                {
                    "predicate": CONSTITUTIVE_REQUIREMENTS_PREDICATE,
                    "text": "not-json",
                }
            ]
            for concept_id in concept_ids
        },
    )

    assert result["success"] is False
    assert result["errors_by_type_concept_id"] == {
        VON_USER_ORGANISATION_TYPE_ID: "represented_profile_malformed"
    }
    assert writes == []


def test_startup_check_uses_only_dependency_receipt(monkeypatch):
    observed = {}
    monkeypatch.setattr(
        service.seed_freshness,
        "check_startup_seed_freshness",
        lambda **kwargs: (
            observed.update(kwargs)
            or {
                "fresh": True,
                "reason": "dependency_receipt_current",
                "metadata": {"counts": {"profiles_read_back": 1}},
            }
        ),
    )
    monkeypatch.setattr(
        service.seed_freshness,
        "public_startup_seed_freshness_result",
        dict,
    )

    report = ensure_constitutive_relation_requirement_profiles_current_for_startup()

    assert report["ready"] is True
    assert report["canonical_read_batches"] == {
        "concepts": 0,
        "text_assertions": 0,
    }
    assert observed["family_id"] == CONSTITUTIVE_REQUIREMENT_STARTUP_FAMILY_ID
    assert observed["concept_ids"] == [VON_USER_ORGANISATION_TYPE_ID]
    assert observed["text_relation_subject_ids"] == [VON_USER_ORGANISATION_TYPE_ID]


def test_reconciliation_rechecks_after_materialising_profile(monkeypatch):
    canonical_passes = [
        {
            "success": True,
            "changed": True,
            "freshness_receipt": {
                "persisted": False,
                "reason": "canonical_state_changed",
            },
        },
        {
            "success": True,
            "changed": False,
            "freshness_receipt": {
                "persisted": True,
                "reason": "freshness_receipt_persisted",
            },
        },
    ]
    monkeypatch.setattr(
        service.seed_freshness,
        "begin_startup_seed_freshness_observation",
        lambda **_kwargs: {"success": True},
    )
    monkeypatch.setattr(
        service,
        "ensure_canonical_constitutive_relation_requirement_profiles",
        lambda **_kwargs: canonical_passes.pop(0),
    )

    report = reconcile_canonical_constitutive_relation_requirement_profiles()

    assert report["ready"] is True
    assert report["changed"] is True
    assert report["reconciliation_pass_count"] == 2
