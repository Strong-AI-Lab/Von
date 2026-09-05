# Backend test drift diagnosis — 5 September 2026

## Scope and evidence

Source: [4 September full backend run](https://github.com/Strong-AI-Lab/Von/actions/runs/33902795573), commit `edf6f2b7be67610a760fed7e0ecb4d9704d81e77`: 134 failures, of which 30 were outside the 106-entry backlog; two backlog entries passed.

Replayed those exact 30 node IDs on code based on `5b2f8b976069` using the project PDM environment (macOS, Python 3.14), with the workflow's deliberately unavailable Mongo endpoints and invalid external-service credentials. Result: **17 failed, 13 passed in 135.83 seconds**. These are selected-test results, not a Linux/full-suite recovery claim. No production database, live model calls, or runtime deployment was used.

A matched control supplied only an isolated mongomock database (`VON_USE_MOCK_DB=1`, `VON_DB_NAME=drift_diagnosis_isolated`) for the access-control, session-organisation, multi-window, and model-registry cases: **13 passed in 1.10 seconds**, including all 11 that failed in the no-database replay. The same 13 cases consumed 121.839 seconds of test time in that replay.

## Findings

1. **Eleven failures are missing database fixtures, not demonstrated production defects.** Ten access/session/multi-window cases now require durable window bindings or ontology mutation coordination receipts, but their older fixtures leave these on real Mongo access while the CI environment supplies no database. The model-registry test mocks legacy graph getters; the newer batch loader first reads the actual concept collection and raises before reaching the mocked compatibility loader. The isolated mock database resolves all eleven without a production-code change. Keep the faithful durable behaviour; repair fixture coverage and isolation.

2. **Two failures expose a retrieval-state loss in production support code.** In `catalogue._search_knowledge_base`, the filtering loop replaces a `RAGQueryResults` list subclass with a plain list, then attempts to read `retrieval_state` from that replacement. A backend that supplies state on the result but has no `get_last_retrieval_state` method loses both valid-empty and embedding-mismatch semantics. A real backend exposing the secondary getter can recover state, so this does not establish that every deployed retrieval is affected. Preserve the original typed state before filtering, then retain the existing visibility-aware adjustment.

3. **Three history failures are strict expectations that omit added metadata.** The differences are additional provenance/focal-concept fields and lightweight concept-Q&A projection keys. The supplied history and optional situation remain present. Update expected contracts while retaining the assertions that history is not loaded unnecessarily or overwritten.

4. **One seed-authority failure is an omitted bootstrap allowlist entry.** `academic_roster_workflow_vontology_service.py` is explicitly bootstrap support and is called as a startup bootstrap function; its repo seed reference is not evidence of a runtime workflow choosing repo text over represented authority. Add this support module to the appropriate checked allowlist after retaining the bootstrap/runtime distinction.

5. **Thirteen original failures pass in the selected replay; that is not evidence they are fixed in the full suite.** Running the service-export eviction test before previously passing cases reproduced failures in scope-coordination contention, chat-session restoration, and temperature handling. That baseline control was interrupted after 239.50 seconds because module eviction also bypassed the intended mocks and caused repeated unavailable-database work; it was not a completed suite. The original temperature, membership-recovery, and mocked task failures are consistent with service-module identity pollution rather than provider behaviour.

## Repairs and delivery evidence

The user subsequently authorised carrying out the repairs in an isolated worktree. The retrieval tool now captures attempt-specific state before converting/filtering its list. A neighbouring test verifies that private login-identity rows stay filtered and that a mutable getter describing another request cannot override the current result's state.

The service-export check now runs in a fresh subprocess, leaving collected test modules and mock targets intact. Session test modules use an opt-in fresh mock database and reset/restore window bindings. The legacy model-registry test explicitly selects its mocked compatibility loader and isolates its snapshot caches. History assertions include the added metadata while retaining exact history/projection checks. The academic-roster bootstrap support path is included in the seed-authority allowlist; no production authority logic changed.

- Exact 30 reported cases with the service-export test deliberately first: **31 passed in 14.14 seconds** in the no-database CI-style environment.
- Impacted and neighbouring retrieval/privacy, history, registry, session, durable-recovery, seed-authority, and mail-profile files: **125 passed in 10.67 seconds** in the same environment.
- Ruff comparison against the base: no new findings; unrelated existing lint debt is preserved.
- No full backend suite has been rerun, so these results do not prune the historical failure backlog or establish that all original full-suite interactions have been removed.

The cadence change was delivered separately as [PR 557](https://github.com/Strong-AI-Lab/Von/pull/557), merged at `7b6df954a804b13625f0ec0b6ecede6c419be1e4`. Sixteen local targeted checks and a [successful Linux diagnostic workflow](https://github.com/Strong-AI-Lab/Von/actions/runs/33976707819) support the configuration change. Real scheduled notification delivery remains to be observed. The accepted-failure backlog has not been changed by this task.

Other active worktrees modify different catalogue functions; this repair is confined to `_search_knowledge_base`. The primary checkout's documentation and backlog changes were preserved. Jira read access returned `authenticated_actor_context_required`; this evidence is retained here instead of claiming a Jira update. No production runtime deployment or live model evaluation was performed.

## Exact selected replay results

| No-database result | Test node ID |
|---|---|
| Fail | `tests/backend/test_access_control_identity_resolution.py::test_window_session_organisation_context_controls_visibility` |
| Fail | `tests/backend/test_chat_history_conversation_situation.py::test_malformed_optional_situation_does_not_hide_history_or_get_overwritten` |
| Fail | `tests/backend/test_chat_history_conversation_situation.py::test_session_state_can_read_carrier_metadata_without_loading_history` |
| Fail | `tests/backend/test_chat_history_conversation_situation.py::test_session_state_returns_history_and_optional_situation_without_changing_history_contract` |
| Pass | `tests/backend/test_mail_profile_resource_vontology_service.py::test_bootstrap_materialises_mail_profile_resource_vocabulary` |
| Fail | `tests/backend/test_model_registry_service.py::test_get_model_registry_snapshot_prefers_graph_and_exposes_constraints` |
| Fail | `tests/backend/test_rag_retrieval_state.py::test_search_knowledge_base_does_not_report_index_mismatch_as_success` |
| Fail | `tests/backend/test_rag_retrieval_state.py::test_search_knowledge_base_preserves_valid_empty_as_success` |
| Fail | `tests/backend/test_repo_seed_authority_guardrails.py::test_repo_seed_authority_is_restricted_to_seed_support_paths` |
| Fail | `tests/backend/test_session_org_routes.py::test_legacy_session_user_id_owns_durable_window_binding_canonically` |
| Fail | `tests/backend/test_session_org_routes.py::test_set_organisation_rejects_non_member_without_changing_session` |
| Pass | `tests/backend/test_session_org_routes.py::test_set_organisation_reports_scope_coordination_contention_as_retryable` |
| Fail | `tests/backend/test_session_org_routes.py::test_set_organisation_updates_session_and_namespace` |
| Fail | `tests/backend/test_session_org_routes.py::test_set_user_concept_preserves_window_scoped_org_namespace` |
| Pass | `tests/backend/test_set_chat_session_endpoint.py::test_set_chat_session_projects_terminal_concept_q_and_a_state` |
| Pass | `tests/backend/test_structured_tool_calling_client.py::TestTemperatureGuards::test_openai_provider_omits_temperature_for_gpt5_mini` |
| Pass | `tests/backend/test_structured_tool_calling_client.py::TestTemperatureGuards::test_resolve_safe_temperature_for_model[gpt-5-mini-0.7-None]` |
| Pass | `tests/backend/test_structured_tool_calling_client.py::TestTemperatureGuards::test_resolve_safe_temperature_for_model[gpt-5-mini-2026-03-01-0.7-None]` |
| Pass | `tests/backend/test_task_management_service.py::TestTaskParityDatesAndEpic::test_update_task_fields_rejects_destination_without_membership` |
| Pass | `tests/backend/test_window_session_context_persistence.py::test_old_tab_recovers_from_exact_owned_conversation_metadata` |
| Pass | `tests/backend/test_window_session_context_persistence.py::test_recovery_cannot_rebind_between_membership_invalidation_and_commit` |
| Pass | `tests/backend/test_window_session_context_persistence.py::test_restart_preserves_two_org_tabs_and_authoritative_personal_tab` |
| Pass | `tests/backend/test_window_session_context_persistence.py::test_restart_recovers_org_and_revalidates_current_role` |
| Pass | `tests/backend/test_window_session_context_persistence.py::test_restart_recovery_fails_closed_and_deletes_revoked_membership` |
| Pass | `tests/backend/test_window_session_multi_org_isolation.py::TestCreateChatSessionUsesWindowContext::test_create_session_keeps_explicit_personal_window_context` |
| Fail | `tests/backend/test_window_session_multi_org_isolation.py::TestWindowSessionContextIsolation::test_two_windows_can_have_different_orgs` |
| Fail | `tests/backend/test_window_session_multi_org_isolation.py::TestWindowSessionStoreIsolation::test_get_effective_context_prefers_window_session` |
| Fail | `tests/backend/test_window_session_multi_org_isolation.py::TestWindowSessionStoreIsolation::test_get_effective_context_treats_another_users_window_as_unknown` |
| Fail | `tests/backend/test_window_session_multi_org_isolation.py::test_get_effective_context_ignores_partial_window_scope_in_compatibility_mode` |
| Fail | `tests/backend/test_window_session_multi_org_isolation.py::test_get_effective_context_unknown_window_falls_back_in_compatibility_mode` |
