from __future__ import annotations

from src.backend.services.thinking_semantic_projection_service import (
    SEMANTIC_OPERATION_SCHEMA_VERSION,
    build_semantic_operation_projection,
    normalise_semantic_operation_projection,
)


def _relation_projection(**overrides):
    arguments = {
        "source_id": "#V#nathan_young_doctoral_candidature_situation",
        "predicate": "#V#has_doctoral_supervisor",
        "target": "#V#robert_amor",
    }
    arguments.update(overrides.pop("arguments", {}))
    values = {
        "operation_id": "call-relationship",
        "capability_name": "add_relationship",
        "execution_method": "add_relationship",
        "capability_kind": "registered_tool",
        "arguments": arguments,
        "lifecycle_status": "running",
    }
    values.update(overrides)
    return build_semantic_operation_projection(**values)


def test_relation_projection_has_role_labelled_arguments_and_readable_start() -> None:
    projection = _relation_projection()

    assert projection["schema_version"] == SEMANTIC_OPERATION_SCHEMA_VERSION
    assert projection["visibility"] == "conversation_scope"
    assert [item["role"] for item in projection["arguments"]] == [
        "subject",
        "predicate",
        "object",
    ]
    assert projection["relation"]["predicate"]["concept_id"] == (
        "#V#has_doctoral_supervisor"
    )
    assert projection["summary"] == (
        "Add Relationship: Subject: Nathan Young Doctoral Candidature Situation; "
        "Relation: Has Doctoral Supervisor; Object: Robert Amor (in progress)."
    )


def test_represented_workflow_uses_human_label_without_losing_identities() -> None:
    projection = build_semantic_operation_projection(
        operation_id="call-arxiv-workflow",
        capability_name="represented_workflow_0348c4f884fc88757dcf",
        execution_method="workflow_execute",
        capability_kind="represented_workflow",
        capability_display_name="Arxiv Paper Representation Workflow",
        arguments={"workflow_id": "#V#arxiv_paper_representation_workflow"},
        lifecycle_status="running",
    )

    assert projection["capability"] == {
        "id": "represented_workflow_0348c4f884fc88757dcf",
        "label": "Arxiv Paper Representation Workflow",
        "kind": "represented_workflow",
        "execution_method": "workflow_execute",
    }
    assert projection["arguments"][0]["value"] == (
        "#V#arxiv_paper_representation_workflow"
    )
    assert projection["summary"] == "Using Arxiv Paper Representation Workflow."
    assert normalise_semantic_operation_projection(projection) == projection


def test_legacy_workflow_projection_falls_back_to_canonical_workflow_id() -> None:
    projection = build_semantic_operation_projection(
        operation_id="call-arxiv-workflow",
        capability_name="represented_workflow_0348c4f884fc88757dcf",
        execution_method="workflow_execute",
        capability_kind="represented_workflow",
        arguments={"workflow_id": "#V#arxiv_paper_representation_workflow"},
        lifecycle_status="running",
    )
    projection["capability"]["label"] = (
        "Represented Workflow 0348C4F884Fc88757Dcf"
    )
    projection["summary"] = "Using Represented Workflow 0348C4F884Fc88757Dcf."
    projection["arguments"] = []
    projection["outcome"] = {
        "status": "failed",
        "success": False,
        "workflow_id": "#V#arxiv_paper_representation_workflow",
    }
    projection["lifecycle_status"] = "failed"

    normalised = normalise_semantic_operation_projection(projection)

    assert normalised is not None
    assert normalised["capability"]["id"] == (
        "represented_workflow_0348c4f884fc88757dcf"
    )
    assert normalised["capability"]["label"] == (
        "Arxiv Paper Representation Workflow"
    )
    assert normalised["summary"] == (
        "Arxiv Paper Representation Workflow did not succeed."
    )


def test_nested_predicate_reference_and_receipt_only_unchanged_are_truthful() -> None:
    projection = _relation_projection(
        arguments={
            "predicate": None,
            "predicate_ref": {"concept_id": "#V#has_doctoral_supervisor"},
        },
        lifecycle_status="succeeded",
        success=True,
        result={"effect_status": "succeeded", "changed": False},
    )

    assert projection["relation"]["predicate"]["concept_id"] == (
        "#V#has_doctoral_supervisor"
    )
    assert projection["outcome"]["changed"] is False
    assert projection["verification"] == {
        "status": "receipt_only",
        "canonical_read_back_present": False,
        "source": "effect_receipt",
    }
    assert projection["summary"] == (
        "Add Relationship reported that no change was needed: "
        "Subject: Nathan Young Doctoral Candidature Situation; "
        "Relation: Has Doctoral Supervisor; Object: Robert Amor."
    )


def test_explicit_successful_matching_canonical_read_back_is_labelled_verified() -> (
    None
):
    projection = _relation_projection(
        lifecycle_status="succeeded",
        success=True,
        result={
            "success": True,
            "effect_status": "succeeded",
            "changed": False,
            "canonical_read_back": {
                "source_id": "#V#nathan_young_doctoral_candidature_situation",
                "predicate": "#V#has_doctoral_supervisor",
                "target": "#V#robert_amor",
            },
            "effect_id": "actor-private-effect",
            "evidence_id": "actor-private-evidence",
        },
    )

    assert projection["verification"] == {
        "status": "verified",
        "canonical_read_back_present": True,
        "source": "canonical_read_back",
    }
    assert projection["summary"].startswith(
        "Add Relationship verified that no change was needed:"
    )
    assert "effect_id" not in projection["outcome"]
    assert "evidence_id" not in projection["outcome"]


def test_real_scoped_assertion_read_back_shape_can_verify_matching_relation() -> None:
    projection = build_semantic_operation_projection(
        operation_id="call-scoped-assertion",
        capability_name="upsert_scoped_assertion",
        execution_method="upsert_scoped_assertion",
        capability_kind="registered_tool",
        arguments={
            "subject_concept_id": "#V#candidate_situation",
            "predicate": "#V#has_doctoral_supervisor",
            "target_concept_id": "#V#robert_amor",
        },
        lifecycle_status="succeeded",
        success=True,
        result={
            "success": True,
            "effect_status": "succeeded",
            "changed": False,
            "canonical_read_back": {
                "schema_version": "scoped_knowledge_assertion.v1",
                "assertion_id": "ska_1234567890abcdef",
                "subject_concept_id": "#V#candidate_situation",
                "predicate": "#V#has_doctoral_supervisor",
                "object_kind": "concept",
                "object_concept_id": "#V#robert_amor",
                "status": "asserted",
            },
        },
    )

    assert projection["verification"]["status"] == "verified"
    assert projection["summary"].startswith(
        "Upsert Scoped Assertion verified that no change was needed:"
    )


def test_failed_canonical_read_back_object_remains_receipt_only() -> None:
    projection = _relation_projection(
        lifecycle_status="succeeded",
        success=True,
        result={
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "canonical_read_back": {
                "success": False,
                "error": "read-back unavailable",
            },
        },
    )

    assert projection["verification"] == {
        "status": "receipt_only",
        "canonical_read_back_present": True,
        "source": "effect_receipt",
    }
    assert "verified" not in projection["summary"]


def test_arbitrary_nonempty_canonical_read_back_remains_receipt_only() -> None:
    projection = _relation_projection(
        lifecycle_status="succeeded",
        success=True,
        result={
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "canonical_read_back": {
                "success": True,
                "detail": "a payload existed but did not prove the relation",
            },
        },
    )

    assert projection["verification"]["status"] == "receipt_only"
    assert "verified" not in projection["summary"]


def test_mismatched_relation_read_back_remains_receipt_only() -> None:
    projection = _relation_projection(
        lifecycle_status="succeeded",
        success=True,
        result={
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "canonical_read_back": {
                "subject_concept_id": (
                    "#V#nathan_young_doctoral_candidature_situation"
                ),
                "predicate": "#V#has_doctoral_supervisor",
                "object_concept_id": "#V#someone_else",
            },
        },
    )

    assert projection["verification"]["status"] == "receipt_only"
    assert "verified" not in projection["summary"]


def test_empty_canonical_read_back_is_present_but_not_claimed_verified() -> None:
    projection = _relation_projection(
        lifecycle_status="succeeded",
        success=True,
        result={
            "effect_status": "succeeded",
            "changed": False,
            "canonical_read_back": {},
        },
    )

    assert projection["verification"] == {
        "status": "receipt_only",
        "canonical_read_back_present": True,
        "source": "effect_receipt",
    }
    assert "verified" not in projection["summary"]


def test_relation_operation_wording_preserves_remove_partial_and_unknown() -> None:
    removed = _relation_projection(
        capability_name="remove_relationship",
        execution_method="remove_relationship",
        lifecycle_status="succeeded",
        success=True,
        result={"effect_status": "succeeded", "changed": True},
    )
    partial = _relation_projection(
        capability_name="preview_remove_relationship",
        execution_method="preview_remove_relationship",
        lifecycle_status="partial",
        success=False,
        result={"effect_status": "partial", "changed": True},
    )
    unknown = _relation_projection(
        lifecycle_status="indeterminate",
        success=False,
        result={
            "effect_status": "indeterminate",
            "mutation_outcome": "unknown",
        },
    )

    assert removed["summary"].startswith("Remove Relationship reported a change:")
    assert partial["summary"].startswith(
        "Preview Remove Relationship completed only partially:"
    )
    assert unknown["summary"].startswith(
        "The outcome of Add Relationship could not be verified:"
    )
    assert "Represented" not in " ".join(
        (removed["summary"], partial["summary"], unknown["summary"])
    )


def test_literal_text_preserves_case_and_punctuation() -> None:
    projection = build_semantic_operation_projection(
        operation_id="call-text",
        capability_name="upsert_text_relation",
        execution_method="upsert_text_relation",
        capability_kind="registered_tool",
        arguments={
            "concept_id": "#V#robert_amor",
            "predicate": "#V#has_professional_title",
            "text": "Professor of Computer Science (emeritus).",
        },
        lifecycle_status="succeeded",
        success=True,
        result={"effect_status": "succeeded", "changed": False},
    )

    assert projection["relation"]["object"]["display"] == (
        "Professor of Computer Science (emeritus)."
    )
    assert "Value: “Professor of Computer Science (emeritus).”" in projection["summary"]
    assert "Professor Of Computer Science" not in projection["summary"]


def test_concept_search_projects_query_and_bounded_named_results() -> None:
    running = build_semantic_operation_projection(
        operation_id="call-search-running",
        capability_name="search_concepts",
        execution_method="search_concepts",
        capability_kind="registered_tool",
        arguments={"query": "University of Auckland"},
        lifecycle_status="running",
    )
    completed = build_semantic_operation_projection(
        operation_id="call-search-completed",
        capability_name="search_concepts",
        execution_method="search_concepts",
        capability_kind="registered_tool",
        arguments={"query": "University of Auckland"},
        lifecycle_status="succeeded",
        success=True,
        result={
            "total_count": 4,
            "results": [
                {
                    "concept_id": "#V#university_of_auckland",
                    "name": "University of Auckland",
                },
                {"concept_id": "#V#university", "name": "University"},
            ],
        },
    )

    assert running["focus"] == {
        "role": "query",
        "label": "Query",
        "source_argument": "query",
        "value_kind": "text",
        "value": "University of Auckland",
        "display": "University of Auckland",
    }
    assert running["summary"] == (
        "Search Concepts: Query: “University of Auckland” (in progress)."
    )
    assert completed["observation"]["count"] == 4
    assert completed["observation"]["items"] == [
        {
            "name": "University of Auckland",
            "identifier": "#V#university_of_auckland",
        },
        {"name": "University", "identifier": "#V#university"},
    ]
    assert completed["summary"] == (
        "Search Concepts returned 4 concept matches: University of Auckland, "
        "University and 2 others for Query: “University of Auckland”."
    )


def test_predicate_incidence_projects_anchor_and_predicate_results() -> None:
    projection = build_semantic_operation_projection(
        operation_id="call-incidence",
        capability_name="get_predicate_incidence",
        execution_method="get_predicate_incidence",
        capability_kind="registered_tool",
        arguments={"concept_id": "#V#university_of_auckland"},
        lifecycle_status="succeeded",
        success=True,
        result={
            "total_predicates": 3,
            "predicates": [
                {"predicate_concept_id": "#V#has_affiliation"},
                {"predicate_concept_id": "#V#is_member_of"},
            ],
        },
    )

    assert projection["focus"]["label"] == "Concept"
    assert projection["focus"]["concept_id"] == "#V#university_of_auckland"
    assert projection["observation"]["count_label"] == "predicates"
    assert projection["summary"] == (
        "Get Predicate Incidence returned 3 predicates: Has Affiliation, "
        "Is Member Of and 1 other for Concept: University Of Auckland."
    )

    incomplete = build_semantic_operation_projection(
        operation_id="call-incidence-incomplete",
        capability_name="get_predicate_incidence",
        execution_method="get_predicate_incidence",
        capability_kind="registered_tool",
        arguments={"concept_id": "#V#university_of_auckland"},
        lifecycle_status="succeeded",
        success=True,
        result={
            "total_predicates": 0,
            "predicates": [],
            "coverage_complete": False,
        },
    )
    assert incomplete["summary"] == (
        "Get Predicate Incidence returned 0 predicates (coverage incomplete) "
        "for Concept: University Of Auckland."
    )


def test_live_predicate_identifiers_are_not_replaced_by_the_anchor_concept() -> None:
    projection = build_semantic_operation_projection(
        operation_id="call-live-incidence",
        capability_name="get_predicate_incidence",
        execution_method="get_predicate_incidence",
        capability_kind="registered_tool",
        arguments={"concept_id": "#V#university_of_auckland_strong_ai_lab"},
        lifecycle_status="succeeded",
        success=True,
        result={
            "concept_id": "#V#university_of_auckland_strong_ai_lab",
            "total_predicates": 5,
            "counts_are_lower_bounds": True,
            "coverage_complete": False,
            "predicates": [
                {"predicate_concept_id": "hasName", "relation_hit_count": 5},
                {
                    "predicate_concept_id": "is_an_instance_of",
                    "relation_hit_count": 5,
                },
                {"predicate_concept_id": "has_instance", "relation_hit_count": 2},
                {"predicate_concept_id": "hasDescription", "relation_hit_count": 1},
                {"predicate_concept_id": "hasNote", "relation_hit_count": 1},
            ],
        },
    )

    assert projection["observation"]["items"] == [
        {"name": "Has Name", "identifier": "hasName", "identifier_kind": "predicate"},
        {
            "name": "Is An Instance Of",
            "identifier": "is_an_instance_of",
            "identifier_kind": "predicate",
        },
        {
            "name": "Has Instance",
            "identifier": "has_instance",
            "identifier_kind": "predicate",
        },
        {
            "name": "Has Description",
            "identifier": "hasDescription",
            "identifier_kind": "predicate",
        },
        {"name": "Has Note", "identifier": "hasNote", "identifier_kind": "predicate"},
    ]
    assert "University Of Auckland Strong Ai Lab" not in ", ".join(
        item["name"] for item in projection["observation"]["items"]
    )
    assert projection["summary"] == (
        "Get Predicate Incidence returned at least 5 predicates: Has Name, "
        "Is An Instance Of, Has Instance, Has Description and Has Note for "
        "Concept: University Of Auckland Strong Ai Lab."
    )
    assert normalise_semantic_operation_projection(projection) == projection


def test_gmail_reads_project_mailbox_and_bounded_message_metadata_without_body() -> (
    None
):
    auth = build_semantic_operation_projection(
        operation_id="call-gmail-auth",
        capability_name="gmail_get_auth_config",
        execution_method="gmail_get_auth_config",
        capability_kind="registered_tool",
        arguments={"profile_id": "vonwitbrock-gmail"},
        lifecycle_status="succeeded",
        success=True,
        result={
            "success": True,
            "profile_id": "vonwitbrock-gmail",
            "authorised_email": "zhanvonwitbrock@gmail.com",
            "token_status": "authorised",
        },
    )
    listed = build_semantic_operation_projection(
        operation_id="call-gmail-list",
        capability_name="gmail_list_messages",
        execution_method="gmail_list_messages",
        capability_kind="registered_tool",
        arguments={"profile": "vonwitbrock-gmail", "query": "in:inbox"},
        lifecycle_status="succeeded",
        success=True,
        result={
            "profile": "vonwitbrock-gmail",
            "authorised_email": "zhanvonwitbrock@gmail.com",
            "messages": [{"id": "19c123", "message_id": "19c123"}],
        },
    )
    fetched = build_semantic_operation_projection(
        operation_id="call-gmail-get",
        capability_name="gmail_get_message",
        execution_method="gmail_get_message",
        capability_kind="registered_tool",
        arguments={"profile": "vonwitbrock-gmail", "message_id": "19c123"},
        lifecycle_status="succeeded",
        success=True,
        result={
            "profile": "vonwitbrock-gmail",
            "authorised_email": "zhanvonwitbrock@gmail.com",
            "message_id": "19c123",
            "subject": "Thesis Submission and Examiner Nomination",
            "sender": "Xianda Zheng <xzhe162@aucklanduni.ac.nz>",
            "date": "Wed, 5 Aug 2026 19:48:06 +0800",
            "body": "Private message body that must not enter Thinking.",
        },
    )

    assert auth["summary"] == (
        "Gmail Get Auth Config returned zhanvonwitbrock@gmail.com for Mailbox "
        "profile: Vonwitbrock Gmail (Authorisation: authorised)."
    )
    assert listed["summary"] == (
        "Gmail List Messages returned 1 message for Query: “in:inbox” "
        "(Mailbox: zhanvonwitbrock@gmail.com)."
    )
    assert fetched["summary"] == (
        "Gmail Get Message returned Thesis Submission and Examiner Nomination for "
        "Mailbox profile: Vonwitbrock Gmail (Mailbox: zhanvonwitbrock@gmail.com; "
        "Sender: Xianda Zheng <xzhe162@aucklanduni.ac.nz>; Date: Wed, 5 Aug 2026 "
        "19:48:06 +0800)."
    )
    assert [item["role"] for item in fetched["arguments"]] == [
        "mailbox",
        "message",
    ]
    assert fetched["observation"]["items"] == [
        {
            "name": "Thesis Submission and Examiner Nomination",
            "identifier": "19c123",
            "identifier_kind": "message",
        }
    ]
    assert "body" not in fetched["observation"]
    assert "Private message body" not in fetched["summary"]
    assert normalise_semantic_operation_projection(auth) == auth
    assert normalise_semantic_operation_projection(listed) == listed
    assert normalise_semantic_operation_projection(fetched) == fetched


def test_conversation_get_projects_name_and_date_without_transcript_content() -> None:
    projection = build_semantic_operation_projection(
        operation_id="call-conversation-get",
        capability_name="conversation_get",
        execution_method="conversation_get",
        capability_kind="registered_tool",
        arguments={"session_id": "session-1"},
        lifecycle_status="succeeded",
        success=True,
        result={
            "success": True,
            "session_name": "Mars project planning",
            "last_message_at": "2026-08-21T10:30:00+00:00",
            "created_at": "2026-08-20T08:00:00+00:00",
            "segments": [[{"role": "user", "content": "Private transcript content"}]],
        },
    )

    assert projection["observation"] == {
        "items": [{"name": "Mars project planning"}],
        "count_is_lower_bound": False,
        "details": [
            {
                "label": "Date",
                "source_field": "last_message_at",
                "value_kind": "datetime",
                "value": "2026-08-21T10:30:00+00:00",
            }
        ],
    }
    assert projection["summary"] == (
        "Conversation Get returned Mars project planning "
        "(Date: 2026-08-21T10:30:00+00:00)."
    )
    assert "Private transcript content" not in projection["summary"]
    assert normalise_semantic_operation_projection(projection) == projection


def test_fetch_concept_projects_requested_and_returned_concept() -> None:
    projection = build_semantic_operation_projection(
        operation_id="call-fetch",
        capability_name="fetch_concept",
        execution_method="fetch_concept",
        capability_kind="registered_tool",
        arguments={"concept_id": "#V#university_of_auckland"},
        lifecycle_status="succeeded",
        success=True,
        result={
            "concept_id": "#V#university_of_auckland",
            "name": "University of Auckland",
            "description": "This unbounded field must not enter Thinking.",
        },
    )

    assert projection["observation"] == {
        "items": [
            {
                "name": "University of Auckland",
                "identifier": "#V#university_of_auckland",
            }
        ],
        "count_is_lower_bound": False,
    }
    assert projection["summary"] == (
        "Fetch Concept returned University of Auckland for Concept: "
        "University Of Auckland."
    )
    assert "description" not in projection["observation"]
    assert normalise_semantic_operation_projection(projection) == projection


def test_read_observation_is_bounded_and_drops_non_concept_identifiers() -> None:
    projection = build_semantic_operation_projection(
        operation_id="call-bounded-search",
        capability_name="search_concepts",
        execution_method="search_concepts",
        capability_kind="registered_tool",
        arguments={"query": "bounded results"},
        lifecycle_status="succeeded",
        success=True,
        result={
            "total_count": 20,
            "results": [
                {
                    "concept_id": (
                        "not-a-concept-secret" if index == 0 else f"#V#result_{index}"
                    ),
                    "name": f"Result {index} " + ("x" * 300),
                    "unbounded_payload": "must not survive",
                }
                for index in range(20)
            ],
            "unbounded_payload": "must not survive",
        },
    )

    assert len(projection["observation"]["items"]) == 5
    assert len(projection["observation"]["items"][0]["name"]) <= 120
    assert "identifier" not in projection["observation"]["items"][0]
    assert "unbounded_payload" not in projection["observation"]
    assert len(projection["summary"]) <= 1_024
    assert normalise_semantic_operation_projection(projection) == projection


def test_projection_bounds_values_and_does_not_invent_a_relation() -> None:
    projection = build_semantic_operation_projection(
        operation_id="call-generic",
        capability_name="create_concepts",
        execution_method="create_concepts",
        capability_kind="registered_tool",
        arguments={
            "name": "x" * 1_000,
            "unused_1": "one",
            "unused_2": "two",
        },
        lifecycle_status="succeeded",
        success=True,
        result={"effect_status": "succeeded", "changed": True},
    )

    assert "relation" not in projection
    assert len(projection["arguments"]) == 1
    assert len(projection["arguments"][0]["value"]) <= 240
    assert projection["summary"] == "Finished Create Concepts."


def test_strict_normaliser_reconstructs_bounded_allowlisted_projection() -> None:
    projection = _relation_projection(
        lifecycle_status="succeeded",
        success=True,
        result={"effect_status": "succeeded", "changed": True},
    )
    projection["summary"] = "<script>untrusted()</script>" + ("x" * 5_000)
    projection["unexpected"] = {"unbounded": "x" * 10_000}
    projection["arguments"][0]["value"] = "#V#" + ("x" * 1_000)
    projection["arguments"][0]["display"] = "x" * 1_000
    projection["outcome"]["effect_id"] = "actor-private-effect"
    projection["outcome"]["evidence_id"] = "actor-private-evidence"
    projection["verification"] = {
        "status": "verified",
        "canonical_read_back_present": False,
        "source": "none",
    }

    normalised = normalise_semantic_operation_projection(projection)

    assert normalised is not None
    assert "unexpected" not in normalised
    assert "<script>" not in normalised["summary"]
    assert len(normalised["arguments"][0]["value"]) <= 240
    assert len(normalised["arguments"][0]["display"]) <= 240
    assert len(normalised["summary"]) <= 1_024
    assert "effect_id" not in normalised["outcome"]
    assert "evidence_id" not in normalised["outcome"]
    assert normalised["verification"] == {
        "status": "receipt_only",
        "canonical_read_back_present": False,
        "source": "effect_receipt",
    }


def test_strict_normaliser_rejects_unknown_schema_or_visibility_contract() -> None:
    projection = _relation_projection()
    forged_running_verification = normalise_semantic_operation_projection(
        {
            **projection,
            "verification": {
                "status": "verified",
                "canonical_read_back_present": True,
                "source": "canonical_read_back",
            },
        }
    )

    assert forged_running_verification is not None
    assert forged_running_verification["verification"]["status"] == "unknown"
    assert (
        normalise_semantic_operation_projection(
            {**projection, "schema_version": "thinking_semantic_operation.v2"}
        )
        is None
    )
    assert (
        normalise_semantic_operation_projection(
            {**projection, "visibility": "organisation_scope"}
        )
        is None
    )
