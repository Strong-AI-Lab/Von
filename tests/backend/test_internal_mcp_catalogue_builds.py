def test_internal_mcp_catalogue_builds_and_includes_relationship_tools():
    from src.backend.integrations.internal_mcp import build_default_catalogue

    catalogue = build_default_catalogue()
    methods = set(catalogue.list_methods())

    assert "add_relationship" in methods
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


def test_default_catalogue_exposes_calibrated_effect_admission_windows():
    from src.backend.integrations.internal_mcp import build_default_catalogue

    catalogue = build_default_catalogue()
    snapshot = catalogue.snapshot()

    assert catalogue.get("create_concepts").effect_admission_window_sec is None
    assert catalogue.get("upsert_text_relation").effect_admission_window_sec == 8.0
    assert catalogue.get("add_relationship").effect_admission_window_sec == 5.0
    assert snapshot["create_concepts"]["effect_admission_window_sec"] is None
    assert snapshot["upsert_text_relation"]["effect_admission_window_sec"] == 8.0
    assert snapshot["add_relationship"]["effect_admission_window_sec"] == 5.0


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
            },
        )
    )
    delegated_effects = {
        name
        for name in actor_mail_reads
        if catalogue.get(name).category == "write"
    }

    # A newly registered ordinary read does not need a second positive
    # allow-list entry. These representative unannotated capabilities are
    # inherited directly from the live catalogue for an authenticated actor.
    assert {
        "concept_exists",
        "fetch_concept",
        "find_relations_with_argument",
        "read_file_copy",
        "search_concepts",
        "task_list",
        "workflow_list_instances",
    } <= actor_reads
    assert public_reads <= actor_reads

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
        "upsert_text_relation",
        "add_relationship",
    }
    assert not {
        name for name in public_reads if catalogue.get(name).category == "write"
    }

    create_definition = catalogue.get("create_concepts")
    assert create_definition.ordinary_turn_trusted_argument_bindings == {
        "namespace": "turn_namespace",
        "created_by_concept_id": "actor_user_concept_id",
    }
    assert create_definition.ordinary_turn_fixed_arguments == {
        "organisation_concept_id": None,
        "org_id": None,
        "scope_mode": "user_org_default",
        "visibility_scope_mode": None,
    }
    assert catalogue.get("upsert_text_relation").ordinary_turn_fixed_arguments == {
        "provenance": None,
    }

    # Server-bound resources are absent until the entry point supplies the
    # actor's represented binding; the model never selects the profile.
    gmail_reads = {
        "gmail_get_auth_config",
        "gmail_get_attachment",
        "gmail_get_message",
        "gmail_list_labels",
        "gmail_list_messages",
    }
    assert gmail_reads.isdisjoint(actor_reads)
    assert gmail_reads <= actor_mail_reads

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
        "linkedin_get_messages",
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
        assert (
            result["error_code"]
            == "workflow_global_admin_authority_required"
        )


def test_internal_mcp_gmail_list_messages_accepts_max_results_aliases():
    from src.backend.integrations.internal_mcp import build_default_catalogue
    from src.backend.integrations.internal_mcp.schemas import validate_payload

    catalogue = build_default_catalogue()
    method = catalogue.get("gmail_list_messages")

    ok, errors = validate_payload(
        method.input_schema,
        {"profile": "zhan-gmail", "query": "in:inbox", "max_results": 10},
    )
    assert ok, errors

    ok, errors = validate_payload(
        method.input_schema,
        {"profile": "zhan-gmail", "query": "in:inbox", "maxResults": 10},
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
):
    from src.backend.integrations.internal_mcp import (
        build_default_catalogue,
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


def test_internal_mcp_gmail_handlers_expose_detail_follow_up_contract(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

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
            "snippet": "Short preview",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Sender <sender@example.test>"},
                    {"name": "Subject", "value": "Subject line"},
                    {"name": "Date", "value": "Sat, 25 Apr 2026 09:00:00 +0000"},
                ]
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
    assert detail_payload["sender"] == "Sender <sender@example.test>"
    assert detail_payload["from"] == "Sender <sender@example.test>"
    assert detail_payload["subject"] == "Subject line"
    assert detail_payload["date"] == "Sat, 25 Apr 2026 09:00:00 +0000"
    assert detail_payload["snippet"] == "Short preview"
    assert "body" not in detail_payload


def test_internal_mcp_gmail_get_message_includes_body_only_when_requested(
    monkeypatch,
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
    )

    assert requested_formats == ["metadata", "full"]
    assert "body" not in metadata_payload
    assert body_payload["body"] == "Explicitly requested message body"
    assert body_payload["body_truncated"] is False


def test_gmail_list_messages_zero_results_exposes_empty_messages(monkeypatch):
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
        lambda _profile: SimpleNamespace(query_prefix=None, label_filter=[]),
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

    payload = catalogue_module._gmail_list_messages(profile="vonwitbrock-gmail")

    assert "effective_query" in payload
    eq = payload["effective_query"]
    assert eq["applied_query_prefix"] == fake_profile.query_prefix
    assert eq["applied_label_filter"] == ["INBOX"]
    assert eq["caller_query"] is None
    assert eq["effective_query_string"] == fake_profile.query_prefix
    assert eq["effective_label_ids"] == ["INBOX"]
    assert eq["bypass_profile_query_prefix"] is False

    assert "notes" in payload
    assert any("query_prefix" in n for n in payload["notes"])

    # The handler did not pass bypass through unless asked.
    assert captured_kwargs.get("bypass_profile_query_prefix") is False


def test_gmail_list_messages_bypass_profile_query_prefix_passes_through(
    monkeypatch,
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
        "scope": "received",
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
        scope="received",
    )

    assert payload["messages"] == []
    assert captured_kwargs["profile_id"] == "zhan-gmail"
    assert captured_kwargs["max_results"] == 10


def test_gmail_list_messages_resolves_represented_profile_resource_alias(monkeypatch):
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


def test_gmail_modify_labels_resolves_represented_profile_resource_alias(monkeypatch):
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
    )

    assert payload["profile"] == "zhan-gmail"
    assert captured_kwargs["profile_id"] == "zhan-gmail"
    assert captured_kwargs["message_id"] == "msg-1"
    assert captured_kwargs["add_labels"] == ["Label_7"]


def test_source_processing_marker_tools_registered_as_vontology_surfaces():
    from src.backend.integrations.internal_mcp import build_default_catalogue

    snapshot = build_default_catalogue().snapshot()

    assert snapshot["get_source_processing_marker"]["category"] == "read"
    assert snapshot["record_source_processing_marker"]["category"] == "write"
    assert snapshot["get_source_processing_marker"]["input_schema"]["required"] == [
        "source_item_id",
        "source_system",
    ]
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


def test_gmail_send_message_registered_and_gateway_invokes(monkeypatch):
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

    catalogue = build_default_catalogue()
    method = catalogue.get("gmail_send_message")
    assert method.category == "write"
    assert method.output_schema is not None
    assert "allow_send" in (method.description or "")

    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
    )
    result = gateway.invoke(
        "gmail_send_message",
        {
            "profile": "zhan-gmail",
            "recipient": "witbrock@gmail.com",
            "subject": "Hi From Von",
            "body": "An interesting body.",
            "allow_send": True,
        },
    ).payload

    assert result["message_id"] == "sent-1"
    assert captured["profile_id"] == "zhan-gmail"
    assert captured["to"] == ["witbrock@gmail.com"]
    assert captured["body_text"] == "An interesting body."
    assert captured["allow_send"] is True
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
        },
    ).payload

    assert result["success"] is False
    assert result["error_code"] == "send_not_allowed"
    assert called is False


def test_gmail_create_label_registered_and_gateway_invokes(monkeypatch):
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


def test_gmail_create_label_gateway_surfaces_conflict(monkeypatch):
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

    import pytest

    from src.backend.integrations.google import gmail_service as gs

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

    class _FakeService:
        def users(self):
            return _FakeUsers()

    send_profile = gs.GmailProfile(
        profile_id="zhan-gmail",
        token_path="/tmp/fake-token.json",
        scopes=[gs.SEND_SCOPE],
    )
    read_profile = gs.GmailProfile(
        profile_id="read-only-gmail",
        token_path="/tmp/fake-token.json",
        scopes=list(gs.DEFAULT_SCOPES),
    )
    profiles = {
        "zhan-gmail": send_profile,
        "read-only-gmail": read_profile,
    }

    monkeypatch.setattr(gs, "get_service", lambda *_a, **_kw: _FakeService())

    payload = gs.send_message(
        profile_id="zhan-gmail",
        to="witbrock@gmail.com",
        subject="Hi From Von",
        body_text="Here is a small interesting thought.",
        allow_send=True,
        profiles=profiles,
    )

    assert payload["message_id"] == "gmail-sent-1"
    assert payload["profile"] == "zhan-gmail"
    assert payload["recipient_count"] == 1

    raw_message = captured["send_kwargs"]["body"]["raw"]
    decoded = base64.urlsafe_b64decode(raw_message.encode("ascii"))
    message = message_from_bytes(decoded, policy=email_policy)
    assert message["To"] == "witbrock@gmail.com"
    assert message["Subject"] == "Hi From Von"
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == (
        "Here is a small interesting thought."
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
