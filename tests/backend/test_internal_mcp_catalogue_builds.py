import pytest


@pytest.fixture
def trusted_gmail_operator(monkeypatch):
    """Explicit local-operator provenance for handler-mechanics tests."""

    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp.gateway import (
        bind_internal_mcp_actor_context_source,
    )

    profile_ids = (
        "zhan-gmail",
        "vonwitbrock-gmail",
        "represented-profile",
        "operator-profile",
    )
    monkeypatch.setattr(
        gmail_service,
        "load_profiles_from_env",
        lambda: {
            profile_id: gmail_service.GmailProfile(
                profile_id=profile_id,
                token_path=f"/nonexistent/{profile_id}.json",
            )
            for profile_id in profile_ids
        },
    )
    with bind_internal_mcp_actor_context_source(
        "trusted_operator_payload_fallback"
    ):
        yield


class _GmailHttpError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.resp = type("Response", (), {"status": status})()


def test_internal_mcp_catalogue_builds_and_includes_relationship_tools():
    from src.backend.integrations.internal_mcp import build_default_catalogue

    catalogue = build_default_catalogue()
    methods = set(catalogue.list_methods())

    assert "add_relationship" in methods
    assert "get_doi_metadata" in methods
    assert "add_text_assertion_concept_links" in methods
    assert "store_text_assertion" in methods
    assert "upsert_scoped_assertion" in methods
    assert "retract_scoped_assertion" in methods
    assert "list_scoped_assertions" in methods
    assert "remove_relationship" in methods
    assert "preview_remove_relationship" in methods
    assert "remove_relationships_bulk" in methods
    assert "undo_relationship_removal" in methods
    assert "turn_execution_list" in methods
    assert "turn_execution_get" in methods
    assert "turn_execution_get_diagnostics" in methods
    assert "failure_case_intake_collect" in methods
    assert "failure_case_reference_resolve" in methods
    assert "turn_execution_get_critic_bundle" in methods
    assert "turn_execution_search_failures" in methods
    assert "turn_execution_build_benchmark" in methods
    assert "turn_execution_build_context_answering_benchmark" in methods
    assert "turn_execution_build_selector_benchmark" in methods
    assert "turn_execution_build_dashboard" in methods
    assert "turn_execution_backfill_from_chat_history" in methods
    assert "turn_execution_namespace_coverage_report" in methods
    assert "workflow_build_prediction_envelope" in methods
    assert "workflow_validate_candidate" in methods
    assert "workflow_concept_parity_audit" in methods
    assert "skill_catalogue_list" in methods
    assert "skill_catalogue_sync" in methods
    assert "testing_theory_create_slice" in methods
    assert "testing_theory_import_canonical_context" in methods
    assert "testing_theory_assert_local_claims" in methods
    assert "testing_theory_compute_diff" in methods
    assert "testing_theory_rollback_local_writes" in methods
    assert "testing_theory_promote_validated_claims" in methods
    assert "testing_theory_gc_expired" in methods
    assert "experiment_create_spec" in methods
    assert "experiment_start_run" in methods
    assert "experiment_record_observation" in methods
    assert "experiment_compute_verdict" in methods
    assert "experiment_emit_learning_signal" in methods
    assert "experiment_execute_target_workflow" in methods
    assert "experiment_execute_regression_suite" in methods
    assert "experiment_run_list" in methods
    assert "experiment_run_get" in methods
    assert "episode_critique_build_benchmark" in methods
    assert "episode_critique_memory_list" in methods
    assert "episode_critique_memory_get" in methods
    assert "context_bundle_resolve_effective_context" in methods
    assert "context_bundle_assemble_context_dossier" in methods
    assert "context_bundle_update_report_revision" in methods
    assert "context_bundle_build_reconstructed_workspace" in methods
    assert "context_bundle_build_benchmark" in methods
    assert "repo_dossier_file_snapshot" in methods
    assert "repo_dossier_search" in methods
    assert "repo_dossier_workflow_definition_get" in methods
    assert "repo_dossier_prompt_definition_get" in methods
    assert "repo_dossier_git_metadata" in methods
    assert "materialise_paper_recommendations" in methods
    assert "testing_prepare_experiment_spec" in methods
    assert "testing_prepare_meeting_invitation_spec" in methods
    assert "testing_prepare_arxiv_paper_ingestion_fixture" in methods
    assert "testing_verify_arxiv_paper_ingestion_result" in methods
    assert "testing_cleanup_arxiv_paper_ingestion_artifacts" in methods
    assert {
        "message_send_direct",
        "message_get_direct",
        "message_list_direct",
    } <= methods


def test_default_catalogue_has_no_inactive_effect_admission_windows():
    from src.backend.integrations.internal_mcp import build_default_catalogue

    catalogue = build_default_catalogue()
    snapshot = catalogue.snapshot()

    assert catalogue.get("create_concepts").effect_admission_window_sec is None
    assert catalogue.get("upsert_text_relation").effect_admission_window_sec is None
    assert catalogue.get("retract_scoped_assertion").effect_admission_window_sec is None
    assert catalogue.get("add_relationship").effect_admission_window_sec is None
    assert snapshot["create_concepts"]["effect_admission_window_sec"] is None
    assert snapshot["upsert_text_relation"]["effect_admission_window_sec"] is None
    assert snapshot["add_relationship"]["effect_admission_window_sec"] is None


def test_remote_file_import_has_observed_advisory_headroom():
    from src.backend.integrations.internal_mcp import (
        InternalMCPTransport,
        build_default_catalogue,
    )

    definition = build_default_catalogue().get("import_url_file_copy")

    assert definition.advisory_timeout_sec is None
    assert definition.timeout_sec is None
    assert definition.hard_timeout_enabled is False
    assert definition.successful_duration_bootstrap_sec == pytest.approx(173.641)
    assert definition.resolved_advisory_timeout(
        InternalMCPTransport()
    ) == pytest.approx(173.641 * 1.25)
    assert definition.resolved_timeout(InternalMCPTransport()) is None


def test_paper_download_advisory_covers_observed_blob_rehydration():
    from src.backend.integrations.internal_mcp import (
        InternalMCPTransport,
        build_default_catalogue,
    )

    definition = build_default_catalogue().get("download_paper")

    assert definition.advisory_timeout_sec is None
    assert definition.timeout_sec is None
    assert definition.hard_timeout_enabled is False
    assert definition.successful_duration_bootstrap_sec == pytest.approx(68.321)
    assert definition.resolved_advisory_timeout(
        InternalMCPTransport()
    ) == pytest.approx(68.321 * 1.25)
    assert definition.resolved_timeout(InternalMCPTransport()) is None


def test_default_catalogue_has_no_elapsed_time_hard_boundaries():
    from src.backend.integrations.internal_mcp import build_default_catalogue

    snapshot = build_default_catalogue().snapshot()

    assert snapshot
    assert all(
        definition["hard_timeout_enabled"] is False for definition in snapshot.values()
    )


def test_adaptive_deadline_history_is_limited_to_observed_long_running_imports():
    from src.backend.integrations.internal_mcp import build_default_catalogue

    snapshot = build_default_catalogue().snapshot()

    assert {
        name
        for name, definition in snapshot.items()
        if definition["successful_duration_bootstrap_sec"] is not None
    } == {"download_paper", "import_url_file_copy"}


def test_ordinary_semantic_reads_use_advisory_only_transport_timing():
    from src.backend.integrations.internal_mcp import (
        InternalMCPTransport,
        build_default_catalogue,
    )

    catalogue = build_default_catalogue()
    transport = InternalMCPTransport()

    for method_name in (
        "fetch_concept",
        "find_relations_with_argument",
        "get_predicate_incidence",
        "get_related_concepts",
        "get_text_relations",
        "get_text_relations_summary",
        "rag_get_item",
        "rag_list_collections",
        "rag_list_indexed",
        "resolve_concept_by_name",
        "search_concepts",
        "search_concept_descriptions",
        "search_knowledge_base",
        "vontology_concept_search",
    ):
        definition = catalogue.get(method_name)
        assert definition.hard_timeout_enabled is False
        assert definition.resolved_timeout(transport) is None


def test_frequent_gmail_and_scoped_assertion_paths_use_advisory_only_timing():
    from src.backend.integrations.internal_mcp import (
        InternalMCPTransport,
        build_default_catalogue,
    )

    catalogue = build_default_catalogue()
    transport = InternalMCPTransport()
    expected_advisory_seconds = {
        "gmail_get_auth_config": 10.0,
        "gmail_list_messages": 20.0,
        "gmail_get_message": 20.0,
        "gmail_get_attachment": 20.0,
        "gmail_import_attachment": 20.0,
        "gmail_list_labels": 15.0,
        "upsert_scoped_assertion": 20.0,
        "add_text_assertion_concept_links": 20.0,
        "store_text_assertion": 20.0,
        "list_scoped_assertions": 20.0,
    }

    for method_name, advisory_seconds in expected_advisory_seconds.items():
        definition = catalogue.get(method_name)
        assert definition.timeout_sec is None
        assert definition.advisory_timeout_sec == advisory_seconds
        assert definition.hard_timeout_enabled is False
        assert definition.resolved_timeout(transport) is None
        assert definition.resolved_advisory_timeout(transport) == advisory_seconds


def test_workflow_execute_uses_its_bounded_await_without_an_outer_hard_timeout():
    from src.backend.integrations.internal_mcp import (
        InternalMCPTransport,
        build_default_catalogue,
    )

    definition = build_default_catalogue().get("workflow_execute")

    assert definition.advisory_timeout_sec == 75.0
    assert definition.hard_timeout_enabled is False
    assert definition.resolved_timeout(InternalMCPTransport()) is None


def test_workflow_instance_await_is_a_model_visible_soft_checkpoint():
    from src.backend.integrations.internal_mcp import (
        InternalMCPTransport,
        build_default_catalogue,
    )

    definition = build_default_catalogue().get("workflow_get_instance")

    assert definition.advisory_timeout_sec == 75.0
    assert definition.hard_timeout_enabled is False
    assert definition.resolved_timeout(InternalMCPTransport()) is None
    assert "await_terminal" in definition.input_schema.optional
    assert "workflow_data" in definition.output_schema.optional
    assert "exhaustive list or count" in definition.description
    assert "never cancels or retries" in definition.description


def test_ordinary_turn_read_projection_follows_capability_authority_metadata():
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )
    from src.backend.services.adaptive_turn_service import (
        ordinary_turn_capability_delegation,
    )

    catalogue = build_default_catalogue()
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
    )

    public_reads = set(
        ordinary_turn_capability_delegation(gateway, user_concept_id=None)
    )
    actor_reads = set(
        ordinary_turn_capability_delegation(
            gateway,
            user_concept_id="#V#ordinary_actor",
        )
    )
    actor_mail_reads = set(
        ordinary_turn_capability_delegation(
            gateway,
            user_concept_id="#V#ordinary_actor",
            trusted_argument_values={
                "gmail_profile": "represented-profile",
                "turn_namespace": "#V#ordinary_actor@ordinary_org",
                "actor_user_concept_id": "#V#ordinary_actor",
                "actor_organisation_concept_id": "#V#ordinary_org",
                "turn_id": "turn-1",
                "conversation_id": "conversation-1",
            },
        )
    )
    actor_without_org = set(
        ordinary_turn_capability_delegation(
            gateway,
            user_concept_id="#V#ordinary_actor",
            trusted_argument_values={
                "turn_namespace": "#V#ordinary_actor",
                "actor_user_concept_id": "#V#ordinary_actor",
                "actor_organisation_concept_id": None,
                "turn_id": "turn-no-org",
            },
        )
    )
    delegated_effects = {
        name for name in actor_mail_reads if catalogue.get(name).category == "write"
    }

    # A newly registered ordinary read does not need a second positive
    # allow-list entry. These representative unannotated capabilities are
    # inherited directly from the live catalogue for an authenticated actor.
    assert {
        "concept_exists",
        "fetch_concept",
        "find_relations_with_argument",
        "read_file_copy",
        "resolve_publication_scope_profile",
        "search_concepts",
        "task_list",
        "workflow_list_instances",
    } <= actor_reads
    assert public_reads <= actor_reads
    assert {
        "create_concepts",
        "add_text_assertion_concept_links",
        "list_scoped_assertions",
        "retract_scoped_assertion",
        "store_text_assertion",
        "upsert_scoped_assertion",
    } <= actor_without_org

    # Capabilities that can persist state remain write-category even when
    # their primary output is a report or ranking.
    assert catalogue.get("build_paper_recommendations").category == "write"
    assert catalogue.get("episode_critique_build_benchmark").category == "write"
    assert {
        "build_paper_recommendations",
        "episode_critique_build_benchmark",
    }.isdisjoint(actor_mail_reads)
    assert delegated_effects == {
        "create_concepts",
        "add_text_assertion_concept_links",
        "change_concept_publication_scope",
        "record_source_processing_marker",
        "preview_concept_publication_scope_change",
            "retract_scoped_assertion",
            "store_text_assertion",
            "upsert_scoped_assertion",
        "upsert_text_relation",
        "upsert_uncertain_relationship_assertion",
        "add_relationship",
        "gmail_import_attachment",
        "gmail_send_message",
        "message_send_direct",
        "conversation_manage",
        "task_create",
        "task_update_status",
        "task_add_comment",
    }
    assert not {
        name for name in public_reads if catalogue.get(name).category == "write"
    }
    assert {"conversation_list", "conversation_get", "conversation_manage"} <= (
        actor_mail_reads
    )

    message_send_definition = catalogue.get("message_send_direct")
    assert message_send_definition.ordinary_turn_effect is True
    assert message_send_definition.ordinary_turn_trusted_argument_bindings == {
        "acting_user_concept_id": "actor_user_concept_id",
        "organisation_concept_id": "actor_organisation_concept_id",
        "request_id": "turn_id",
        "namespace": "turn_namespace",
    }
    assert catalogue.get("message_get_direct").ordinary_turn_effect is False
    assert catalogue.get("message_list_direct").ordinary_turn_effect is False
    task_create_definition = catalogue.get("task_create")
    assert task_create_definition.ordinary_turn_effect is True
    assert task_create_definition.ordinary_turn_trusted_argument_bindings == {
        "assignee_id": "actor_user_concept_id",
        "assignee_concept_id": "actor_user_concept_id",
        "created_by_concept_id": "actor_user_concept_id",
        "organisation_concept_id": "actor_organisation_concept_id",
        "originating_session_id": "conversation_id",
        "namespace": "turn_namespace",
        "acting_user_concept_id": "actor_user_concept_id",
        "request_id": "turn_id",
    }
    task_status_definition = catalogue.get("task_update_status")
    assert task_status_definition.ordinary_turn_effect is True
    assert (
        task_status_definition.ordinary_turn_mutation_subject_argument
        == "task_concept_id"
    )
    assert task_status_definition.ordinary_turn_trusted_argument_bindings == {
        "namespace": "turn_namespace",
        "acting_user_concept_id": "actor_user_concept_id",
        "organisation_concept_id": "actor_organisation_concept_id",
    }
    task_comment_definition = catalogue.get("task_add_comment")
    assert task_comment_definition.ordinary_turn_effect is True
    assert (
        task_comment_definition.ordinary_turn_mutation_subject_argument
        == "task_concept_id"
    )
    assert task_comment_definition.ordinary_turn_trusted_argument_bindings == {
        "author_concept_id": "actor_user_concept_id",
        "namespace": "turn_namespace",
        "acting_user_concept_id": "actor_user_concept_id",
        "organisation_concept_id": "actor_organisation_concept_id",
        "request_id": "turn_id",
    }

    create_definition = catalogue.get("create_concepts")
    assert create_definition.ordinary_turn_trusted_argument_bindings == {
        "namespace": "turn_namespace",
        "created_by_concept_id": "actor_user_concept_id",
    }
    assert create_definition.ordinary_turn_fixed_arguments == {
        "organisation_concept_id": None,
        "org_id": None,
        "visibility_scope_mode": None,
    }
    assert create_definition.input_schema.enum_values["scope_mode"] == [
        "user_only_default",
        "organisation_general",
        "global_general",
        None,
    ]
    marker_definition = catalogue.get("record_source_processing_marker")
    assert marker_definition.ordinary_turn_trusted_argument_bindings == {
        "namespace": "turn_namespace",
        "created_by_concept_id": "actor_user_concept_id",
    }
    assert marker_definition.ordinary_turn_fixed_arguments == {
        "organisation_concept_id": None,
    }
    assert marker_definition.ordinary_turn_effect is True
    assert catalogue.get("upsert_text_relation").ordinary_turn_fixed_arguments == {
        "provenance": None,
    }
    scoped_definition = catalogue.get("upsert_scoped_assertion")
    assert scoped_definition.ordinary_turn_trusted_argument_bindings == {
        "acting_user_concept_id": "actor_user_concept_id",
        "namespace": "turn_namespace",
        "turn_id": "turn_id",
    }
    assert scoped_definition.ordinary_turn_fixed_arguments == {
        "organisation_concept_id": None,
        "org_concept_id": None,
        "organisation_id": None,
        "org_id": None,
        "canonical_publication": False,
    }
    assert scoped_definition.input_schema.enum_values["scope_mode"] == [
        "user",
        "organisation",
        None,
    ]
    assert scoped_definition.ordinary_turn_mutation_subject_argument is None
    text_assertion_definition = catalogue.get("store_text_assertion")
    assert text_assertion_definition.ordinary_turn_trusted_argument_bindings == {
        "acting_user_concept_id": "actor_user_concept_id",
        "namespace": "turn_namespace",
        "turn_id": "turn_id",
    }
    assert text_assertion_definition.ordinary_turn_fixed_arguments == {
        "organisation_concept_id": None,
        "org_concept_id": None,
        "organisation_id": None,
        "org_id": None,
        "canonical_publication": False,
    }
    assert text_assertion_definition.ordinary_turn_effect is True
    assert text_assertion_definition.ordinary_turn_mutation_subject_argument is None
    link_definition = catalogue.get("add_text_assertion_concept_links")
    assert link_definition.ordinary_turn_trusted_argument_bindings == {
        "acting_user_concept_id": "actor_user_concept_id",
        "namespace": "turn_namespace",
        "turn_id": "turn_id",
    }
    assert link_definition.ordinary_turn_fixed_arguments == {
        "organisation_concept_id": None,
        "org_concept_id": None,
        "organisation_id": None,
        "org_id": None,
    }
    assert link_definition.ordinary_turn_effect is True
    assert link_definition.ordinary_turn_mutation_subject_argument is None
    uncertain_definition = catalogue.get("upsert_uncertain_relationship_assertion")
    assert uncertain_definition.ordinary_turn_effect is True
    assert uncertain_definition.ordinary_turn_mutation_subject_argument == "source_id"
    assert "possible duplicate" in uncertain_definition.description
    assert "instead of creating a new predicate" in uncertain_definition.description
    retract_definition = catalogue.get("retract_scoped_assertion")
    assert retract_definition.input_schema.required == {"assertion_id": str}
    assert retract_definition.input_schema.optional == {}
    assert retract_definition.ordinary_turn_trusted_argument_bindings == {
        "acting_user_concept_id": "actor_user_concept_id",
        "namespace": "turn_namespace",
    }
    assert retract_definition.ordinary_turn_fixed_arguments == {
        "organisation_concept_id": None,
        "org_concept_id": None,
        "organisation_id": None,
        "org_id": None,
    }
    list_definition = catalogue.get("list_scoped_assertions")
    assert list_definition.ordinary_turn_trusted_argument_bindings == {
        "acting_user_concept_id": "actor_user_concept_id",
    }
    assert list_definition.ordinary_turn_fixed_arguments == {
        "organisation_concept_id": None,
        "org_concept_id": None,
        "organisation_id": None,
        "org_id": None,
    }
    assert catalogue.get(
        "search_knowledge_base"
    ).ordinary_turn_trusted_argument_bindings == {
        "namespace": "turn_namespace",
        "user_concept_id": "actor_user_concept_id",
        "organisation_concept_id": "actor_organisation_concept_id",
    }
    for capability_name in (
        "rag_list_collections",
        "rag_list_indexed",
        "rag_get_item",
    ):
        assert catalogue.get(
            capability_name
        ).ordinary_turn_trusted_argument_bindings == {
            "namespace": "turn_namespace",
        }
    # Profile-scoped reads are absent until the entry point supplies the
    # actor-authorised constrained choice set. The model may select only from
    # those stable selectors; the server maps the selection to a runtime alias.
    gmail_reads = {
        "gmail_get_auth_config",
        "gmail_get_attachment",
        "gmail_get_message",
        "gmail_list_labels",
        "gmail_list_messages",
    }
    assert gmail_reads.isdisjoint(actor_reads)
    assert gmail_reads <= actor_mail_reads
    assert catalogue.get(
        "gmail_get_attachment"
    ).ordinary_turn_trusted_argument_bindings == {
        "namespace": "turn_namespace",
    }
    assert catalogue.get(
        "gmail_get_attachment"
    ).ordinary_turn_trusted_argument_choice_bindings == {
        "profile": "gmail_profile",
    }
    gmail_import_definition = catalogue.get("gmail_import_attachment")
    assert gmail_import_definition.category == "write"
    assert gmail_import_definition.ordinary_turn_effect is True
    assert gmail_import_definition.ordinary_turn_trusted_argument_bindings == {
        "namespace": "turn_namespace",
    }
    assert gmail_import_definition.ordinary_turn_trusted_argument_choice_bindings == {
        "profile": "gmail_profile",
    }

    gmail_send_definition = catalogue.get("gmail_send_message")
    assert gmail_send_definition.category == "write"
    assert gmail_send_definition.ordinary_turn_effect is True
    assert gmail_send_definition.ordinary_turn_trusted_argument_bindings == {
        "acting_user_concept_id": "actor_user_concept_id",
        "organisation_concept_id": "actor_organisation_concept_id",
        "request_id": "turn_id",
        "namespace": "turn_namespace",
    }
    assert gmail_send_definition.ordinary_turn_trusted_argument_choice_bindings == {
        "profile": "gmail_profile",
    }
    assert gmail_send_definition.ordinary_turn_fixed_arguments == {
        "allow_send": True,
    }
    assert gmail_send_definition.write_guardrail == {
        "ordinary_turn_explicit_request": True,
    }

    # Exact history recovery is constrained to the active conversation carrier.
    # The model cannot redirect the session, actor, namespace, or signed-ref path.
    assert "chat_history_get_segments" not in actor_reads
    assert "chat_history_get_segments" in actor_mail_reads
    history_definition = catalogue.get("chat_history_get_segments")
    assert history_definition.ordinary_turn_trusted_argument_bindings == {
        "session_id": "conversation_id",
        "namespace": "turn_namespace",
        "user_concept_id": "actor_user_concept_id",
        "organisation_concept_id": "actor_organisation_concept_id",
    }
    assert history_definition.ordinary_turn_fixed_arguments == {
        "conversation_ref": None,
        "include_debug": False,
    }

    from src.backend.services.adaptive_turn_service import _trusted_tool_payload

    history_payload = _trusted_tool_payload(
        gateway=gateway,
        tool_name="chat_history_get_segments",
        model_payload={
            "session_id": "foreign-conversation",
            "conversation_ref": {"session_id": "foreign-conversation"},
            "namespace": "#V#foreign@foreign_org",
            "user_concept_id": "#V#foreign",
            "organisation_concept_id": "#V#foreign_org",
            "include_debug": True,
            "segment_size": 10,
        },
        trusted_argument_values={
            "conversation_id": "conversation-1",
            "turn_namespace": "#V#ordinary_actor@ordinary_org",
            "actor_user_concept_id": "#V#ordinary_actor",
            "actor_organisation_concept_id": "#V#ordinary_org",
        },
    )
    assert history_payload == {
        "session_id": "conversation-1",
        "conversation_ref": None,
        "namespace": "#V#ordinary_actor@ordinary_org",
        "user_concept_id": "#V#ordinary_actor",
        "organisation_concept_id": "#V#ordinary_org",
        "include_debug": False,
        "segment_size": 10,
    }

    # These are mechanism-level exclusions: they expose ambient deployment
    # accounts, host-local data, raw cross-namespace data, or operator state.
    assert {
        "chat_introspect",
        "coding_agent_mcp_access_profile",
        "failure_case_intake_collect",
        "failure_case_reference_resolve",
        "get_predicate_extent",
        "github_get_file_contents",
        "github_list_tools",
        "gmail_list_profiles",
        "jira_search",
        "linkedin_search_messages",
        "list_recent_screenshots",
        "mongo_cost_guardrails_report",
        "mongo_query_diagnostics_report",
        "repo_dossier_search",
        "skill_catalogue_list",
        "testing_theory_compute_diff",
        "turn_execution_get_diagnostics",
        "workflow_concept_parity_audit",
        "workflow_list_event_bindings",
        "workflow_materialisation_diagnostics",
    }.isdisjoint(actor_mail_reads)

    # A proven unsafe option narrows that option rather than excluding the
    # whole otherwise-readable capability.
    assert {
        "context_bundle_build_benchmark",
        "search_proxy_diagnostics",
        "turn_execution_build_context_answering_benchmark",
        "turn_execution_build_selector_benchmark",
    } <= actor_reads


def test_no_org_scoped_assertion_contract_is_visible_and_identity_bound() -> None:
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )
    from src.backend.services.adaptive_turn_service import (
        _capability_catalogue,
        _trusted_tool_payload,
        ordinary_turn_capability_delegation,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    trusted = {
        "turn_namespace": "#V#ordinary_actor",
        "actor_user_concept_id": "#V#ordinary_actor",
        "actor_organisation_concept_id": None,
        "turn_id": "turn-no-org",
    }
    delegated = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#ordinary_actor",
        trusted_argument_values=trusted,
    )
    assert "upsert_scoped_assertion" in delegated
    capability = _capability_catalogue(
        gateway,
        delegated,
        {"names": ["upsert_scoped_assertion"]},
        trusted_argument_values=trusted,
    )["capabilities"][0]
    assert capability["input_schema"]["properties"]["scope_mode"]["enum"] == [
        "user",
        "organisation",
        None,
    ]
    assert {
        "acting_user_concept_id",
        "organisation_concept_id",
        "org_concept_id",
        "organisation_id",
        "org_id",
        "namespace",
        "turn_id",
    }.isdisjoint(capability["input_schema"]["properties"])

    payload = _trusted_tool_payload(
        gateway=gateway,
        tool_name="upsert_scoped_assertion",
        model_payload={
            "subject_concept_id": "#V#subject",
            "predicate": "#V#related_to",
            "target_concept_id": "#V#target",
            "scope_mode": "user",
            "organisation_concept_id": "#V#spoofed_org",
            "org_id": "#V#spoofed_org_alias",
        },
        trusted_argument_values=trusted,
    )
    assert payload["acting_user_concept_id"] == "#V#ordinary_actor"
    assert payload["namespace"] == "#V#ordinary_actor"
    assert payload["organisation_concept_id"] is None
    assert payload["org_id"] is None
    assert payload["scope_mode"] == "user"


def test_actor_scoped_private_reads_reject_payload_only_identity(monkeypatch):
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module
    from src.backend.security import access_control

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    payload_identity = {
        "user_concept_id": "#V#claimed_user",
        "org_id": "#V#claimed_org",
        "namespace": "#V#claimed_user@claimed_org",
    }
    calls = {
        "chat_history_get_segments": {
            **payload_identity,
            "session_id": "claimed-session",
        },
        "chat_history_get_debug_entry": {
            **payload_identity,
            "session_id": "claimed-session",
            "history_index": 0,
        },
        "conversation_telemetry_get_locator": {
            **payload_identity,
            "session_id": "claimed-session",
        },
        "turn_execution_list": payload_identity,
        "turn_execution_get": {
            **payload_identity,
            "request_id": "claimed-turn",
        },
        "turn_execution_search_failures": payload_identity,
        "turn_execution_build_benchmark": payload_identity,
        "experiment_run_list": payload_identity,
        "experiment_run_get": {
            **payload_identity,
            "run_id": "claimed-run",
        },
        "episode_critique_memory_list": payload_identity,
        "episode_critique_memory_get": {
            **payload_identity,
            "memory_id": "claimed-memory",
        },
    }

    for method_name, arguments in calls.items():
        result = gateway.invoke(method_name, arguments).payload
        assert result["success"] is False
        assert result["error_code"] == "authenticated_actor_context_required"

    monkeypatch.setattr(
        catalogue_module,
        "_rag_list_indexed",
        lambda **_kwargs: {"success": True, "items": [], "count": 0},
    )
    with access_control.override_current_actor(
        user_concept_id="#V#authenticated_user",
        organisation_concept_id="#V#authenticated_org",
    ):
        authenticated = gateway.invoke(
            "turn_execution_list",
            payload_identity,
        ).payload

    assert authenticated["success"] is True


def test_failure_case_learning_reads_require_operator_provenance():
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    for method_name in (
        "failure_case_intake_collect",
        "failure_case_reference_resolve",
    ):
        result = gateway.invoke(method_name, {}).payload
        assert result["success"] is False
        assert result["error_code"] == "workflow_global_admin_authority_required"


def test_internal_mcp_mongo_diagnostics_exposes_query_stats_source():
    from src.backend.integrations.internal_mcp import build_default_catalogue
    from src.backend.integrations.internal_mcp.schemas import schema_to_json_schema

    method = build_default_catalogue().get("mongo_query_diagnostics_report")

    assert method.input_schema.enum_values["source"] == (
        "combined",
        "in_process",
        "profiler",
        "query_stats",
    )
    assert schema_to_json_schema(method.input_schema)["properties"]["source"][
        "enum"
    ] == ["combined", "in_process", "profiler", "query_stats"]


def test_internal_mcp_gmail_list_messages_accepts_max_results_aliases():
    from src.backend.integrations.internal_mcp import build_default_catalogue
    from src.backend.integrations.internal_mcp.schemas import (
        schema_to_json_schema,
        validate_payload,
    )

    catalogue = build_default_catalogue()
    method = catalogue.get("gmail_list_messages")

    ok, errors = validate_payload(
        method.input_schema,
        {"profile": "zhan-gmail", "query": "in:inbox", "max_results": 10},
    )
    assert ok, errors
    assert method.input_schema.enum_values["scope"] == (
        "whole_mailbox",
        "profile_default_view",
    )
    assert schema_to_json_schema(method.input_schema)["properties"]["scope"][
        "enum"
    ] == ["whole_mailbox", "profile_default_view"]

    invalid_ok, invalid_errors = validate_payload(
        method.input_schema,
        {"profile": "zhan-gmail", "scope": "received"},
    )
    assert invalid_ok is False
    assert any(
        "Optional field 'scope' expected one of" in error
        for error in invalid_errors
    )

    ok, errors = validate_payload(
        method.input_schema,
        {"profile": "zhan-gmail", "query": "in:inbox", "maxResults": 10},
    )
    assert ok, errors

    ok, errors = validate_payload(
        method.input_schema,
        {"profile": "zhan-gmail", "page_token": "page-2"},
    )
    assert ok, errors

    assert method.output_schema is not None
    assert "gmail_get_message" in (method.description or "")
    assert "message_id" in (method.description or "")
    list_output_description = method.output_schema.description or ""
    assert "gmail_get_message" in list_output_description
    assert "message_id" in list_output_description

    detail_method = catalogue.get("gmail_get_message")
    assert detail_method.output_schema is not None
    assert "gmail_list_messages" in (detail_method.description or "")
    assert "message_id" in (detail_method.description or "")
    detail_output_description = detail_method.output_schema.description or ""
    assert "sender" in detail_output_description
    assert "subject" in detail_output_description


def test_internal_mcp_gmail_list_profiles_registered_and_handler_returns_summaries(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.internal_mcp import (
        build_default_catalogue,
    )
    from src.backend.integrations.internal_mcp import (
        catalogue as catalogue_module,
    )

    catalogue = build_default_catalogue()
    method = catalogue.get("gmail_list_profiles")
    assert method is not None
    assert method.category == "read"
    assert (
        "authorised" in (method.description or "").lower()
        or "authorized" in (method.description or "").lower()
    )

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.list_profile_summaries",
        lambda: [
            {
                "profile_id": "zhan-gmail",
                "authorised_email": "zhanvonwitbrock@gmail.com",
            },
            {"profile_id": "vonwitbrock-gmail", "authorised_email": None},
        ],
    )

    payload = catalogue_module._gmail_list_profiles()
    assert payload["count"] == 2
    assert payload["profiles"][0]["profile_id"] == "zhan-gmail"
    assert payload["profiles"][0]["authorised_email"] == "zhanvonwitbrock@gmail.com"
    # Non-sensitive: only the two declared keys per row.
    for entry in payload["profiles"]:
        assert set(entry.keys()) == {"profile_id", "authorised_email"}


def test_internal_mcp_gmail_handlers_expose_detail_follow_up_contract(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    monkeypatch.setattr(
        catalogue_module,
        "_gmail_authorised_email",
        lambda profile_id: (
            "zhanvonwitbrock@gmail.com" if profile_id == "zhan-gmail" else None
        ),
    )

    def fake_list_messages(**kwargs):
        assert kwargs["profile_id"] == "zhan-gmail"
        return {
            "messages": [
                {"id": "msg-1", "threadId": "thread-1"},
                {"id": "msg-2", "threadId": "thread-2", "subject": "Listed"},
            ],
            "resultSizeEstimate": 2,
        }

    def fake_get_message(**kwargs):
        assert kwargs["profile_id"] == "zhan-gmail"
        assert kwargs["message_id"] == "msg-1"
        return {
            "id": "msg-1",
            "threadId": "thread-1",
            "labelIds": ["INBOX", "UNREAD"],
            "snippet": "Short preview",
            "raw": "top-level-raw-must-not-escape",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [
                    {"name": "From", "value": "Sender <sender@example.test>"},
                    {"name": "Subject", "value": "Subject line"},
                    {"name": "Date", "value": "Sat, 25 Apr 2026 09:00:00 +0000"},
                ],
                "parts": [
                    {
                        "partId": "0",
                        "mimeType": "text/plain",
                        "body": {"data": "body-base64-must-not-escape"},
                    },
                    {
                        "partId": "1",
                        "filename": "ticket.pdf",
                        "mimeType": "application/pdf",
                        "body": {
                            "attachmentId": "attachment-1",
                            "data": "attachment-base64-must-not-escape",
                            "size": 456,
                        },
                    },
                ],
            },
        }

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.list_messages",
        fake_list_messages,
    )
    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.get_message",
        fake_get_message,
    )

    list_payload = catalogue_module._gmail_list_messages(
        profile="zhan-gmail",
        query="in:inbox",
        max_results=2,
    )

    assert list_payload["messages"][0]["message_id"] == "msg-1"
    assert list_payload["authorised_email"] == "zhanvonwitbrock@gmail.com"
    follow_up = list_payload["_tool_follow_up"]
    assert follow_up["schema_version"] == "mcp_tool_follow_up.v1"
    assert follow_up["item_array_field"] == "messages"
    assert follow_up["required_when_any_item_missing_fields"] == [
        "sender",
        "subject",
        "date",
        "snippet",
    ]
    assert follow_up["follow_up_tools"][0]["tool"] == "gmail_get_message"
    assert follow_up["follow_up_tools"][0]["input_bindings"] == {
        "profile": {"source": "request", "field": "profile"},
        "message_id": {"source": "item", "field": "message_id"},
    }

    detail_payload = catalogue_module._gmail_get_message(
        profile="zhan-gmail",
        message_id="msg-1",
    )

    assert detail_payload["message_id"] == "msg-1"
    assert detail_payload["thread_id"] == "thread-1"
    assert detail_payload["label_ids"] == ["INBOX", "UNREAD"]
    assert detail_payload["profile"] == "zhan-gmail"
    assert detail_payload["authorised_email"] == "zhanvonwitbrock@gmail.com"
    assert detail_payload["sender"] == "Sender <sender@example.test>"
    assert detail_payload["from"] == "Sender <sender@example.test>"
    assert detail_payload["subject"] == "Subject line"
    assert detail_payload["date"] == "Sat, 25 Apr 2026 09:00:00 +0000"
    assert detail_payload["snippet"] == "Short preview"
    assert detail_payload["attachments"] == [
        {
            "attachment_id": "attachment-1",
            "filename": "ticket.pdf",
            "content_type": "application/pdf",
            "part_id": "1",
            "size_bytes": 456,
        }
    ]
    assert detail_payload["attachment_count"] == 1
    assert detail_payload["attachments_truncated"] is False
    assert "body" not in detail_payload
    assert "payload" not in detail_payload
    assert "raw" not in detail_payload
    assert "must-not-escape" not in repr(detail_payload)


def test_internal_mcp_gmail_get_message_includes_body_only_when_requested(
    monkeypatch,
    trusted_gmail_operator,
):
    import base64

    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    requested_formats: list[str] = []

    def fake_get_message(**kwargs):
        requested_formats.append(kwargs["format"])
        return {
            "id": "msg-body",
            "payload": {
                "mimeType": "text/plain",
                "body": {
                    "data": base64.urlsafe_b64encode(
                        b"Explicitly requested message body"
                    ).decode("ascii")
                },
                "headers": [],
            },
        }

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.get_message",
        fake_get_message,
    )

    metadata_payload = catalogue_module._gmail_get_message(
        profile="zhan-gmail",
        message_id="msg-body",
    )
    body_payload = catalogue_module._gmail_get_message(
        profile="zhan-gmail",
        message_id="msg-body",
        include_body=True,
        max_body_chars=12,
    )

    assert requested_formats == ["full", "full"]
    assert "body" not in metadata_payload
    assert body_payload["body"] == "Explicitly r"
    assert body_payload["body_truncated"] is True
    assert "payload" not in body_payload


def test_internal_mcp_gmail_get_message_reports_truncated_body_without_text(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.get_message",
        lambda **_kwargs: {
            "id": "msg-many-parts",
            "payload": {
                "mimeType": "multipart/mixed",
                "parts": [
                    {"mimeType": "application/octet-stream", "body": {}}
                    for _index in range(300)
                ],
            },
        },
    )

    payload = catalogue_module._gmail_get_message(
        profile="zhan-gmail",
        message_id="msg-many-parts",
        include_body=True,
    )

    assert "body" not in payload
    assert payload["body_truncated"] is True


def test_gmail_list_messages_surfaces_invalid_grant_as_reauthorisation(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    def fake_list_messages(**_kwargs):
        raise RuntimeError("invalid_grant: Token has been expired or revoked")

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.list_messages",
        fake_list_messages,
    )

    payload = catalogue_module._gmail_list_messages(profile="zhan-gmail")

    assert payload["success"] is False
    assert payload["error_code"] == "invalid_grant"
    assert payload["error_details"] == {
        "exception_type": "RuntimeError",
        "failure_kind": "authorisation",
        "reauthorisation_required": True,
    }


def test_gmail_get_message_keeps_transport_timeout_distinct_from_auth(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    def fake_get_message(**_kwargs):
        raise TimeoutError("connection attempt timed out")

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.get_message",
        fake_get_message,
    )

    payload = catalogue_module._gmail_get_message(
        profile="zhan-gmail",
        message_id="msg-timeout",
    )

    assert payload["success"] is False
    assert payload["error_code"] == "gmail_transport_timeout"
    assert payload["error_details"]["failure_kind"] == "transport_timeout"
    assert payload["error_details"]["reauthorisation_required"] is False


def test_gmail_get_message_surfaces_insufficient_oauth_scope(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    def fake_get_message(**_kwargs):
        raise _GmailHttpError("insufficientPermissions", 403)

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.get_message",
        fake_get_message,
    )

    payload = catalogue_module._gmail_get_message(
        profile="zhan-gmail",
        message_id="msg-scope",
    )

    assert payload["success"] is False
    assert payload["error_code"] == "insufficient_scopes"
    assert payload["error_details"]["http_status"] == 403
    assert payload["error_details"]["failure_kind"] == "authorisation"
    assert payload["error_details"]["reauthorisation_required"] is True


def test_gmail_list_messages_zero_results_exposes_empty_messages(
    monkeypatch,
    trusted_gmail_operator,
):
    from types import SimpleNamespace

    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    def fake_list_messages(**kwargs):
        assert kwargs["profile_id"] == "zhan-gmail"
        return {"resultSizeEstimate": 0}

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.list_messages",
        fake_list_messages,
    )
    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.get_profile",
        lambda _profile, *_args: SimpleNamespace(
            profile_id="zhan-gmail",
            query_prefix=None,
            label_filter=[],
        ),
    )

    payload = catalogue_module._gmail_list_messages(
        profile="zhan-gmail",
        query="from:(no-such-sender@example.invalid)",
        max_results=1,
    )

    assert payload["messages"] == []
    assert payload["resultSizeEstimate"] == 0
    assert payload["_tool_follow_up"]["item_array_field"] == "messages"


def test_gmail_list_messages_surfaces_effective_query_and_warns_on_prefix(
    monkeypatch,
    trusted_gmail_operator,
):
    """JVNAUTOSCI-2127: list response must surface profile-level filtering.

    When a profile defines ``query_prefix`` and the caller did not supply
    ``query``, the annotated payload must:
      * include ``effective_query`` describing the applied prefix and the
        composed query string actually sent to Gmail, and
      * include a ``notes`` warning that mailing-list/BCC/alias mail may be
        silently excluded.
    """

    from src.backend.integrations.google import gmail_service as gs
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    captured_kwargs: dict = {}

    def fake_list_messages(**kwargs):
        captured_kwargs.update(kwargs)
        return {"messages": [{"id": "msg-1"}], "resultSizeEstimate": 1}

    fake_profile = gs.GmailProfile(
        profile_id="vonwitbrock-gmail",
        token_path="/tmp/fake-token.json",
        label_filter=["INBOX"],
        query_prefix=("to:zhanvonwitbrock@gmail.com OR from:zhanvonwitbrock@gmail.com"),
    )

    monkeypatch.setattr(gs, "list_messages", fake_list_messages)
    monkeypatch.setattr(gs, "get_profile", lambda *_a, **_kw: fake_profile)

    payload = catalogue_module._gmail_list_messages(
        profile="vonwitbrock-gmail",
        page_token="gmail-page-2",
    )

    assert "effective_query" in payload
    eq = payload["effective_query"]
    assert eq["applied_query_prefix"] == fake_profile.query_prefix
    assert eq["applied_label_filter"] == ["INBOX"]
    assert eq["caller_query"] is None
    assert eq["effective_query_string"] == fake_profile.query_prefix
    assert eq["effective_label_ids"] == ["INBOX"]
    assert eq["bypass_profile_query_prefix"] is False
    assert eq["scope"] == "profile_default_view"
    assert eq["scope_source"] == "legacy_default"

    assert "notes" in payload
    assert any("query_prefix" in n for n in payload["notes"])

    # The handler did not pass bypass through unless asked.
    assert captured_kwargs.get("bypass_profile_query_prefix") is False
    assert captured_kwargs["page_token"] == "gmail-page-2"


def test_gmail_list_messages_reports_grouped_profile_and_caller_query(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.google import gmail_service as gs
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    captured_kwargs: dict = {}

    def fake_list_messages(**kwargs):
        captured_kwargs.update(kwargs)
        return {"messages": [], "resultSizeEstimate": 0}

    fake_profile = gs.GmailProfile(
        profile_id="vonwitbrock-gmail",
        token_path="/tmp/fake-token.json",
        query_prefix="to:owner@example.test OR from:owner@example.test",
    )
    monkeypatch.setattr(gs, "list_messages", fake_list_messages)
    monkeypatch.setattr(gs, "get_profile", lambda *_a, **_kw: fake_profile)

    payload = catalogue_module._gmail_list_messages(
        profile="vonwitbrock-gmail",
        query='newer_than:2d subject:"booking confirmation"',
        max_results=3,
    )

    assert captured_kwargs["query"] == 'newer_than:2d subject:"booking confirmation"'
    assert payload["effective_query"]["effective_query_string"] == (
        "(to:owner@example.test OR from:owner@example.test) "
        '(newer_than:2d subject:"booking confirmation")'
    )


def test_gmail_list_messages_bypass_profile_query_prefix_passes_through(
    monkeypatch,
    trusted_gmail_operator,
):
    """JVNAUTOSCI-2127: bypass flag forwards to gmail_service and clears prefix."""

    from src.backend.integrations.google import gmail_service as gs
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    captured_kwargs: dict = {}

    def fake_list_messages(**kwargs):
        captured_kwargs.update(kwargs)
        return {"messages": [], "resultSizeEstimate": 0}

    fake_profile = gs.GmailProfile(
        profile_id="vonwitbrock-gmail",
        token_path="/tmp/fake-token.json",
        label_filter=["INBOX"],
        query_prefix="to:zhanvonwitbrock@gmail.com",
    )

    monkeypatch.setattr(gs, "list_messages", fake_list_messages)
    monkeypatch.setattr(gs, "get_profile", lambda *_a, **_kw: fake_profile)

    payload = catalogue_module._gmail_list_messages(
        profile="vonwitbrock-gmail",
        bypass_profile_query_prefix=True,
    )

    assert captured_kwargs["bypass_profile_query_prefix"] is True

    eq = payload["effective_query"]
    assert eq["bypass_profile_query_prefix"] is True
    assert eq["applied_query_prefix"] is None
    assert eq["applied_label_filter"] is None
    assert eq["effective_query_string"] is None
    assert eq["scope"] == "whole_mailbox"
    assert eq["scope_source"] == "legacy_bypass_profile_query_prefix"
    # No filtering note when nothing was applied.
    assert payload.get("notes") in (None, [])


def test_gmail_list_messages_input_schema_accepts_bypass_flag():
    """JVNAUTOSCI-2127: bypass_profile_query_prefix is part of the input schema."""

    from src.backend.integrations.internal_mcp import build_default_catalogue
    from src.backend.integrations.internal_mcp.schemas import validate_payload

    catalogue = build_default_catalogue()
    method = catalogue.get("gmail_list_messages")

    ok, errors = validate_payload(
        method.input_schema,
        {"profile": "zhan-gmail", "bypass_profile_query_prefix": True},
    )
    assert ok, errors

    assert method.output_schema is not None
    output_description = method.output_schema.description or ""
    assert "effective_query" in output_description
    assert "include_metadata" in output_description
    assert "message bodies and MIME payloads are not returned" in output_description
    assert "metadata_error" in output_description

    input_description = method.input_schema.description or ""
    assert "adjacent clauses mean AND" in input_description
    assert "uppercase OR or braces express a union" in input_description
    assert "scope='whole_mailbox'" in input_description
    assert "scope='profile_default_view'" in input_description
    assert "order/order_by/sort remain compatibility hints" in input_description


def test_gmail_list_messages_input_schema_accepts_model_planning_hints():
    from src.backend.integrations.internal_mcp import build_default_catalogue
    from src.backend.integrations.internal_mcp.schemas import (
        normalise_payload_aliases,
        validate_payload,
    )

    catalogue = build_default_catalogue()
    method = catalogue.get("gmail_list_messages")
    payload = {
        "profile": "zhan-gmail",
        "limit": 10,
        "order_by": "newest",
        "order": "desc",
        "scope": "whole_mailbox",
        "include_metadata": ["id", "from", "subject", "date"],
    }

    normalise_payload_aliases(method.input_schema, payload)
    ok, errors = validate_payload(method.input_schema, payload)

    assert ok, errors
    assert payload["max_results"] == 10
    assert "limit" not in payload


def test_predicate_incidence_input_schema_accepts_target_type_alias():
    from src.backend.integrations.internal_mcp import build_default_catalogue
    from src.backend.integrations.internal_mcp.schemas import (
        normalise_payload_aliases,
        validate_payload,
    )

    catalogue = build_default_catalogue()
    method = catalogue.get("get_predicate_incidence")
    assert method.description.startswith(
        "Discover predicates actually used around a known anchor or type when "
        "relationship coverage is uncertain."
    )
    payload = {
        "target_type": "#V#scientific_publication",
        "relation_kind": "binary",
    }

    normalise_payload_aliases(method.input_schema, payload)
    ok, errors = validate_payload(method.input_schema, payload)

    assert ok, errors
    assert payload["instance_of"] == "#V#scientific_publication"
    assert "target_type" not in payload


def test_gmail_list_messages_handler_accepts_limit_alias_and_planning_hints(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.google import gmail_service as gs
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    captured_kwargs: dict = {}

    def fake_list_messages(**kwargs):
        captured_kwargs.update(kwargs)
        return {"messages": [], "resultSizeEstimate": 0}

    fake_profile = gs.GmailProfile(
        profile_id="zhan-gmail",
        token_path="/tmp/fake-token.json",
    )

    monkeypatch.setattr(gs, "list_messages", fake_list_messages)
    monkeypatch.setattr(gs, "get_profile", lambda *_a, **_kw: fake_profile)

    payload = catalogue_module._gmail_list_messages(
        profile="zhan-gmail",
        limit=10,
        order="desc",
        scope="whole_mailbox",
        include_metadata=["sender", "subject", "date", "snippet"],
    )

    assert payload["messages"] == []
    assert captured_kwargs["profile_id"] == "zhan-gmail"
    assert captured_kwargs["max_results"] == 10
    assert captured_kwargs["include_metadata"] == [
        "sender",
        "subject",
        "date",
        "snippet",
    ]
    assert captured_kwargs["bypass_profile_query_prefix"] is True
    assert payload["effective_query"]["scope"] == "whole_mailbox"
    assert payload["effective_query"]["scope_source"] == "explicit_scope"


def test_gmail_list_messages_profile_default_view_applies_profile_filters(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.google import gmail_service as gs
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    captured_kwargs: dict = {}

    def fake_list_messages(**kwargs):
        captured_kwargs.update(kwargs)
        return {"messages": [], "resultSizeEstimate": 0}

    fake_profile = gs.GmailProfile(
        profile_id="zhan-gmail",
        token_path="/tmp/fake-token.json",
        label_filter=["INBOX"],
        query_prefix="to:zhan@example.test OR from:zhan@example.test",
    )
    monkeypatch.setattr(gs, "list_messages", fake_list_messages)
    monkeypatch.setattr(gs, "get_profile", lambda *_a, **_kw: fake_profile)

    payload = catalogue_module._gmail_list_messages(
        profile="zhan-gmail",
        scope="profile_default_view",
    )

    assert captured_kwargs["bypass_profile_query_prefix"] is False
    effective_query = payload["effective_query"]
    assert effective_query["scope"] == "profile_default_view"
    assert effective_query["scope_source"] == "explicit_scope"
    assert effective_query["applied_query_prefix"] == fake_profile.query_prefix
    assert effective_query["applied_label_filter"] == ["INBOX"]


def test_gmail_list_messages_rejects_invalid_or_conflicting_explicit_scope(
    monkeypatch,
):
    from src.backend.integrations.google import gmail_service as gs
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    gmail_called = False

    def fake_list_messages(**_kwargs):
        nonlocal gmail_called
        gmail_called = True
        raise AssertionError("invalid mailbox scope must fail before Gmail access")

    monkeypatch.setattr(gs, "list_messages", fake_list_messages)

    invalid = catalogue_module._gmail_list_messages(
        profile="zhan-gmail",
        scope="received",
    )
    whole_mailbox_conflict = catalogue_module._gmail_list_messages(
        profile="zhan-gmail",
        scope="whole_mailbox",
        bypass_profile_query_prefix=False,
    )
    default_view_conflict = catalogue_module._gmail_list_messages(
        profile="zhan-gmail",
        scope="profile_default_view",
        bypass_profile_query_prefix=True,
    )

    assert invalid["error_code"] == "invalid_mailbox_scope"
    assert invalid["error_details"]["allowed_scopes"] == [
        "whole_mailbox",
        "profile_default_view",
    ]
    assert whole_mailbox_conflict["error_code"] == "conflicting_mailbox_scope"
    assert default_view_conflict["error_code"] == "conflicting_mailbox_scope"
    assert gmail_called is False


def test_ordinary_gmail_methods_use_actor_authorised_profile_choices():
    from src.backend.integrations.internal_mcp import build_default_catalogue

    catalogue = build_default_catalogue()
    profile_argument_by_method = {
        "gmail_get_auth_config": "profile_id",
        "gmail_list_messages": "profile",
        "gmail_get_message": "profile",
        "gmail_get_attachment": "profile",
        "gmail_import_attachment": "profile",
        "gmail_list_labels": "profile",
    }

    for method_name, profile_argument in profile_argument_by_method.items():
        definition = catalogue.get(method_name)
        assert definition.ordinary_turn_trusted_argument_choice_bindings == {
            profile_argument: "gmail_profile",
        }
        assert profile_argument not in (
            definition.ordinary_turn_trusted_argument_bindings or {}
        )

    profile_listing = catalogue.get("gmail_list_profiles")
    assert profile_listing.ordinary_turn_excluded_reason == "deployment_global_account"
    assert "excluded from ordinary" in (profile_listing.description or "")


def test_gmail_list_messages_projection_narrows_follow_up_fields(
    monkeypatch,
    trusted_gmail_operator,
):
    from types import SimpleNamespace

    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    def fake_list_messages(**_kwargs):
        return {
            "messages": [
                {
                    "id": "message-1",
                    "message_id": "message-1",
                    "threadId": "thread-1",
                    "thread_id": "thread-1",
                    "sender": "Airline <travel@example.test>",
                    "subject": "Booking confirmation",
                    "date": "Mon, 10 Aug 2026 18:00:00 +0000",
                }
            ],
            "metadata_projection": {
                "requested_fields": ["sender", "subject", "date"],
                "metadata_format": "metadata",
                "body_included": False,
                "attempted_count": 1,
                "succeeded_count": 1,
                "failed_count": 0,
            },
        }

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.list_messages",
        fake_list_messages,
    )
    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.get_profile",
        lambda _profile, *_args: SimpleNamespace(
            profile_id="zhan-gmail",
            query_prefix=None,
            label_filter=[],
        ),
    )

    payload = catalogue_module._gmail_list_messages(
        profile="zhan-gmail",
        max_results=1,
        include_metadata=["from", "subject", "date"],
    )

    follow_up = payload["_tool_follow_up"]
    assert follow_up["required_when_any_item_missing_fields"] == [
        "sender",
        "subject",
        "date",
    ]
    assert follow_up["available_via_follow_up_fields"] == [
        "sender",
        "subject",
        "date",
        "snippet",
    ]
    assert "snippet" not in follow_up["required_when_any_item_missing_fields"]


def test_gmail_list_messages_resolves_represented_profile_resource_alias(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.google import gmail_service as gs
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module
    from src.backend.services import concept_service

    captured_kwargs: dict = {}

    def fake_list_messages(**kwargs):
        captured_kwargs.update(kwargs)
        return {"messages": [], "resultSizeEstimate": 0}

    fake_profile = gs.GmailProfile(
        profile_id="zhan-gmail",
        token_path="/tmp/fake-token.json",
    )

    monkeypatch.setattr(gs, "list_messages", fake_list_messages)
    monkeypatch.setattr(gs, "get_profile", lambda *_a, **_kw: fake_profile)
    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id",
        lambda concept_id: {
            "concept_id": concept_id,
            "attributes": {"runtime_profile_alias": "zhan-gmail"},
        },
    )

    payload = catalogue_module._gmail_list_messages(
        profile="#V#gmail_profile_zhan_gmail",
        max_results=10,
    )

    assert payload["messages"] == []
    assert captured_kwargs["profile_id"] == "zhan-gmail"
    assert payload["profile"] == "zhan-gmail"


def test_gmail_modify_labels_resolves_represented_profile_resource_alias(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module
    from src.backend.services import concept_service

    captured_kwargs: dict = {}

    def fake_modify_labels(**kwargs):
        captured_kwargs.update(kwargs)
        return {
            "message_id": kwargs["message_id"],
            "profile": kwargs["profile_id"],
            "added": kwargs["add_labels"],
        }

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.modify_labels",
        fake_modify_labels,
    )
    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id",
        lambda concept_id: {
            "concept_id": concept_id,
            "attributes": {"runtime_profile_alias": "zhan-gmail"},
        },
    )

    payload = catalogue_module._gmail_modify_labels(
        profile="#V#gmail_profile_zhan_gmail",
        message_id="msg-1",
        add_labels=["Label_7"],
        allow_mutation=True,
        verify_after=True,
    )

    assert payload["profile"] == "zhan-gmail"
    assert captured_kwargs["profile_id"] == "zhan-gmail"
    assert captured_kwargs["message_id"] == "msg-1"
    assert captured_kwargs["add_labels"] == ["Label_7"]
    assert captured_kwargs["verify_after"] is True


def test_gmail_list_labels_supports_required_exact_name_resolution(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    captured_kwargs: dict = {}

    def fake_list_labels(**kwargs):
        captured_kwargs.update(kwargs)
        return {
            "label_id": "Label_3",
            "label_name": "VON/PAPER/REPRESENTED",
            "exact_match_count": 1,
        }

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.list_labels",
        fake_list_labels,
    )

    payload = catalogue_module._gmail_list_labels(
        profile="vonwitbrock-gmail",
        exact_name="VON/PAPER/REPRESENTED",
        require_exact_match=True,
    )

    assert payload["label_id"] == "Label_3"
    assert captured_kwargs["exact_name"] == "VON/PAPER/REPRESENTED"
    assert captured_kwargs["require_exact_match"] is True


def test_source_processing_marker_tools_registered_as_vontology_surfaces():
    from src.backend.integrations.internal_mcp import build_default_catalogue

    snapshot = build_default_catalogue().snapshot()

    assert snapshot["get_source_processing_marker"]["category"] == "read"
    assert snapshot["record_source_processing_marker"]["category"] == "write"
    assert snapshot["record_source_processing_marker"]["ordinary_turn_effect"] is True
    assert snapshot["get_source_processing_marker"]["input_schema"]["required"] == [
        "source_item_id",
        "source_system",
    ]
    assert (
        "source_profile"
        in snapshot["get_source_processing_marker"]["input_schema"]["optional"]
    )
    assert snapshot["record_source_processing_marker"]["input_schema"]["required"] == [
        "source_item_id",
        "source_profile",
        "source_system",
    ]
    assert (
        "historical profileless marker"
        in snapshot["get_source_processing_marker"]["input_schema"]["description"]
    )
    assert (
        "exact stable profile identifier"
        in snapshot["record_source_processing_marker"]["input_schema"]["description"]
    )
    assert (
        "do not omit, translate, canonicalise, or invent it"
        in snapshot["record_source_processing_marker"]["input_schema"]["description"]
    )
    assert snapshot["record_source_processing_marker"]["input_schema"]["aliases"] == {
        "profile": "source_profile",
        "profile_id": "source_profile",
    }
    assert (
        "represented_outputs"
        in snapshot["record_source_processing_marker"]["input_schema"]["optional"]
    )
    assert (
        "message_processing_marker"
        in snapshot["record_source_processing_marker"]["output_schema"]["optional"]
    )


def test_spreadsheet_record_plan_tools_are_bounded_internal_surfaces():
    from src.backend.integrations.internal_mcp import build_default_catalogue

    snapshot = build_default_catalogue().snapshot()

    assert snapshot["compile_spreadsheet_record_plan"]["category"] == "read"
    assert snapshot["compile_spreadsheet_record_plan"]["input_schema"]["required"] == [
        "plan",
        "spreadsheet",
        "write_authority_contract",
    ]
    assert {
        "user_concept_id",
        "organisation_concept_id",
    } <= set(snapshot["compile_spreadsheet_record_plan"]["input_schema"]["optional"])
    assert (
        snapshot["build_spreadsheet_record_materialisation_request"]["category"]
        == "read"
    )
    assert snapshot["compare_spreadsheet_record_batch"]["category"] == "read"
    assert (
        snapshot["build_spreadsheet_record_completion_evidence"]["category"] == "read"
    )
    assert snapshot["build_spreadsheet_batch_completion_evidence"]["category"] == "read"


def test_gmail_send_message_registered_and_gateway_invokes(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )

    captured: dict = {}

    def fake_send_message(**kwargs):
        captured.update(kwargs)
        return {
            "id": "sent-1",
            "message_id": "sent-1",
            "profile": kwargs["profile_id"],
            "to": [kwargs["to"]],
            "recipient_count": 1,
            "subject": kwargs["subject"],
        }

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.send_message",
        fake_send_message,
    )
    monkeypatch.setattr(
        "src.backend.services.mail_profile_resource_vontology_service."
        "mail_profile_resource_represents_identity",
        lambda **_kwargs: True,
    )

    catalogue = build_default_catalogue()
    method = catalogue.get("gmail_send_message")
    assert method.category == "write"
    assert method.output_schema is not None
    assert "allow_send" in (method.description or "")

    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
        trusted_actor_payload_fallback=True,
    )
    result = gateway.invoke(
        "gmail_send_message",
        {
            "profile": "zhan-gmail",
            "recipient": "witbrock@gmail.com",
            "subject": "Hi From Von",
            "body": "An interesting body.",
            "allow_send": True,
            "request_id": "turn-send-1",
        },
    ).payload

    assert result["message_id"] == "sent-1"
    assert captured["profile_id"] == "zhan-gmail"
    assert captured["to"] == ["witbrock@gmail.com"]
    assert captured["body_text"] == "An interesting body."
    assert captured["allow_send"] is True
    assert captured["request_id"] == "turn-send-1"
    assert (
        captured["profile_resource_concept_id"]
        == "#V#gmail_profile_zhan_gmail"
    )
    assert captured["audit_context"]["tool"] == "gmail_send_message"


def test_gmail_send_message_gateway_fails_closed_without_allow_send(monkeypatch):
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )

    called = False

    def fake_send_message(**kwargs):
        nonlocal called
        called = True
        return {"id": "sent-1"}

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.send_message",
        fake_send_message,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    result = gateway.invoke(
        "gmail_send_message",
        {
            "profile": "zhan-gmail",
            "to": "witbrock@gmail.com",
            "subject": "Hi From Von",
            "body_text": "An interesting body.",
            "allow_send": False,
            "request_id": "turn-send-denied-1",
        },
    ).payload

    assert result["success"] is False
    assert result["error_code"] == "send_not_allowed"
    assert called is False


def test_gmail_create_label_registered_and_gateway_invokes(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )

    captured: dict = {}

    def fake_create_label(**kwargs):
        captured.update(kwargs)
        return {
            "id": "Label_1",
            "label_id": "Label_1",
            "name": kwargs["name"],
            "profile": kwargs["profile_id"],
            "created": True,
        }

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.create_label",
        fake_create_label,
    )

    catalogue = build_default_catalogue()
    method = catalogue.get("gmail_create_label")
    assert method.category == "write"
    assert method.output_schema is not None
    assert "allow_mutation=true" in (method.description or "")

    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
        trusted_actor_payload_fallback=True,
    )
    result = gateway.invoke(
        "gmail_create_label",
        {
            "profile": "zhan-gmail",
            "label_name": "VON/PAPER",
            "label_list_visibility": "labelShow",
            "message_list_visibility": "show",
            "allow_mutation": True,
        },
    ).payload

    assert result["label_id"] == "Label_1"
    assert result["created"] is True
    assert captured["profile_id"] == "zhan-gmail"
    assert captured["name"] == "VON/PAPER"
    assert captured["label_list_visibility"] == "labelShow"
    assert captured["message_list_visibility"] == "show"
    assert captured["allow_mutation"] is True
    assert captured["audit_context"]["tool"] == "gmail_create_label"


def test_gmail_create_label_gateway_fails_closed_without_allow_mutation(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )

    called = False

    def fake_create_label(**kwargs):
        nonlocal called
        called = True
        return {"id": "Label_1"}

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.create_label",
        fake_create_label,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    result = gateway.invoke(
        "gmail_create_label",
        {
            "profile": "zhan-gmail",
            "name": "VON/PAPER",
            "allow_mutation": False,
        },
    ).payload

    assert result["success"] is False
    assert result["error_code"] == "mutation_not_allowed"
    assert called is False


def test_gmail_create_label_gateway_surfaces_conflict(
    monkeypatch,
    trusted_gmail_operator,
):
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )

    class _Resp:
        status = 409

    class _ConflictError(Exception):
        resp = _Resp()

    def fake_create_label(**kwargs):
        raise _ConflictError("Label already exists")

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.create_label",
        fake_create_label,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
        trusted_actor_payload_fallback=True,
    )
    result = gateway.invoke(
        "gmail_create_label",
        {
            "profile": "zhan-gmail",
            "name": "VON/PAPER",
            "allow_mutation": True,
        },
    ).payload

    assert result["success"] is False
    assert result["error_code"] == "gmail_label_already_exists"
    assert result["error_details"]["name"] == "VON/PAPER"


def test_gmail_service_send_message_builds_raw_mime_and_checks_scope(monkeypatch):
    import base64
    from email import message_from_bytes
    from email.policy import default as email_policy

    import mongomock
    import pytest

    from src.backend.integrations.google import gmail_service as gs
    from src.backend.services.settings_service import (
        GmailOutboundRateLimitSettings,
    )

    captured: dict = {}

    class _Req:
        def execute(self):
            return {"id": "gmail-sent-1", "threadId": "thread-1"}

    class _FakeMessages:
        def send(self, **kwargs):
            captured["send_kwargs"] = kwargs
            return _Req()

    class _FakeUsers:
        def messages(self):
            return _FakeMessages()

        def getProfile(self, **_kwargs):
            class _ProfileReq:
                def execute(self):
                    return {"emailAddress": "zhan@example.test"}

            return _ProfileReq()

    class _FakeService:
        def users(self):
            return _FakeUsers()

    send_profile = gs.GmailProfile(
        profile_id="zhan-gmail",
        token_path="/tmp/fake-token.json",
        scopes=[gs.SEND_SCOPE, *gs.DEFAULT_SCOPES],
    )
    read_profile = gs.GmailProfile(
        profile_id="read-only-gmail",
        token_path="/tmp/fake-token.json",
        scopes=list(gs.DEFAULT_SCOPES),
    )
    send_only_profile = gs.GmailProfile(
        profile_id="send-only-gmail",
        token_path="/tmp/fake-token.json",
        scopes=[gs.SEND_SCOPE],
    )
    profiles = {
        "zhan-gmail": send_profile,
        "read-only-gmail": read_profile,
        "send-only-gmail": send_only_profile,
    }

    monkeypatch.setattr(gs, "get_service", lambda *_a, **_kw: _FakeService())
    monkeypatch.setattr(gs, "_resolve_vontology_profile_scopes", lambda _profile: None)
    test_database = mongomock.MongoClient().von_test
    delivery_collection = test_database.gmail_outbound_deliveries
    delivery_collection.create_index("delivery_fingerprint", unique=True)

    payload = gs.send_message(
        profile_id="zhan-gmail",
        to="witbrock@gmail.com",
        subject="Hi From Von",
        body_text="Here is a small interesting thought.",
        allow_send=True,
        profiles=profiles,
        profile_resource_concept_id="#V#gmail_profile_zhan_gmail",
        request_id="test-gmail-service-send",
        outbound_rate_limit_settings=GmailOutboundRateLimitSettings(enabled=True),
        outbound_quota_collection=test_database.gmail_outbound_quota,
        outbound_delivery_collection=delivery_collection,
    )

    assert payload["message_id"] == "gmail-sent-1"
    assert payload["profile"] == "zhan-gmail"
    assert payload["recipient_count"] == 1

    raw_message = captured["send_kwargs"]["body"]["raw"]
    decoded = base64.urlsafe_b64decode(raw_message.encode("ascii"))
    message = message_from_bytes(decoded, policy=email_policy)
    assert message["To"] == "witbrock@gmail.com"
    assert message["Subject"] == "Hi From Von"
    assert message["From"] == "Von AI Agent <zhan@example.test>"
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == (
        "Here is a small interesting thought.\n\n"
        f"{gs.AI_AGENT_DISCLOSURE_TEXT}"
    )

    with pytest.raises(ValueError, match="allow_send"):
        gs.send_message(
            profile_id="zhan-gmail",
            to="witbrock@gmail.com",
            subject="Hi From Von",
            body_text="Body",
            allow_send=False,
            profiles=profiles,
        )

    with pytest.raises(PermissionError, match="send-capable"):
        gs.send_message(
            profile_id="read-only-gmail",
            to="witbrock@gmail.com",
            subject="Hi From Von",
            body_text="Body",
            allow_send=True,
            profiles=profiles,
        )

    with pytest.raises(PermissionError, match="read-back"):
        gs.send_message(
            profile_id="send-only-gmail",
            to="witbrock@gmail.com",
            subject="Hi From Von",
            body_text="Body",
            allow_send=True,
            profiles=profiles,
        )


def test_gmail_service_list_messages_bypass_skips_profile_prefix(monkeypatch):
    """JVNAUTOSCI-2127: gmail_service honours bypass_profile_query_prefix."""

    from src.backend.integrations.google import gmail_service as gs

    captured: dict = {}

    class _FakeMessages:
        def list(self, **kwargs):
            captured["list_kwargs"] = kwargs

            class _Req:
                def execute(self_inner):
                    return {"messages": []}

            return _Req()

    class _FakeUsers:
        def messages(self):
            return _FakeMessages()

    class _FakeService:
        def users(self):
            return _FakeUsers()

    fake_profile = gs.GmailProfile(
        profile_id="vonwitbrock-gmail",
        token_path="/tmp/fake-token.json",
        label_filter=["INBOX"],
        query_prefix="to:zhanvonwitbrock@gmail.com",
    )

    monkeypatch.setattr(gs, "get_profile", lambda *_a, **_kw: fake_profile)
    monkeypatch.setattr(gs, "get_service", lambda *_a, **_kw: _FakeService())

    # Without bypass, the profile prefix and label filter are applied.
    gs.list_messages(profile_id="vonwitbrock-gmail")
    assert captured["list_kwargs"]["q"] == fake_profile.query_prefix
    assert captured["list_kwargs"]["labelIds"] == ["INBOX"]

    # With bypass, neither is applied.
    captured.clear()
    gs.list_messages(
        profile_id="vonwitbrock-gmail",
        bypass_profile_query_prefix=True,
    )
    assert captured["list_kwargs"]["q"] is None
    assert captured["list_kwargs"]["labelIds"] is None
