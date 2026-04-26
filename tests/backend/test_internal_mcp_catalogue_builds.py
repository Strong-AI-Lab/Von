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
    assert "authorised" in (method.description or "").lower() or "authorized" in (
        method.description or ""
    ).lower()

    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.list_profile_summaries",
        lambda: [
            {"profile_id": "zhan-gmail", "authorised_email": "zhanvonwitbrock@gmail.com"},
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
        query_prefix=(
            "to:zhanvonwitbrock@gmail.com OR from:zhanvonwitbrock@gmail.com"
        ),
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
