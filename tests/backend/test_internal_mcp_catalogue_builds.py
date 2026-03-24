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
    assert "turn_execution_search_failures" in methods
    assert "turn_execution_build_benchmark" in methods
    assert "turn_execution_backfill_from_chat_history" in methods
    assert "turn_execution_namespace_coverage_report" in methods
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
    assert "testing_prepare_experiment_spec" in methods
    assert "testing_prepare_meeting_invitation_spec" in methods


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
