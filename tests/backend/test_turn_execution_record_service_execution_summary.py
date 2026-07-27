import json
from types import SimpleNamespace

import pytest

from src.backend.services.turn_decision_attribution_service import (
    DECISION_KINDS,
    TURN_DECISION_ATTRIBUTION_SCHEMA_VERSION,
)
from src.backend.services.turn_execution_record_service import (
    TURN_EXECUTION_CORRECTNESS_SCHEMA_VERSION,
    append_late_effect_observation,
    build_turn_execution_correctness_summary,
    build_turn_execution_record,
    build_workflow_routing_diagnostics,
    get_turn_execution_record_projection,
    project_final_answer_tool_evidence,
    record_effect_observation_phase,
    upsert_turn_execution_record_projection,
    _classify_tool_invocation_status,
    _normalise_projection_field_entries,
    _summarise_tool_execution_context,
)
from src.backend.services.required_tool_obligation_service import (
    BLOCKER_CONTRACT_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY,
    BLOCKER_REQUIRED_TOOL_ATTEMPT_FAILED,
    BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED,
    BLOCKER_TOOL_BUDGET_EXHAUSTED_BEFORE_REQUIRED_TOOLS,
    build_required_tool_obligation_ledger,
)

_KR_REQUIRED_TOOLS = [
    "search_concepts",
    "create_concepts",
    "add_relationship",
    "upsert_singleton_text_relation",
    "fetch_concept",
    "get_text_relations_summary",
]


def _build_effect_projection_record(
    request_id: str,
    tool_invocations: list[dict],
    **overrides,
):
    return build_turn_execution_record(
        request_id=request_id,
        session_id=f"session-{request_id}",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Continue this bounded turn.",
        response_text="The bounded turn completed.",
        interaction_timestamp_utc="2026-07-27T00:00:00Z",
        tool_invocations=tool_invocations,
        **overrides,
    )


def test_explicit_error_with_argument_payload_is_not_classified_as_success() -> None:
    assert (
        _classify_tool_invocation_status(
            invocation={
                "tool": "create_concepts",
                "status": "error",
                "payload": {
                    "name": "create_concepts",
                    "arguments": {"concepts": [{"name": "Unstarted"}]},
                },
            }
        )
        == "error"
    )


def test_not_started_durable_effect_blocks_completion_with_exact_receipt() -> None:
    invocation = {
        "tool": "create_concepts",
        "status": "error",
        "payload": {
            "name": "create_concepts",
            "arguments": {"concepts": [{"name": "Unstarted"}]},
        },
        "effective_arguments": {"concepts": [{"name": "Unstarted"}]},
        "effect_id": "effect_not_started_1",
        "effect_status": "failed",
        "changed": False,
        "mutation_outcome": "not_started",
        "outcome_finality": "terminal_for_turn",
        "error_code": "insufficient_effect_window",
        "transport": {
            "schema_version": "internal_mcp_transport.v1",
            "outcome": "not_started",
            "timeout_phase": "pre_dispatch",
        },
        "evidence": {"preview": "private bounded receipt detail"},
    }

    record = _build_effect_projection_record(
        "req-not-started-effect",
        [invocation],
    )

    serialised = record["execution"]["tool_invocations"][0]
    assert serialised["status"] == "not_started"
    assert serialised["mutation_outcome"] == "not_started"
    assert serialised["outcome_finality"] == "terminal_for_turn"
    assert serialised["error_code"] == "insufficient_effect_window"
    assert "effective_payload" not in serialised
    assert "evidence" not in serialised
    effect = next(
        item
        for item in record["required_effects"]
        if item["effect_id"] == "effect_not_started_1"
    )
    assert effect["status"] == "not_executed"
    assert effect["failure_codes"] == ["insufficient_effect_window"]
    assert record["execution"]["summary"]["successful_invocation_count"] == 0
    assert record["completion_gate"]["decision"] == "escalation_required"
    assert record["completion_gate"]["safe_to_claim_completion"] is False
    assert record["execution_correctness"]["failure_mode"] == "mutation_not_executed"


def test_distinct_same_tool_effect_failure_is_not_masked_by_success() -> None:
    record = _build_effect_projection_record(
        "req-distinct-effects",
        [
            {
                "tool": "create_concepts",
                "status": "ok",
                "payload": {
                    "name": "create_concepts",
                    "arguments": {"concepts": [{"name": "Created"}]},
                },
                "effect_id": "effect_created",
                "effect_status": "succeeded",
                "changed": True,
            },
            {
                "tool": "create_concepts",
                "status": "error",
                "payload": {
                    "name": "create_concepts",
                    "arguments": {"concepts": [{"name": "Unstarted"}]},
                },
                "effect_id": "effect_unstarted",
                "effect_status": "failed",
                "changed": False,
                "mutation_outcome": "not_started",
                "outcome_finality": "terminal_for_turn",
                "error_code": "insufficient_effect_batch_window",
                "transport": {"outcome": "not_started"},
            },
        ],
    )

    effects = {
        item["effect_id"]: item["status"]
        for item in record["required_effects"]
        if item["effect_id"] in {"effect_created", "effect_unstarted"}
    }
    assert effects == {
        "effect_created": "satisfied",
        "effect_unstarted": "not_executed",
    }
    assert record["completion_gate"]["safe_to_claim_completion"] is False


@pytest.mark.parametrize(
    ("invocation_status", "effect_status", "mutation_outcome"),
    [
        ("ok", "partial", "partial"),
        ("error", "indeterminate", "unknown"),
    ],
)
def test_partial_and_indeterminate_effects_remain_unsafe(
    invocation_status: str,
    effect_status: str,
    mutation_outcome: str,
) -> None:
    effect_id = f"effect_{effect_status}"
    record = _build_effect_projection_record(
        f"req-{effect_status}-effect",
        [
            {
                "tool": "create_concepts",
                "status": invocation_status,
                "payload": {
                    "name": "create_concepts",
                    "arguments": {"concepts": [{"name": "Incomplete"}]},
                },
                "effect_id": effect_id,
                "effect_status": effect_status,
                "changed": True if effect_status == "partial" else None,
                "mutation_outcome": mutation_outcome,
                "outcome_finality": "terminal_for_turn",
            }
        ],
    )

    serialised = record["execution"]["tool_invocations"][0]
    assert serialised["status"] == effect_status
    projected_effect = next(
        item for item in record["required_effects"] if item["effect_id"] == effect_id
    )
    assert projected_effect["status"] == "not_satisfied"
    assert record["completion_gate"]["safe_to_claim_completion"] is False
    assert record["completion_gate"]["repeat_eligible"] is False
    assert effect_id in record["completion_gate"]["evidence_payload"][
        "repeat_ineligible_effect_ids"
    ]


def test_successful_effect_with_canonical_readback_remains_completable() -> None:
    record = _build_effect_projection_record(
        "req-successful-effect-readback",
        [
            {
                "tool": "create_concepts",
                "status": "ok",
                "payload": {
                    "name": "create_concepts",
                    "arguments": {"concepts": [{"name": "Created"}]},
                },
                "effect_id": "effect_created_and_read",
                "effect_status": "succeeded",
                "changed": True,
                "result_target_ids": ["#V#created"],
            },
            {
                "tool": "fetch_concept",
                "status": "ok",
                "effective_arguments": {"concept_id": "#V#created"},
                "effective_payload": {
                    "success": True,
                    "concept_id": "#V#created",
                },
            },
        ],
    )

    effect = next(
        item
        for item in record["required_effects"]
        if item["effect_id"] == "effect_created_and_read"
    )
    assert effect["status"] == "satisfied"
    assert record["postcondition_checks"][0]["status"] == "verified"
    assert (
        record["postcondition_checks"][0]["verification_mode"]
        == "state_requery_correlated"
    )
    assert record["completion_gate"]["safe_to_claim_completion"] is True


def test_unrelated_verification_read_does_not_verify_effect() -> None:
    record = _build_effect_projection_record(
        "req-unrelated-readback",
        [
            {
                "tool": "create_concepts",
                "status": "ok",
                "effect_id": "effect_created",
                "effect_status": "succeeded",
                "changed": True,
                "result_target_ids": ["#V#created"],
            },
            {
                "tool": "fetch_concept",
                "status": "ok",
                "effective_arguments": {"concept_id": "#V#unrelated"},
                "effective_payload": {
                    "success": True,
                    "concept_id": "#V#unrelated",
                },
            },
        ],
    )

    check = next(
        item
        for item in record["postcondition_checks"]
        if item["effect_id"] == "effect_created"
    )
    assert check["status"] == "inconclusive"
    assert check["verification_mode"] == "state_requery_target_mismatch"
    assert check["observed"]["required_targets"] == ["#V#created"]
    assert check["observed"]["observed_verification_targets"] == ["#V#unrelated"]
    assert record["completion_gate"]["safe_to_claim_completion"] is False


def test_relationship_readback_correlates_source_target_and_predicate() -> None:
    record = _build_effect_projection_record(
        "req-relationship-readback",
        [
            {
                "tool": "add_relationship",
                "status": "ok",
                "effect_id": "effect_relationship",
                "effect_status": "succeeded",
                "changed": True,
                "effective_arguments": {
                    "source_id": "#V#source",
                    "target_id": "#V#target",
                    "predicate": "#V#related_to",
                },
            },
            {
                "tool": "find_relations_with_argument",
                "status": "ok",
                "effective_arguments": {
                    "source_id": "#V#source",
                    "target_id": "#V#target",
                    "predicate": "#V#related_to",
                },
                "effective_payload": {
                    "success": True,
                    "hits": [
                        {
                            "source_concept_id": "#V#source",
                            "target_concept_id": "#V#target",
                            "predicate_concept_id": "#V#related_to",
                        }
                    ],
                },
            },
        ],
    )

    effect = next(
        item
        for item in record["required_effects"]
        if item["effect_id"] == "effect_relationship"
    )
    assert effect["targets"] == ["#V#source", "#V#target"]
    assert effect["required_predicates"] == ["#V#related_to"]
    assert effect["required_relation_tuples"] == [
        {
            "source_id": "#V#source",
            "predicate_id": "#V#related_to",
            "target_id": "#V#target",
        }
    ]
    check = next(
        item
        for item in record["postcondition_checks"]
        if item["effect_id"] == "effect_relationship"
    )
    assert check["status"] == "verified"
    assert check["verification_mode"] == "state_requery_correlated"
    assert record["completion_gate"]["safe_to_claim_completion"] is True


def test_relationship_readback_does_not_join_fields_across_distinct_rows() -> None:
    record = _build_effect_projection_record(
        "req-relationship-split-readback",
        [
            {
                "tool": "add_relationship",
                "status": "ok",
                "effect_id": "effect_relationship_split",
                "effect_status": "succeeded",
                "changed": True,
                "effective_arguments": {
                    "source_id": "#V#source",
                    "target_id": "#V#target",
                    "predicate": "#V#related_to",
                },
            },
            {
                "tool": "find_relations_with_argument",
                "status": "ok",
                "effective_arguments": {
                    "source_id": "#V#source",
                    "target_id": "#V#target",
                    "predicate": "#V#related_to",
                },
                "effective_payload": {
                    "success": True,
                    "hits": [
                        {
                            "source_concept_id": "#V#source",
                            "target_concept_id": "#V#other_target",
                            "predicate_concept_id": "#V#related_to",
                        },
                        {
                            "source_concept_id": "#V#source",
                            "target_concept_id": "#V#target",
                            "predicate_concept_id": "#V#other_predicate",
                        },
                    ],
                },
            },
        ],
    )

    check = next(
        item
        for item in record["postcondition_checks"]
        if item["effect_id"] == "effect_relationship_split"
    )
    assert check["status"] == "inconclusive"
    assert check["verification_mode"] == "state_requery_target_mismatch"
    assert check["observed"]["observed_verification_relation_tuples"] == [
        {
            "source_id": "#V#source",
            "predicate_id": "#V#related_to",
            "target_id": "#V#other_target",
        },
        {
            "source_id": "#V#source",
            "predicate_id": "#V#other_predicate",
            "target_id": "#V#target",
        },
    ]
    assert record["completion_gate"]["safe_to_claim_completion"] is False


def test_relationship_readback_accepts_exact_tuple_among_distractor_rows() -> None:
    record = _build_effect_projection_record(
        "req-relationship-exact-readback",
        [
            {
                "tool": "add_relationship",
                "status": "ok",
                "effect_id": "effect_relationship_exact",
                "effect_status": "succeeded",
                "changed": True,
                "effective_arguments": {
                    "source_id": "#V#source",
                    "target_id": "#V#target",
                    "predicate": "#V#related_to",
                },
            },
            {
                "tool": "find_relations_with_argument",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "hits": [
                        {
                            "source_concept_id": "#V#source",
                            "target_concept_id": "#V#other_target",
                            "predicate_concept_id": "#V#related_to",
                        },
                        {
                            "source_concept_id": "#V#source",
                            "target_concept_id": "#V#target",
                            "predicate_concept_id": "#V#related_to",
                        },
                    ],
                },
            },
        ],
    )

    check = next(
        item
        for item in record["postcondition_checks"]
        if item["effect_id"] == "effect_relationship_exact"
    )
    assert check["status"] == "verified"
    assert check["verification_mode"] == "state_requery_correlated"
    assert check["observed"]["correlated_verification_tools"] == [
        "find_relations_with_argument"
    ]
    assert record["completion_gate"]["safe_to_claim_completion"] is True


def test_stable_effect_receipts_remain_visible_with_workflow_contract() -> None:
    record = _build_effect_projection_record(
        "req-workflow-and-stable-effects",
        [
            {
                "tool": "create_concepts",
                "status": "ok",
                "effect_id": "effect_created",
                "effect_status": "succeeded",
                "changed": True,
                "result_target_ids": ["#V#created"],
            },
            {
                "tool": "create_concepts",
                "status": "error",
                "effect_id": "effect_unstarted",
                "effect_status": "failed",
                "changed": False,
                "mutation_outcome": "not_started",
                "outcome_finality": "terminal_for_turn",
                "error_code": "insufficient_effect_window",
                "transport": {"outcome": "not_started"},
            },
            {
                "tool": "fetch_concept",
                "status": "ok",
                "effective_arguments": {"concept_id": "#V#created"},
                "effective_payload": {
                    "success": True,
                    "concept_id": "#V#created",
                },
            },
        ],
        selected_workflow_trace={
            "workflow_required_effects_contract": {
                "schema_version": "workflow_required_effects_contract.v1",
                "contract_id": "bounded-readback",
                "required_effects": [
                    {
                        "effect_id": "workflow_readback",
                        "effect_type": "tool_execution",
                        "required_tools": ["fetch_concept"],
                    }
                ],
            }
        },
    )

    effects = {
        item["effect_id"]: item["status"]
        for item in record["required_effects"]
        if item["effect_id"]
        in {"workflow_readback", "effect_created", "effect_unstarted"}
    }
    assert effects == {
        "workflow_readback": "satisfied",
        "effect_created": "satisfied",
        "effect_unstarted": "not_executed",
    }
    assert record["completion_gate"]["safe_to_claim_completion"] is False


def test_timeout_transport_metadata_survives_durable_turn_record_readback(
    monkeypatch,
) -> None:
    class _Collection:
        document = None

        def update_one(self, _query, update, **_kwargs):
            self.document = dict(update["$set"])
            return SimpleNamespace(modified_count=0, matched_count=0, upserted_id="1")

        def find_one(self, query, **_kwargs):
            if self.document and self.document.get("request_id") == query.get(
                "request_id"
            ):
                return dict(self.document)
            return None

    collection = _Collection()
    import src.backend.services.turn_execution_record_service as record_service

    monkeypatch.setattr(
        record_service,
        "get_turn_execution_records_collection",
        lambda: collection,
    )
    transport_metadata = {
        "schema_version": "internal_mcp_transport.v1",
        "execution_id": "mcp_timeout_readback",
        "outcome": "timed_out",
        "duration_ms": 40.5,
        "timeout_sec": 0.04,
        "advisory_timeout_sec": 0.01,
        "advisory_budget_exceeded": True,
        "queue_duration_ms": 1.5,
        "handler_duration_ms": None,
        "handler_elapsed_ms": 38.5,
        "transport_overhead_ms": 0.5,
        "timeout_phase": "handler",
        "late_result_policy": "discard_from_turn",
    }
    record = build_turn_execution_record(
        request_id="req-timeout-readback",
        session_id="session-timeout-readback",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Use the represented workflow to inspect the target.",
        response_text="The bounded read timed out.",
        interaction_timestamp_utc="2026-07-18T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#synthetic_retrieval_workflow",
            "verdict": "rag_selected",
        },
        tool_invocations=[
            {
                "tool": "synthetic_grounded_read",
                "status": "timeout",
                "error": "Hard deadline exceeded.",
                "error_code": "tool_timeout",
                "duration_ms": 40.5,
                "execution_id": "mcp_timeout_readback",
                "queue_duration_ms": 1.5,
                "handler_duration_ms": None,
                "handler_elapsed_ms": 38.5,
                "transport_overhead_ms": 0.5,
                "timeout_sec": 0.04,
                "advisory_timeout_sec": 0.01,
                "advisory_budget_exceeded": True,
                "timeout_phase": "handler",
                "transport": transport_metadata,
                "effective_payload": {
                    "success": False,
                    "status": "timed_out",
                    "error_code": "tool_timeout",
                },
            }
        ],
    )

    outcome = upsert_turn_execution_record_projection(
        record=record,
        user_id="#V#user",
        session_id="session-timeout-readback",
        namespace="#V#user@org",
        org_id="#V#org",
    )
    readback = get_turn_execution_record_projection(
        request_id="req-timeout-readback",
        namespace="#V#user@org",
    )

    assert outcome["updated"] is True
    assert readback is not None
    invocation = readback["execution"]["tool_invocations"][0]
    assert invocation["status"] == "timeout"
    assert invocation["error_code"] == "tool_timeout"
    assert invocation["execution_id"] == "mcp_timeout_readback"
    assert invocation["queue_duration_ms"] == 1.5
    assert invocation["handler_elapsed_ms"] == 38.5
    assert invocation["transport_overhead_ms"] == 0.5
    assert invocation["transport"] == transport_metadata


def test_late_effect_observation_is_bounded_idempotent_and_survives_full_upsert(
    monkeypatch,
) -> None:
    class _Collection:
        document = None

        def update_one(self, query, update, **_kwargs):
            existed = self.document is not None
            if not existed and not _kwargs.get("upsert"):
                return SimpleNamespace(
                    modified_count=0,
                    matched_count=0,
                    upserted_id=None,
                )
            if existed and query.get("request_id") != self.document.get("request_id"):
                return SimpleNamespace(
                    modified_count=0,
                    matched_count=0,
                    upserted_id=None,
                )
            late_filter = query.get("late_effect_observations")
            if existed and isinstance(late_filter, dict):
                observation_id = (
                    (late_filter.get("$not") or {})
                    .get("$elemMatch", {})
                    .get("observation_id")
                )
                if any(
                    item.get("observation_id") == observation_id
                    for item in self.document.get("late_effect_observations", [])
                    if isinstance(item, dict)
                ):
                    return SimpleNamespace(
                        modified_count=0,
                        matched_count=0,
                        upserted_id=None,
                    )
            if self.document is None:
                self.document = dict(update.get("$setOnInsert") or {})
            modified = False
            if "$set" in update:
                for key, value in update["$set"].items():
                    if self.document.get(key) != value:
                        modified = True
                    self.document[key] = value
            for key, value in (update.get("$addToSet") or {}).items():
                values = self.document.setdefault(key, [])
                if value not in values:
                    values.append(value)
                    modified = True
            for key, value in (update.get("$push") or {}).items():
                self.document.setdefault(key, []).append(value)
                modified = True
            for key, value in (update.get("$max") or {}).items():
                current = self.document.get(key)
                if current is None or current < value:
                    self.document[key] = value
                    modified = True
            return SimpleNamespace(
                modified_count=1 if modified and existed else 0,
                matched_count=1 if existed else 0,
                upserted_id=(
                    None
                    if existed or not _kwargs.get("upsert")
                    else "late-observation-1"
                ),
            )

        def find_one(self, query, **_kwargs):
            if not self.document:
                return None
            for field_name, expected in query.items():
                if self.document.get(field_name) != expected:
                    return None
            return dict(self.document)

    collection = _Collection()
    import src.backend.services.turn_execution_record_service as record_service

    monkeypatch.setattr(
        record_service,
        "get_turn_execution_records_collection",
        lambda: collection,
    )
    late_observation = {
        "schema_version": "internal_mcp_late_completion.v1",
        "execution_id": "mcp_late_effect_1",
        "method_name": "synthetic_effect",
        "category": "write",
        "outcome": "late_success",
        "observed_at_utc": "2026-07-27T10:00:00Z",
        "queue_duration_ms": 1.0,
        "handler_duration_ms": 22_000.0,
        "payload": {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "created_ids": ["#V#late_created"],
            "api_key": "must-not-be-persisted",
            "large_detail": "x" * 20_000,
        },
        "payload_truncated": False,
        "payload_redacted": False,
        "error_type": None,
        "error": None,
    }

    first = append_late_effect_observation(
        request_id="req-late-effect",
        effect_id="effect-late-1",
        execution_id="mcp_late_effect_1",
        observation=late_observation,
        user_id="#V#user",
        session_id="session-late-effect",
        namespace="#V#user@org",
        org_id="#V#org",
    )
    duplicate = append_late_effect_observation(
        request_id="req-late-effect",
        effect_id="effect-late-1",
        execution_id="mcp_late_effect_1",
        observation={
            **late_observation,
            "observed_at_utc": "2026-07-27T10:00:02Z",
            "payload": {
                **late_observation["payload"],
                "changed": False,
            },
        },
        user_id="#V#user",
        session_id="session-late-effect",
        namespace="#V#user@org",
        org_id="#V#org",
    )
    assert first["appended"] is True
    assert duplicate["appended"] is False
    assert duplicate["duplicate"] is True
    assert collection.document is not None
    assert len(collection.document["late_effect_observations"]) == 1
    stored_observation = collection.document["late_effect_observations"][0]
    assert stored_observation["effect_id"] == "effect-late-1"
    assert stored_observation["execution_id"] == "mcp_late_effect_1"
    assert stored_observation["payload"]["effect_status"] == "succeeded"
    assert stored_observation["payload"]["api_key"] == "[redacted]"
    assert len(stored_observation["payload"]["large_detail"]) == 2_003
    assert len(json.dumps(stored_observation)) < 64_000
    assert "source_observation_sha256" not in stored_observation
    assert "storage_truncated" not in stored_observation
    assert stored_observation["storage_transformed"] is True
    assert collection.document["late_effect_updated_at_utc"] == (
        collection.document["updated_at_utc"]
    )

    full_record = build_turn_execution_record(
        request_id="req-late-effect",
        session_id="session-late-effect",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Apply the bounded effect.",
        response_text="The turn later completed.",
        interaction_timestamp_utc="2026-07-27T10:00:01Z",
        tool_invocations=[
            {
                "tool": "synthetic_effect",
                "status": "timeout",
                "effect_id": "effect-late-1",
                "effect_status": "indeterminate",
                "changed": False,
                "execution_id": "mcp_late_effect_1",
                "effective_payload": {
                    "success": False,
                    "error_code": "tool_timeout_outcome_unknown",
                },
            }
        ],
    )
    # Even an accidentally stale whole-turn snapshot must not replace the
    # independently appended observation.
    full_record["late_effect_observations"] = []
    upsert_turn_execution_record_projection(
        record=full_record,
        user_id="#V#user",
        session_id="session-late-effect",
        namespace="#V#user@org",
        org_id="#V#org",
    )
    readback = get_turn_execution_record_projection(
        request_id="req-late-effect",
        namespace="#V#user@org",
    )

    assert readback is not None
    assert len(readback["late_effect_observations"]) == 1
    assert (
        readback["late_effect_observations"][0]["observation_id"]
        == first["observation_id"]
    )
    invocation = readback["execution"]["tool_invocations"][0]
    assert invocation["effect_status"] == "indeterminate"
    assert invocation["changed"] is False
    assert (
        readback["late_effect_observations"][0]["payload"]["effect_status"]
        == "succeeded"
    )
    assert readback["late_effect_observations"][0]["payload"]["changed"] is True


def test_effect_observation_journal_is_actor_scoped_idempotent_and_survives_upsert(
    monkeypatch,
) -> None:
    import mongomock
    import src.backend.services.turn_execution_record_service as record_service

    collection = mongomock.MongoClient().von_test.turn_execution_records
    collection.create_index("request_id", unique=True)
    monkeypatch.setattr(
        record_service,
        "get_turn_execution_records_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        record_service,
        "_turn_execution_mongo_comment",
        lambda *_args, **_kwargs: None,
    )
    scope = {
        "user_id": "#V#user",
        "namespace": "#V#user@org",
        "org_id": "#V#org",
    }

    dispatch = record_effect_observation_phase(
        request_id="req-effect-journal",
        effect_id="effect_abc123",
        phase="dispatch_intent",
        observation={
            "call_id": "call-1",
            "capability_name": "upsert_text_relation",
            "dispatch_state": "intent_recorded",
        },
        **scope,
    )
    assert dispatch["updated"] is True
    identity = collection.find_one(
        {"request_id": "req-effect-journal"}
    )["effect_observation_journal"]["effect_abc123"]["identity"]

    terminal = record_effect_observation_phase(
        request_id="req-effect-journal",
        effect_id="effect_abc123",
        phase="turn_terminal",
        observation={
            "call_id": "must-not-replace-call-identity",
            "capability_name": "must_not_replace_capability",
            "effect_status": "indeterminate",
            "changed": None,
            "transport": {"outcome": "timed_out"},
            "receipt": {"mutation_outcome": "unknown"},
        },
        **scope,
    )
    late = record_effect_observation_phase(
        request_id="req-effect-journal",
        effect_id="effect_abc123",
        phase="late_terminal",
        observation={
            "call_id": "call-1",
            "capability_name": "upsert_text_relation",
            "outcome": "late_success",
            "effect_status": "succeeded",
            "changed": True,
            "payload": {"success": True, "changed": True},
        },
        **scope,
    )
    duplicate_late = record_effect_observation_phase(
        request_id="req-effect-journal",
        effect_id="effect_abc123",
        phase="late_terminal",
        observation={
            "outcome": "late_success",
            "effect_status": "failed",
            "changed": False,
        },
        **scope,
    )

    assert terminal["updated"] is True
    assert late["updated"] is True
    assert duplicate_late["updated"] is False
    assert duplicate_late["duplicate"] is True
    stored = collection.find_one({"request_id": "req-effect-journal"})
    journal = stored["effect_observation_journal"]["effect_abc123"]
    assert journal["identity"] == identity
    assert journal["identity"]["call_id"] == "call-1"
    assert journal["identity"]["capability_name"] == "upsert_text_relation"
    assert journal["late_terminal"]["effect_status"] == "succeeded"

    full_record = build_turn_execution_record(
        request_id="req-effect-journal",
        session_id="session-effect-journal",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Apply the bounded effect.",
        response_text="The turn returned an indeterminate receipt.",
        interaction_timestamp_utc="2026-07-27T10:00:01Z",
    )
    full_record["effect_observation_journal"] = {}
    upsert = upsert_turn_execution_record_projection(
        record=full_record,
        session_id="session-effect-journal",
        **scope,
    )
    assert upsert["updated"] is True
    preserved = collection.find_one({"request_id": "req-effect-journal"})
    assert (
        preserved["effect_observation_journal"]["effect_abc123"]
        == journal
    )


def test_effect_observation_journal_refuses_cross_actor_request_collision(
    monkeypatch,
) -> None:
    import mongomock
    import src.backend.services.turn_execution_record_service as record_service

    collection = mongomock.MongoClient().von_test.turn_execution_records
    collection.insert_one(
        {
            "request_id": "shared-effect-request",
            "namespace": "#V#actor_a@org",
            "user_id": "#V#actor_a",
            "org_id": "#V#org",
        }
    )
    monkeypatch.setattr(
        record_service,
        "get_turn_execution_records_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        record_service,
        "_turn_execution_mongo_comment",
        lambda *_args, **_kwargs: None,
    )

    outcome = record_effect_observation_phase(
        request_id="shared-effect-request",
        effect_id="effect_actor_b",
        phase="dispatch_intent",
        observation={"dispatch_state": "intent_recorded"},
        namespace="#V#actor_b@org",
        user_id="#V#actor_b",
        org_id="#V#org",
    )

    assert outcome["updated"] is False
    assert outcome["reason"] == "actor_scope_mismatch"
    stored = collection.find_one({"request_id": "shared-effect-request"})
    assert "effect_observation_journal" not in stored


def test_late_effect_observation_refuses_cross_actor_request_id_collision(
    monkeypatch,
) -> None:
    class _Collection:
        document = {
            "request_id": "shared-client-request-id",
            "namespace": "#V#actor_a@org",
            "user_id": "#V#actor_a",
            "org_id": "#V#org",
            "late_effect_observations": [],
        }

        def update_one(self, _query, _update, **_kwargs):
            return SimpleNamespace(
                modified_count=0,
                matched_count=0,
                upserted_id=None,
            )

        def find_one(self, query, **_kwargs):
            for field_name, expected in query.items():
                if self.document.get(field_name) != expected:
                    return None
            return dict(self.document)

    collection = _Collection()
    import src.backend.services.turn_execution_record_service as record_service

    monkeypatch.setattr(
        record_service,
        "get_turn_execution_records_collection",
        lambda: collection,
    )

    outcome = append_late_effect_observation(
        request_id="shared-client-request-id",
        effect_id="effect-b",
        execution_id="execution-b",
        observation={
            "outcome": "late_success",
            "payload": {"success": True, "changed": True},
        },
        namespace="#V#actor_b@org",
        user_id="#V#actor_b",
        org_id="#V#org",
    )

    assert outcome["updated"] is False
    assert outcome["reason"] == "actor_scope_mismatch"
    assert collection.document["late_effect_observations"] == []


def test_turn_record_upsert_refuses_cross_actor_request_id_collision(
    monkeypatch,
) -> None:
    from pymongo.errors import DuplicateKeyError

    class _Collection:
        def update_one(self, query, _update, **_kwargs):
            assert query == {
                "request_id": "shared-upsert-request-id",
                "namespace": "#V#actor_b@org",
                "user_id": "#V#actor_b",
                "org_id": "#V#org",
            }
            raise DuplicateKeyError("request_id_unique")

    import src.backend.services.turn_execution_record_service as record_service

    monkeypatch.setattr(
        record_service,
        "get_turn_execution_records_collection",
        lambda: _Collection(),
    )

    outcome = upsert_turn_execution_record_projection(
        record={"request_id": "shared-upsert-request-id"},
        namespace="#V#actor_b@org",
        user_id="#V#actor_b",
        org_id="#V#org",
    )

    assert outcome == {
        "updated": False,
        "reason": "actor_scope_mismatch",
        "request_id": "shared-upsert-request-id",
    }


def test_projection_field_telemetry_preserves_bounded_collection_row_index() -> None:
    entries = _normalise_projection_field_entries(
        [
            {
                "field_concept_id": "#V#jira_issue_status_field",
                "output_key": "status",
                "location": "issues",
                "row_index": 3,
            },
            {
                "field_concept_id": "#V#jira_issue_summary_field",
                "output_key": "summary",
                "location": "issues",
                "row_index": 2_000_000_000,
            },
        ]
    )

    assert entries[0]["row_index"] == 3
    assert entries[1]["row_index"] == 1_000_000_000
    assert entries[1]["row_index_clamped"] is True


def test_turn_record_carries_decision_attribution_payload() -> None:
    record = build_turn_execution_record(
        request_id="req-decision-attribution",
        session_id="session-decision-attribution",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Run the represented paper workflow.",
        response_text="The represented workflow completed.",
        interaction_timestamp_utc="2026-06-22T00:00:00Z",
        workflow_discovery={
            "discovery_payload_origin": "durable_action_discover_workflows_for_turn",
            "query": "represented paper workflow",
            "candidate_count": 1,
            "candidates": [
                {
                    "concept_id": "#V#paper_workflow",
                    "name": "Paper workflow",
                    "routing_eligible": True,
                }
            ],
        },
        workflow_routing={
            "workflow_id": "#V#paper_workflow",
            "verdict": "rag_selected",
            "source": "selector",
            "selection_rationale": "selector_selected_discovered_candidate",
        },
        selected_workflow_trace={
            "workflow_id": "#V#paper_workflow",
            "workflow_model_policy": {
                "policy_source": "graph",
                "graph_completeness": "graph_complete",
                "policy_id": "#V#default_workflow_model_policy",
            },
        },
        aux_llm_calls=[
            {
                "decision_authority_origin": "python",
                "stage": "workflow_dispatch",
                "component": "internal_mcp_orchestrator",
                "function": "record_turn_contract_dispatch_preflight",
                "decision_class": "workflow_dispatch_turn_contract_check",
                "decision_source": "turn_expected_outcome_contract",
                "changed_outcome": False,
                "reason_code": "selected_workflow_satisfies_contract",
            }
        ],
    )

    attribution = record["decision_attribution"]
    assert attribution["schema_version"] == TURN_DECISION_ATTRIBUTION_SCHEMA_VERSION
    summary = attribution["summary"]
    assert set(summary["decision_kind_breakdown"]) == set(DECISION_KINDS)
    assert summary["decision_kind_breakdown"]["discovery"] == "represented"
    assert summary["decision_kind_breakdown"]["selection"] == "represented"
    assert summary["decision_kind_breakdown"]["dispatch"] == "represented"
    assert summary["decision_kind_breakdown"]["model_choice"] == "represented"
    assert summary["python_fallback_count"] == 0
    assert summary["architecture_integrity_score"] == 1.0

    by_kind = {item["decision_kind"]: item for item in attribution["decisions"]}
    assert by_kind["selection"]["concept_ids"] == ["#V#paper_workflow"]
    assert by_kind["model_choice"]["concept_ids"] == [
        "#V#default_workflow_model_policy"
    ]


def test_turn_record_projects_context_adjudication_handoff() -> None:
    record = build_turn_execution_record(
        request_id="req-context-adjudication",
        session_id="session-context-adjudication",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="The interface Gmail access check passes. Are you sure?",
        response_text="I do not have a token-refresh tool available.",
        interaction_timestamp_utc="2026-06-20T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
            "source": "selector",
        },
        turn_execution_diagnostics={
            "turn_context_handoff_decision": {
                "mode": "no_prior_context",
                "summary": "Use only the current Gmail token-refresh request.",
                "routing_evidence_scope": "current_request_only",
                "expected_outcome_scope": "current_request_only",
                "answer_scope": "current_request_only",
            },
            "turn_context_handoff_mode": "no_prior_context",
            "turn_context_handoff_summary": (
                "Use only the current Gmail token-refresh request."
            ),
            "turn_context_handoff_messages": [],
            "turn_context_handoff_lineage": ["history_index:5"],
            "turn_context_handoff_omitted_context_reasons": [
                "Earlier scholarly-paper topic is irrelevant."
            ],
            "turn_context_handoff_risks": [],
        },
    )

    projection = record["context_adjudication"]
    assert projection["mode"] == "no_prior_context"
    assert projection["summary"] == "Use only the current Gmail token-refresh request."
    assert projection["expected_outcome_scope"] == "current_request_only"
    assert projection["lineage"] == ["history_index:5"]
    assert record["execution"]["context_adjudication"] == projection
    assert record["execution"]["summary"]["context_adjudication_observed"] is True
    assert record["execution"]["summary"]["context_adjudication_mode"] == (
        "no_prior_context"
    )


def test_turn_record_preserves_final_answer_synthesis_and_projection_telemetry() -> (
    None
):
    projected_tool_payload = {
        "tool": "gmail_list_messages",
        "status": "ok",
        "call_id": "tool-call-1",
        "payload": {
            "messages": [
                {"id": "msg-1", "subject": "Lab scheduling"},
                {"id": "msg-2", "subject": "Ontology review"},
            ],
            "_tool_evidence_projection": {
                "tool_concept_id": "#V#gmail_list_messages_tool",
                "evidence_view_concept_ids": [
                    "#V#gmail_message_final_answer_evidence_view"
                ],
                "preserved_fields": [
                    {
                        "field_concept_id": "#V#gmail_message_subject_field",
                        "output_key": "subject",
                        "location": "payload.messages[]",
                        "item_count": 2,
                    }
                ],
                "missing_required_fields": [
                    {
                        "field_concept_id": "#V#gmail_message_sender_field",
                        "output_key": "sender",
                        "reason": "missing_from_payload",
                    }
                ],
                "omitted_fields": [
                    {
                        "field_concept_id": "#V#gmail_message_snippet_field",
                        "output_key": "snippet",
                        "reason": "not_selected_for_view",
                    }
                ],
                "redacted_fields": [
                    {
                        "field_concept_id": "#V#gmail_message_body_field",
                        "output_key": "body",
                        "reason": "too_large_for_context",
                    }
                ],
            },
        },
    }
    aux_llm_calls = [
        {
            "type": "workflow_model_policy_stage",
            "stage": "summariser",
            "workflow_stage_id": "screen_backfill",
            "selected": {"provider": "openai", "model_resolved": "gpt-test"},
            "request": {
                "prompt": "Provide the final answer.",
                "context_messages": [
                    {"role": "system", "content": "system rules"},
                    {
                        "role": "tool",
                        "content": json.dumps(projected_tool_payload),
                    },
                ],
                "context_summary": {"message_count": 2},
                "context_lineage": {"added_message_count": 1},
                "tool_names": ["gmail_list_messages"],
                "tool_count": 1,
            },
        }
    ]
    llm_calls = [
        {
            "type": "llm.generate",
            "stage": "summariser",
            "workflow_stage_id": "screen_backfill",
            "provider": "openai",
            "model": "gpt-test",
            "duration_ms": 123,
            "exchange_blob_ref": {
                "collection": "llm_exchange_blobs",
                "blob_id": "blob-123",
                "sha256": "abc123",
            },
        }
    ]

    record = build_turn_execution_record(
        request_id="req-final-synthesis",
        session_id="session-final-synthesis",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Which messages did you find?",
        response_text="I found two relevant messages.",
        interaction_timestamp_utc="2026-04-20T01:00:00Z",
        workflow_routing={"verdict": "tool_calling", "source": "orchestrator"},
        tool_invocations=[],
        aux_llm_calls=aux_llm_calls,
        llm_calls=llm_calls,
    )

    synthesis = record["final_answer_synthesis"]
    assert synthesis["schema_version"] == "final_answer_synthesis_telemetry.v1"
    assert synthesis["exchange_blob_ref"]["blob_id"] == "blob-123"
    assert synthesis["request"]["prompt"]["text"] == "Provide the final answer."
    assert synthesis["context_lineage"] == {"added_message_count": 1}

    projection = synthesis["tool_evidence_projection"]
    assert projection["projection_count"] == 1
    assert projection["tools"] == ["gmail_list_messages"]
    assert projection["source_tool_invocation_ids"] == ["tool-call-1"]
    assert projection["entries"][0]["projected_payload"] == {
        "messages": [
            {"id": "msg-1", "subject": "Lab scheduling"},
            {"id": "msg-2", "subject": "Ontology review"},
        ]
    }
    assert projection["preserved_field_concept_ids"] == [
        "#V#gmail_message_subject_field"
    ]
    assert projection["missing_required_field_concept_ids"] == [
        "#V#gmail_message_sender_field"
    ]
    assert projection["omitted_field_concept_ids"] == ["#V#gmail_message_snippet_field"]
    assert projection["redacted_field_concept_ids"] == ["#V#gmail_message_body_field"]
    lineage = record["requested_evidence_lineage"]
    assert lineage["schema_version"] == "requested_evidence_lineage.v1"
    assert lineage["final_response"]["source"] == "final_visible_response"
    assert lineage["final_response"]["text_checked_char_count"] == len(
        "I found two relevant messages."
    )
    assert {
        (entry.get("field_concept_id"), entry.get("status"))
        for entry in lineage["requested_fields"]
    } == {
        ("#V#gmail_message_subject_field", "satisfied"),
        ("#V#gmail_message_sender_field", "unresolved"),
        ("#V#gmail_message_snippet_field", "omitted"),
        ("#V#gmail_message_body_field", "redacted"),
    }
    assert lineage["unresolved_requested_fields"] == [
        {
            "field_concept_id": "#V#gmail_message_sender_field",
            "output_key": "sender",
            "status": "unresolved",
            "source": "final_answer_tool_evidence_projection",
            "tool": "gmail_list_messages",
            "tool_concept_id": "#V#gmail_list_messages_tool",
            "source_tool_invocation_id": "tool-call-1",
            "evidence_view_concept_ids": [
                "#V#gmail_message_final_answer_evidence_view"
            ],
            "reason": "missing_from_payload",
        }
    ]
    assert (
        "#V#gmail_message_final_answer_evidence_view"
        in (lineage["represented_contract_ids"])
    )
    assert record["execution"]["summary"]["final_answer_synthesis_observed"] is True
    assert (
        record["execution"]["summary"]["final_answer_synthesis_projection_count"] == 1
    )
    assert record["execution"]["summary"]["requested_evidence_lineage_observed"] is True
    assert record["execution"]["summary"]["requested_evidence_field_count"] == 4
    assert (
        record["completion_gate"]["evidence_payload"]["requested_evidence_lineage"][
            "requested_field_status_counts"
        ]["unresolved"]
        == 1
    )
    assert record["final_response"]["synthesis_observed"] is True
    assert record["final_response"]["requested_evidence_lineage_observed"] is True


def test_public_final_answer_tool_evidence_is_bounded_and_secret_redacted() -> None:
    entry = {
        "context_message_index": 2,
        "tool": "synthetic_lookup",
        "source_tool_invocation_id": "tool-call-1",
        "tool_concept_id": "#V#synthetic_lookup_tool",
        "evidence_view_concept_ids": ["#V#synthetic_evidence_view"],
        "preserved_fields": [
            {
                "field_concept_id": "#V#synthetic_summary_field",
                "output_key": "summary",
                "location": "payload.summary",
            }
        ],
        "missing_required_fields": [
            {
                "field_concept_id": "#V#synthetic_missing_field",
                "output_key": "missing",
                "reason": "missing_from_payload",
            }
        ],
        "projected_payload": {
            "summary": "s" * 900,
            "access_token": "must-not-leak",
            "session_cookie": "must-also-not-leak",
            "items": list(range(20)),
        },
    }
    turn_record = {
        "final_answer_synthesis": {
            "request": {"prompt": "private prompt"},
            "llm_call": {"raw_response": "private response"},
            "tool_evidence_projection": {
                "schema_version": "tool_evidence_projection_reachability.v1",
                "projection_count": 20,
                "tools": ["synthetic_lookup"],
                "missing_required_field_concept_ids": ["#V#synthetic_missing_field"],
                "entries": [entry for _ in range(20)],
            },
        }
    }

    projection = project_final_answer_tool_evidence(turn_record)

    assert projection is not None
    assert projection["projection_count"] == 20
    assert projection["included_projection_count"] == 16
    assert projection["omitted_projection_count"] == 4
    assert len(projection["entries"]) == 16
    projected_payload = projection["entries"][0]["projected_payload"]
    assert projected_payload["summary"] == "s" * 700 + "..."
    assert projected_payload["access_token"] == "[redacted]"
    assert projected_payload["session_cookie"] == "[redacted]"
    assert projected_payload["items"][-1] == {"_omitted_items": 12}
    assert projection["missing_required_field_concept_ids"] == [
        "#V#synthetic_missing_field"
    ]
    assert "request" not in projection
    assert "llm_call" not in projection


def test_turn_record_treats_narration_prompt_as_final_answer_synthesis() -> None:
    aux_llm_calls = [
        {
            "type": "workflow_model_policy_stage",
            "stage": "llm.action",
            "policy_stage": "llm.action",
            "request": {
                "prompt": (
                    "# prompt_turn_execution_narrate_completion_report\n"
                    "You are composing the user-facing answer."
                ),
                "context_messages": [
                    {"role": "system", "content": "system rules"},
                    {
                        "role": "user",
                        "content": "Selected Workflow User Response: grounded answer",
                    },
                ],
                "context_summary": {"message_count": 2},
                "context_lineage": {"stage": "narration"},
            },
        }
    ]
    llm_calls = [
        {
            "type": "llm.generate",
            "stage": "llm.action",
            "model": "gpt-test",
            "duration_ms": 45,
        }
    ]

    record = build_turn_execution_record(
        request_id="req-narration-synthesis",
        session_id="session-narration-synthesis",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="What did the workflow find?",
        response_text="grounded answer",
        interaction_timestamp_utc="2026-06-09T01:00:00Z",
        workflow_routing={"verdict": "tool_calling", "source": "selector"},
        tool_invocations=[],
        aux_llm_calls=aux_llm_calls,
        llm_calls=llm_calls,
    )

    synthesis = record["final_answer_synthesis"]
    assert synthesis["stage"] == "llm.action"
    assert synthesis["request"]["prompt"]["text"].startswith(
        "# prompt_turn_execution_narrate_completion_report"
    )
    assert synthesis["llm_call"]["stage"] == "llm.action"
    assert synthesis["context_lineage"] == {"stage": "narration"}
    assert record["execution"]["summary"]["final_answer_synthesis_observed"] is True


def test_worker_unavailable_failure_code_only_applies_to_tool_routes() -> None:
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#chat_assistant_workflow",
            "verdict": "plain_response",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [
                    {
                        "liveness_reason": "worker_unavailable",
                        "event_kind": "heartbeat",
                        "stage": "orchestrator_start",
                        "phase": "orchestrator_start",
                    }
                ],
            }
        },
        aux_llm_calls=[],
        serialised_invocations=[],
    )

    assert summary["tool_route_selected"] is False
    assert "worker_unavailable_zero_execution" not in list(
        summary.get("failure_codes") or []
    )


def test_worker_unavailable_pre_dispatch_prepare_heartbeat_does_not_emit_tool_failure() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [
                    {
                        "liveness_reason": "worker_unavailable",
                        "event_kind": "heartbeat",
                        "stage": "workflow_dispatch_prepare",
                        "phase": "workflow_dispatch_prepare",
                    }
                ],
            }
        },
        aux_llm_calls=[],
        serialised_invocations=[],
    )

    assert summary["tool_route_selected"] is True
    assert summary["executed_count"] == 0
    assert "worker_unavailable_zero_execution" not in list(
        summary.get("failure_codes") or []
    )


def test_worker_unavailable_with_tool_plan_evidence_emits_tool_failure() -> None:
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [
                    {
                        "liveness_reason": "worker_unavailable",
                        "event_kind": "heartbeat",
                        "stage": "tool_plan",
                        "phase": "tool_plan",
                    }
                ],
            }
        },
        aux_llm_calls=[],
        serialised_invocations=[],
    )

    assert summary["tool_route_selected"] is True
    assert summary["executed_count"] == 0
    assert "worker_unavailable_zero_execution" in list(
        summary.get("failure_codes") or []
    )


def test_tool_execution_summary_uses_tool_call_end_events_for_executed_count() -> None:
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#chat_assistant_workflow",
            "verdict": "fallback",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [
                    {
                        "status": "tool_call_start",
                        "event_kind": "tool_call_start",
                        "phase": "tool_execute",
                        "tool": "turn_execution_list",
                        "call_id": "call-1",
                    },
                    {
                        "status": "tool_invoked",
                        "event_kind": "tool_call_end",
                        "phase": "tool_execute",
                        "tool": "turn_execution_list",
                        "call_id": "call-1",
                    },
                ],
            }
        },
        aux_llm_calls=[],
        serialised_invocations=[],
    )

    assert summary["tool_route_selected"] is False
    assert summary["started_count"] == 1
    assert summary["executed_count"] == 1


def test_tool_execution_summary_marks_missing_dispatch_boundary_after_tool_selection() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_selector",
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "rag_selected",
            }
        ],
        serialised_invocations=[],
    )

    assert summary["tool_route_selected"] is True
    assert summary["selected_execution_mode"] == "tool_pipeline"
    assert summary["last_successful_boundary"] == "workflow_selected"
    assert "tool_dispatch_boundary_missing" in list(summary.get("failure_codes") or [])


def test_tool_execution_summary_marks_missing_dispatch_boundary_after_custom_selection() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#arxiv_paper_representation_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_selector",
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "verdict": "rag_selected",
            }
        ],
        serialised_invocations=[],
    )

    assert summary["tool_route_selected"] is False
    assert summary["selected_execution_mode"] == "custom_workflow"
    assert summary["dispatch_workflow_id"] == "#V#arxiv_paper_representation_workflow"
    assert summary["last_successful_boundary"] == "workflow_selected"
    assert "custom_workflow_dispatch_not_started" in list(
        summary.get("failure_codes") or []
    )
    assert summary["custom_workflow_execution"]["observed"] is False


def test_custom_workflow_summary_uses_selected_workflow_trace_when_dispatch_events_missing() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#arxiv_paper_representation_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_selector",
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "verdict": "rag_selected",
            }
        ],
        serialised_invocations=[],
        selected_workflow_trace={
            "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
            "child_workflow_completed": False,
            "child_workflow_final_state": "failed",
            "child_workflow_error": "arxiv_mcp_server_missing_file_path",
            "completion_report_source": "child_completion_report",
        },
    )

    assert summary["dispatch_terminal_status"] == "failed"
    assert summary["dispatch_terminal_failure_reason"] == "child_workflow_failed"
    assert (
        summary["dispatch_terminal_failure_detail"]
        == "arxiv_mcp_server_missing_file_path"
    )
    assert "custom_workflow_dispatch_not_started" not in list(
        summary.get("failure_codes") or []
    )
    assert summary["last_successful_boundary"] == "workflow_terminal"
    assert summary["zero_tool_reason_code"] == (
        "custom_workflow_failed_before_tool_invocation"
    )
    assert summary["custom_workflow_execution"]["observed"] is True
    assert summary["custom_workflow_execution"]["final_state"] == "failed"
    assert (
        summary["custom_workflow_execution"]["completion_report_source"]
        == "child_completion_report"
    )


def test_custom_workflow_summary_uses_trace_execution_summary_when_aux_entry_missing() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#entity_information_retrieval_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_selector",
                "workflow_id": "#V#entity_information_retrieval_workflow",
                "verdict": "rag_selected",
            }
        ],
        serialised_invocations=[],
        selected_workflow_trace={
            "selected_workflow_id": "#V#entity_information_retrieval_workflow",
            "child_workflow_completed": True,
            "child_workflow_final_state": (
                "#V#workflow_step_entity_information_retrieval_workflow_completed"
            ),
            "completion_report_source": "child_completion_report",
            "workflow_execution_summary": {
                "schema_version": "workflow_execution_summary.v1",
                "workflow_id": "#V#entity_information_retrieval_workflow",
                "completed": True,
                "terminal_status": "completed",
                "final_state": (
                    "#V#workflow_step_entity_information_retrieval_workflow_completed"
                ),
                "step_result_envelope_count": 5,
                "action_started_count": 5,
                "action_completed_count": 5,
                "action_success_count": 4,
                "action_failure_count": 1,
                "action_unknown_count": 0,
                "runtime_event_count": 0,
                "terminal_effect_count": 1,
                "terminal_effects": [],
                "durable_side_effect_count": 0,
                "durable_side_effects": [],
            },
        },
    )

    custom_execution = summary["custom_workflow_execution"]
    assert custom_execution["observed"] is True
    assert custom_execution["workflow_id"] == "#V#entity_information_retrieval_workflow"
    assert custom_execution["action_started_count"] == 5
    assert custom_execution["action_completed_count"] == 5
    assert custom_execution["action_failure_count"] == 1
    assert custom_execution["completion_report_source"] == "child_completion_report"
    assert summary["zero_tool_reason_code"] == "custom_workflow_actions_handled_turn"
    assert summary["zero_tool_execution_expected"] is True


def test_tool_execution_summary_preserves_local_handoff_failure_reason() -> None:
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "execution_mode_selected",
                "status": "selected",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "contract_resolution",
                "status": "resolved",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_handoff",
                "status": "failed",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
                "reason": "tool_pipeline_setup_exception",
                "error_class": "RuntimeError",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_terminal",
                "status": "failed",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
                "reason": "tool_pipeline_setup_exception",
                "error_class": "RuntimeError",
                "completed": False,
            },
        ],
        serialised_invocations=[],
    )

    assert summary["workflow_handoff_started"] is False
    assert summary["workflow_handoff_failure_reason"] == "tool_pipeline_setup_exception"
    assert (
        summary["dispatch_terminal_failure_reason"] == "tool_pipeline_setup_exception"
    )
    assert summary["failure_codes"] == [
        "tool_pipeline_setup_exception",
        "tool_dispatch_not_started",
    ]
    assert summary["last_successful_boundary"] == "workflow_terminal"


def test_tool_execution_summary_preserves_custom_workflow_first_step_failure_locality() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#meeting_invitation_testing_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "execution_mode_selected",
                "status": "selected",
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#meeting_invitation_testing_workflow",
                "dispatch_workflow_id": "#V#meeting_invitation_testing_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_handoff",
                "status": "started",
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#meeting_invitation_testing_workflow",
                "dispatch_workflow_id": "#V#meeting_invitation_testing_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_terminal",
                "status": "failed",
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#meeting_invitation_testing_workflow",
                "dispatch_workflow_id": "#V#meeting_invitation_testing_workflow",
                "final_state": "prepare_spec",
                "completed": False,
                "reason": "workflow_launch_input_resolution_failed",
                "detail": (
                    "Workflow #V#meeting_invitation_testing_workflow could not start "
                    "because required launch inputs were unresolved: invitation_text."
                ),
                "workflow_launch_input_resolution_status": "failed",
                "unresolved_required_inputs": ["invitation_text"],
                "failing_state_id": "prepare_spec",
                "failing_action_id": "tool.prepare_spec",
            },
            {
                "type": "workflow_execution",
                "workflow_id": "#V#meeting_invitation_testing_workflow",
                "final_state": "prepare_spec",
                "completed": False,
                "execution_summary": {
                    "schema_version": "workflow_execution_summary.v1",
                    "workflow_id": "#V#meeting_invitation_testing_workflow",
                    "completed": False,
                    "final_state": "prepare_spec",
                    "step_result_envelope_count": 0,
                    "action_started_count": 0,
                    "action_completed_count": 0,
                    "action_success_count": 0,
                    "action_failure_count": 0,
                    "action_unknown_count": 0,
                    "first_failing_state_id": "prepare_spec",
                    "first_failing_action_id": "tool.prepare_spec",
                    "runtime_event_count": 0,
                    "terminal_effect_count": 0,
                    "terminal_effects": [],
                    "durable_side_effect_count": 0,
                    "durable_side_effects": [],
                },
            },
        ],
        serialised_invocations=[],
    )

    assert summary["tool_route_selected"] is False
    assert summary["selected_execution_mode"] == "custom_workflow"
    assert summary["dispatch_workflow_id"] == "#V#meeting_invitation_testing_workflow"
    assert summary["workflow_handoff_started"] is True
    assert summary["dispatch_terminal_status"] == "failed"
    assert summary["dispatch_terminal_final_state"] == "prepare_spec"
    assert summary["dispatch_terminal_failure_reason"] == (
        "workflow_launch_input_resolution_failed"
    )
    assert summary["dispatch_terminal_failure_detail"] == (
        "Workflow #V#meeting_invitation_testing_workflow could not start "
        "because required launch inputs were unresolved: invitation_text."
    )
    assert summary["dispatch_terminal_launch_input_resolution_status"] == "failed"
    assert summary["dispatch_terminal_unresolved_required_inputs"] == [
        "invitation_text"
    ]
    assert summary["dispatch_terminal_failing_state_id"] == "prepare_spec"
    assert summary["dispatch_terminal_failing_action_id"] == "tool.prepare_spec"
    custom_execution = summary["custom_workflow_execution"]
    assert custom_execution["observed"] is True
    assert custom_execution["schema_version"] == "workflow_execution_summary.v1"
    assert custom_execution["workflow_id"] == "#V#meeting_invitation_testing_workflow"
    assert custom_execution["completed"] is False
    assert custom_execution["final_state"] == "prepare_spec"
    assert custom_execution["step_result_envelope_count"] == 0
    assert custom_execution["action_started_count"] == 0
    assert custom_execution["action_completed_count"] == 0
    assert custom_execution["action_success_count"] == 0
    assert custom_execution["action_failure_count"] == 0
    assert custom_execution["action_unknown_count"] == 0
    assert custom_execution["first_failing_state_id"] == "prepare_spec"
    assert custom_execution["first_failing_action_id"] == "tool.prepare_spec"
    assert custom_execution["runtime_event_count"] == 0
    assert custom_execution["terminal_effect_count"] == 0
    assert custom_execution["terminal_effects"] == []
    assert custom_execution["durable_side_effect_count"] == 0
    assert custom_execution["durable_side_effects"] == []
    assert summary["zero_tools_executed"] is True
    assert summary["failure_codes"] == []
    assert summary["zero_tool_reason_code"] == (
        "custom_workflow_failed_before_tool_invocation"
    )
    assert summary["zero_tool_execution_expected"] is False


def test_tool_execution_summary_promotes_authoritative_submission_failure_to_dispatch_failure() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#conversation_turn_execution_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_instance_submission",
                "workflow_id": "#V#conversation_turn_execution_workflow",
                "status": "submission_failed",
                "reason_code": "workflow_not_runnable",
                "error": (
                    "Workflow #V#conversation_turn_execution_workflow is not runnable "
                    "because actions turn_execution.critic and "
                    "turn_execution.completion_gate are unsupported."
                ),
                "submission": {
                    "verification": {
                        "runnable_verification_success": False,
                        "unsupported_action_ids": [
                            "turn_execution.critic",
                            "turn_execution.completion_gate",
                        ],
                    }
                },
            }
        ],
        serialised_invocations=[],
    )

    assert summary["selected_execution_mode"] == "custom_workflow"
    assert summary["dispatch_workflow_id"] == "#V#conversation_turn_execution_workflow"
    assert summary["dispatch_terminal_status"] == "failed"
    assert summary["dispatch_terminal_failure_reason"] == "workflow_not_runnable"
    assert "turn_execution.critic" in (
        summary["dispatch_terminal_failure_detail"] or ""
    )


def test_tool_execution_summary_keeps_tool_pipeline_mode_for_submission_failure_without_boundaries() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_selector",
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "rag_selected",
            },
            {
                "type": "workflow_instance_submission",
                "workflow_id": "#V#tool_calling_workflow",
                "status": "submission_failed",
                "reason_code": "workflow_not_runnable",
                "error": (
                    "Workflow #V#tool_calling_workflow is not runnable because the "
                    "tool pipeline boundary was missing."
                ),
                "submission": {
                    "verification": {
                        "runnable_verification_success": False,
                    }
                },
            },
        ],
        serialised_invocations=[],
    )

    assert summary["selected_execution_mode"] == "tool_pipeline"
    assert summary["dispatch_workflow_id"] == "#V#tool_calling_workflow"
    assert summary["dispatch_terminal_status"] == "failed"
    assert summary["dispatch_terminal_failure_reason"] == "workflow_not_runnable"
    assert "tool_dispatch_boundary_missing" in list(summary.get("failure_codes") or [])
    assert "custom_workflow_dispatch_not_started" not in list(
        summary.get("failure_codes") or []
    )


def test_tool_execution_summary_records_custom_workflow_action_and_side_effect_evidence() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#workflow_creation_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "execution_mode_selected",
                "status": "selected",
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#workflow_creation_workflow",
                "dispatch_workflow_id": "#V#workflow_creation_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_handoff",
                "status": "started",
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#workflow_creation_workflow",
                "dispatch_workflow_id": "#V#workflow_creation_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_terminal",
                "status": "completed",
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#workflow_creation_workflow",
                "dispatch_workflow_id": "#V#workflow_creation_workflow",
                "final_state": "done",
                "completed": True,
            },
            {
                "type": "workflow_execution",
                "workflow_id": "#V#workflow_creation_workflow",
                "final_state": "done",
                "completed": True,
                "execution_summary": {
                    "schema_version": "workflow_execution_summary.v1",
                    "workflow_id": "#V#workflow_creation_workflow",
                    "completed": True,
                    "final_state": "done",
                    "step_result_envelope_count": 2,
                    "action_started_count": 2,
                    "action_completed_count": 2,
                    "action_success_count": 2,
                    "action_failure_count": 0,
                    "action_unknown_count": 0,
                    "runtime_event_count": 4,
                    "terminal_effect_count": 1,
                    "terminal_effects": [
                        {
                            "state_id": "done",
                            "symbol": "#V#workflow_effect_workflow_creation_done_terminal",
                            "alias": "workflow_effect_workflow_creation_done_terminal",
                            "applied": True,
                        }
                    ],
                    "durable_side_effect_count": 2,
                    "durable_side_effects": [
                        {
                            "mutation_kind": "created",
                            "artefact_type": "workflow",
                            "source_key": "created_workflow_ids",
                            "source_path": "created_workflow_ids",
                            "artefact_count": 1,
                            "artefact_ids": ["#V#wf_new"],
                        },
                        {
                            "mutation_kind": "updated",
                            "artefact_type": "type",
                            "source_key": "updated_type_ids",
                            "source_path": "alignment.updated_type_ids",
                            "artefact_count": 1,
                            "artefact_ids": ["#V#durable_workflow"],
                        },
                    ],
                },
            },
        ],
        serialised_invocations=[],
    )

    custom_execution = summary["custom_workflow_execution"]
    assert custom_execution["observed"] is True
    assert custom_execution["workflow_id"] == "#V#workflow_creation_workflow"
    assert custom_execution["action_completed_count"] == 2
    assert custom_execution["action_success_count"] == 2
    assert custom_execution["terminal_effect_count"] == 1
    assert custom_execution["durable_side_effect_count"] == 2
    assert custom_execution["durable_side_effects"] == [
        {
            "mutation_kind": "created",
            "artefact_type": "workflow",
            "source_key": "created_workflow_ids",
            "source_path": "created_workflow_ids",
            "artefact_count": 1,
            "artefact_ids": ["#V#wf_new"],
        },
        {
            "mutation_kind": "updated",
            "artefact_type": "type",
            "source_key": "updated_type_ids",
            "source_path": "alignment.updated_type_ids",
            "artefact_count": 1,
            "artefact_ids": ["#V#durable_workflow"],
        },
    ]
    assert summary["zero_tools_executed"] is True
    assert summary["zero_tool_reason_code"] == "custom_workflow_actions_handled_turn"
    assert summary["zero_tool_execution_expected"] is True


def test_turn_execution_record_keeps_custom_workflow_execution_consistent_across_surfaces() -> (
    None
):
    aux_llm_calls = [
        {
            "type": "workflow_dispatch_boundary",
            "boundary": "execution_mode_selected",
            "status": "selected",
            "selected_execution_mode": "custom_workflow",
            "selected_workflow_id": "#V#workflow_creation_workflow",
            "dispatch_workflow_id": "#V#workflow_creation_workflow",
        },
        {
            "type": "workflow_dispatch_boundary",
            "boundary": "workflow_handoff",
            "status": "started",
            "selected_execution_mode": "custom_workflow",
            "selected_workflow_id": "#V#workflow_creation_workflow",
            "dispatch_workflow_id": "#V#workflow_creation_workflow",
        },
        {
            "type": "workflow_dispatch_boundary",
            "boundary": "workflow_terminal",
            "status": "completed",
            "selected_execution_mode": "custom_workflow",
            "selected_workflow_id": "#V#workflow_creation_workflow",
            "dispatch_workflow_id": "#V#workflow_creation_workflow",
            "final_state": "done",
            "completed": True,
        },
        {
            "type": "workflow_execution",
            "workflow_id": "#V#workflow_creation_workflow",
            "final_state": "done",
            "completed": True,
            "execution_summary": {
                "schema_version": "workflow_execution_summary.v1",
                "workflow_id": "#V#workflow_creation_workflow",
                "completed": True,
                "final_state": "done",
                "step_result_envelope_count": 1,
                "action_started_count": 1,
                "action_completed_count": 1,
                "action_success_count": 1,
                "action_failure_count": 0,
                "action_unknown_count": 0,
                "runtime_event_count": 2,
                "terminal_effect_count": 1,
                "terminal_effects": [
                    {
                        "state_id": "done",
                        "symbol": "#V#workflow_effect_workflow_creation_done_terminal",
                        "alias": "workflow_effect_workflow_creation_done_terminal",
                        "applied": True,
                    }
                ],
                "durable_side_effect_count": 1,
                "durable_side_effects": [
                    {
                        "mutation_kind": "created",
                        "artefact_type": "workflow",
                        "source_key": "created_workflow_ids",
                        "source_path": "created_workflow_ids",
                        "artefact_count": 1,
                        "artefact_ids": ["#V#wf_new"],
                    }
                ],
            },
        },
    ]
    record = build_turn_execution_record(
        request_id="req-custom-1",
        session_id="session-custom-1",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Create the workflow definition.",
        response_text="Workflow created.",
        interaction_timestamp_utc="2026-03-24T01:00:00Z",
        workflow_discovery={
            "matches": [{"concept_id": "#V#workflow_creation_workflow"}]
        },
        workflow_routing={
            "workflow_id": "#V#workflow_creation_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        tool_invocations=[],
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=aux_llm_calls,
    )

    summary = record["execution"]["summary"]
    dispatch = record["workflow_routing_diagnostics"]["dispatch"]
    summary_custom_execution = dict(summary["custom_workflow_execution"])
    dispatch_custom_execution = dict(dispatch["custom_workflow_execution"])
    assert {
        key: summary_custom_execution.get(key)
        for key in dispatch_custom_execution.keys()
    } == dispatch_custom_execution
    assert summary_custom_execution["completion_report_source"] is None
    assert summary_custom_execution["error"] is None
    assert summary_custom_execution["result_snapshot"] is None
    assert summary["zero_tool_reason_code"] == "custom_workflow_actions_handled_turn"
    assert dispatch["zero_tool_reason_code"] == "custom_workflow_actions_handled_turn"
    assert dispatch["custom_workflow_execution"]["durable_side_effects"] == [
        {
            "mutation_kind": "created",
            "artefact_type": "workflow",
            "source_key": "created_workflow_ids",
            "source_path": "created_workflow_ids",
            "artefact_count": 1,
            "artefact_ids": ["#V#wf_new"],
        }
    ]


def test_turn_record_uses_selected_workflow_trace_for_supervised_custom_failure() -> (
    None
):
    record = build_turn_execution_record(
        request_id="req-selected-trace-failure",
        session_id="session-selected-trace-failure",
        namespace="#V#user",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="https://arxiv.org/abs/2603.18678",
        response_text=(
            "I couldn't complete that request because the authoritative "
            "conversation-turn workflow failed."
        ),
        interaction_timestamp_utc="2026-04-12T02:24:06Z",
        workflow_routing={
            "workflow_id": "#V#arxiv_paper_representation_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        tool_invocations=[],
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_selector",
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "verdict": "rag_selected",
            }
        ],
        selected_workflow_trace={
            "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
            "child_workflow_completed": False,
            "child_workflow_final_state": "failed",
            "child_workflow_error": "arxiv_mcp_server_missing_file_path",
            "completion_report_source": "child_completion_report",
        },
        completion_report={
            "response_text": (
                "arXiv download succeeded but no file path was returned by "
                "arxiv-mcp-server"
            )
        },
    )

    summary = record["execution"]["summary"]
    assert summary["dispatch_terminal_status"] == "failed"
    assert summary["dispatch_terminal_failure_reason"] == "child_workflow_failed"
    assert (
        summary["dispatch_terminal_failure_detail"]
        == "arxiv_mcp_server_missing_file_path"
    )
    assert summary["custom_workflow_execution"]["observed"] is True

    workflow_effects = [
        effect
        for effect in record["required_effects"]
        if effect.get("effect_type") == "workflow_execution"
    ]
    assert workflow_effects
    assert workflow_effects[0]["status"] == "not_satisfied"
    assert record["completion_gate"]["decision"] in {"failed", "partial"}
    assert (
        record["execution"]["selected_workflow_trace"]["child_workflow_error"]
        == "arxiv_mcp_server_missing_file_path"
    )


def test_turn_record_preserves_first_class_turn_expected_outcome_contract_snapshot() -> (
    None
):
    expected_contract = {
        "summary": "Answer only with grounded represented records.",
        "grounding_requirement": (
            "Only surface records that are grounded in represented evidence."
        ),
        "precision_policy": "Prefer omission over unsupported claims.",
    }
    record = build_turn_execution_record(
        request_id="req-expected-contract-1",
        session_id="session-expected-contract-1",
        namespace="#V#user",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="List grounded represented records linked to the current user.",
        response_text="Example Record",
        interaction_timestamp_utc="2026-04-20T06:12:00Z",
        workflow_discovery={
            "matches": [{"concept_id": "#V#tool_calling_workflow"}],
            "turn_expected_outcome_contract": expected_contract,
        },
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        selected_workflow_trace={
            "selected_workflow_id": "#V#tool_calling_workflow",
            "expected_outcome_contract": expected_contract,
        },
        turn_expected_outcome_contract={
            "schema_version": "turn_expected_outcome_contract.v1",
            "fields": expected_contract,
            "field_count": len(expected_contract),
            "sources": ["turn_expected_outcome_contract"],
        },
        tool_invocations=[],
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[],
    )

    assert record["turn_expected_outcome_contract"] == expected_contract
    contract_state = record["turn_expected_outcome_contract_state"]
    assert contract_state["schema_version"] == "turn_expected_outcome_contract.v1"
    assert contract_state["fields"] == expected_contract
    assert (
        record["workflow_routing_diagnostics"]["turn_expected_outcome_contract"]
        == expected_contract
    )
    assert (
        record["execution"]["selected_workflow_trace"]["expected_outcome_contract"]
        == expected_contract
    )
    assert (
        record["execution"]["selected_workflow_trace"][
            "expected_outcome_contract_state"
        ]["fields"]
        == expected_contract
    )
    assert record["execution"]["summary"][
        "turn_expected_outcome_contract_field_count"
    ] == len(expected_contract)


def test_build_workflow_routing_diagnostics_surfaces_discovery_stage_timings() -> None:
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery={
            "query": "who am i in this conversation",
            "candidate_count": 0,
            "match_count": 0,
            "candidates": [],
            "stage_timings": [
                {
                    "stage": "capability_index_search",
                    "status": "ok",
                    "elapsed_ms": 12000.5,
                },
                {"stage": "semantic_search", "status": "ok", "elapsed_ms": 45123.7},
                {"stage": "vontology_search", "status": "ok", "elapsed_ms": 800.2},
                {"stage": "enrich_matches", "status": "ok", "elapsed_ms": 50.0},
            ],
        },
        workflow_routing={},
        turn_execution_diagnostics={},
        aux_llm_calls=[],
    )
    discovery_block = diagnostics["discovery"]
    assert discovery_block["stage_timing_count"] == 4
    stage_timings = discovery_block["stage_timings"]
    assert [entry["stage"] for entry in stage_timings] == [
        "capability_index_search",
        "semantic_search",
        "vontology_search",
        "enrich_matches",
    ]
    slowest = discovery_block["slowest_stages"]
    assert [entry["stage"] for entry in slowest[:2]] == [
        "semantic_search",
        "capability_index_search",
    ]
    assert slowest[0]["elapsed_ms"] == 45123.7


def test_build_workflow_routing_diagnostics_surfaces_discovery_payload_origin() -> None:
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery={
            "query": "who am i in this conversation",
            "discovery_payload_origin": "discover_workflows_for_turn",
            "candidates": [],
            "candidate_count": 0,
            "match_count": 0,
        },
        workflow_routing={},
        turn_execution_diagnostics={},
        aux_llm_calls=[],
    )
    discovery_block = diagnostics["discovery"]
    assert discovery_block["discovery_payload_origin"] == "discover_workflows_for_turn"


def test_build_workflow_routing_diagnostics_marks_missing_discovery_payload() -> None:
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery=None,
        workflow_routing={},
        turn_execution_diagnostics={},
        aux_llm_calls=[],
    )
    assert (
        diagnostics["discovery"]["discovery_payload_origin"]
        == "missing_discovery_payload"
    )


def test_build_workflow_routing_diagnostics_marks_unstamped_discovery_payload() -> None:
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery={
            "query": "who am i",
            "candidates": [],
            "candidate_count": 0,
            "match_count": 0,
        },
        workflow_routing={},
        turn_execution_diagnostics={},
        aux_llm_calls=[],
    )
    assert (
        diagnostics["discovery"]["discovery_payload_origin"]
        == "unstamped_discovery_payload"
    )


def test_build_workflow_routing_diagnostics_preserves_selector_exchange_and_dispatch_events() -> (
    None
):
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery={
            "query": "meeting invitation testing workflow",
            "candidate_count": 2,
            "match_count": 0,
            "candidates": [
                {
                    "concept_id": "#V#meeting_invitation_testing_workflow",
                    "name": "Meeting invitation testing workflow",
                    "description": "Materialise a meeting invitation test run.",
                    "routing_eligible": False,
                    "routing_exclusion_reason": "missing_authoritative_purpose",
                },
                {
                    "concept_id": "#V#tool_calling_workflow",
                    "name": "Tool calling workflow",
                    "description": "General-purpose tool workflow",
                    "routing_eligible": True,
                },
            ],
        },
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
            "selection_rationale": "selector_selected_discovered_candidate",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_dispatch_prepare_step",
                "step_id": "workflow_model_policy",
                "step_label": "Load routing model policy",
                "status": "completed",
                "duration_ms": 7,
            },
            {
                "type": "workflow_dispatch_prepare_step",
                "step_id": "selector_candidate_preparation",
                "step_label": "Prepare selector candidates",
                "status": "completed",
                "duration_ms": 11,
            },
            {
                "type": "workflow_dispatch_turn_contract_check",
                "status": "override_required",
                "selected_workflow_id": "#V#meeting_invitation_testing_workflow",
                "selected_workflow_can_satisfy_contract": False,
                "required_tools": [
                    "search_knowledge_base",
                    "search_concepts",
                    "search_web",
                ],
                "required_surface_families": [
                    "knowledge_base",
                    "web",
                ],
                "external_surface_families": ["web"],
                "override_reason": (
                    "selected_custom_workflow_cannot_satisfy_multi_surface_turn_contract"
                ),
                "reasoning": (
                    "The selected custom workflow did not advertise tool-pipeline "
                    "execution, so dispatch had to use the general tool workflow."
                ),
                "turn_expected_outcome_contract": {
                    "success_target": "Grounded meeting invitation test plan.",
                    "selector_guidance": (
                        "Use represented meeting context and live web confirmation."
                    ),
                },
            },
            {
                "type": "workflow_selector_prompt",
                "prompt_id": "#V#chat_turn_classifier_prompt",
                "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
                "prompt_provenance": {
                    "prompt_mode": "rag_first_candidate_selector",
                    "resolved_prompt_id": "#V#chat_turn_classifier_prompt",
                    "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
                    "render_variables": {
                        "turn_text": "Run the meeting invitation test",
                        "candidate_list": "- #V#meeting_invitation_testing_workflow",
                    },
                    "truncated": False,
                },
                "candidate_list": {
                    "text": "- #V#meeting_invitation_testing_workflow",
                    "char_count": 40,
                },
            },
            {
                "type": "workflow_model_policy_stage",
                "stage": "workflow_dispatch",
                "policy_stage": "classifier",
                "request": {
                    "prompt": {"text": "Select workflow", "char_count": 15},
                    "context_messages": [
                        {
                            "role": "system",
                            "content": {
                                "text": "CURRENT USER CONTEXT: Test User (#V#test_user)",
                                "char_count": 46,
                            },
                        },
                        {
                            "role": "system",
                            "content": {
                                "text": "Selector system prompt",
                                "char_count": 22,
                            },
                        },
                        {
                            "role": "user",
                            "content": {
                                "text": "Run the meeting invitation test",
                                "char_count": 31,
                            },
                        },
                    ],
                    "context_message_count": 3,
                    "context_summary": {
                        "message_count": 3,
                        "leading_system_message_count": 2,
                        "role_counts": {"system": 2, "user": 1},
                        "total_content_chars": 99,
                    },
                    "context_lineage": {
                        "stage": "workflow_dispatch",
                        "base_context_source": "augmented_context",
                        "insertion_strategy": "after_leading_system",
                        "base_context_summary": {
                            "message_count": 2,
                            "leading_system_message_count": 1,
                            "role_counts": {"system": 1, "user": 1},
                            "total_content_chars": 77,
                        },
                        "stage_added_message_count": 1,
                        "stage_added_messages": [
                            {
                                "role": "system",
                                "content_preview": "Selector system prompt",
                                "content_char_count": 22,
                            }
                        ],
                        "stage_context_summary": {
                            "message_count": 3,
                            "leading_system_message_count": 2,
                            "role_counts": {"system": 2, "user": 1},
                            "total_content_chars": 99,
                        },
                    },
                },
                "selected": {
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "model_resolved": "gpt-5-mini",
                },
                "fallback_used": True,
                "fallback_attempt_count": 2,
                "failure_count": 1,
                "fallback_attempts": [
                    {
                        "attempt_no": 1,
                        "provider": "ollama",
                        "model": "granite3.3:2b",
                        "status": "failed",
                        "failure_kind": "provider_unreachable",
                        "error": "connection refused",
                    },
                    {
                        "attempt_no": 2,
                        "provider": "openai",
                        "model": "gpt-5-mini",
                        "status": "succeeded",
                        "response": {
                            "text": "#V#tool_calling_workflow",
                            "char_count": 24,
                        },
                    },
                ],
                "errors": [
                    {
                        "model_resolved": "granite3.3:2b",
                        "failure_kind": "provider_unreachable",
                        "error": "connection refused",
                    }
                ],
            },
            {
                "type": "workflow_selector",
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "rag_selected",
                "model_name": "gpt-5-mini",
                "prompt": {"text": "Select workflow", "char_count": 15},
                "prompt_provenance": {
                    "prompt_mode": "rag_first_candidate_selector",
                    "resolved_prompt_id": "#V#chat_turn_classifier_prompt",
                    "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
                    "render_variables": {
                        "turn_text": "Run the meeting invitation test",
                        "candidate_list": "- #V#meeting_invitation_testing_workflow",
                    },
                },
                "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
                "candidate_list": {
                    "text": "- #V#meeting_invitation_testing_workflow",
                    "char_count": 40,
                },
                "context_lineage": {
                    "stage": "workflow_dispatch",
                    "base_context_source": "augmented_context",
                    "stage_added_message_count": 1,
                },
                "response": {
                    "text": "#V#tool_calling_workflow",
                    "char_count": 24,
                },
                "candidate_entries": [
                    {
                        "concept_id": "#V#meeting_invitation_testing_workflow",
                        "name": "Meeting invitation testing workflow",
                        "description": "Materialise a meeting invitation test run.",
                        "candidate_source": "workflow_discovery",
                        "candidate_reason": "discovered_workflow_candidate",
                    },
                    {
                        "concept_id": "#V#tool_calling_workflow",
                        "name": "Tool calling workflow",
                        "description": "General-purpose tool workflow",
                        "candidate_source": "selector_default",
                        "candidate_reason": "builtin_selector_candidate",
                    },
                ],
                "discovery_excluded_candidates": [
                    {
                        "concept_id": "#V#meeting_invitation_testing_workflow",
                        "routing_eligible": False,
                        "routing_exclusion_reason": "missing_authoritative_purpose",
                    }
                ],
                "selection_metadata": {
                    "selection_resolution": "candidate_label_exact_match",
                    "raw_candidate_label": "#V#tool_calling_workflow",
                    "raw_response_format": "text",
                },
                "selection_rationale": "selector_selected_discovered_candidate",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "execution_mode_selected",
                "status": "selected",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "contract_resolution",
                "status": "resolved",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
            },
        ],
        execution_summary={
            "tool_route_selected": True,
            "selected_execution_mode": "tool_pipeline",
            "dispatch_workflow_id": "#V#tool_calling_workflow",
            "dispatch_event_count": 2,
            "contract_resolution_status": "resolved",
            "workflow_handoff_started": False,
            "dispatch_terminal_status": None,
            "dispatch_terminal_final_state": None,
            "dispatch_terminal_completed": None,
            "planned_count": 1,
            "started_count": 0,
            "executed_count": 0,
            "zero_tools_executed": True,
            "failure_codes": ["tool_dispatch_not_started"],
            "last_successful_boundary": "contract_resolution",
        },
    )

    assert diagnostics["schema_version"] == "workflow_routing_diagnostics.v1"
    assert diagnostics["selector"]["prompt"]["text"] == "Select workflow"
    assert diagnostics["selector"]["response"]["text"] == "#V#tool_calling_workflow"
    assert diagnostics["selector"]["model_name"] == "gpt-5-mini"
    assert diagnostics["selector"]["prompt_provenance"]["resolved_prompt_id"] == (
        "#V#chat_turn_classifier_prompt"
    )
    assert diagnostics["selector"]["candidate_list"]["text"] == (
        "- #V#meeting_invitation_testing_workflow"
    )
    assert diagnostics["selector"]["requested_prompt_ids"] == [
        "#V#chat_turn_classifier_prompt"
    ]
    assert diagnostics["selector"]["candidate_source_counts"] == [
        {"name": "selector_default", "count": 1},
        {"name": "workflow_discovery", "count": 1},
    ]
    assert diagnostics["selector"]["model_request"]["prompt"]["text"] == (
        "Select workflow"
    )
    assert diagnostics["selector"]["model_request"]["context_messages"][0]["role"] == (
        "system"
    )
    assert diagnostics["selector"]["model_request"]["context_summary"] == {
        "message_count": 3,
        "leading_system_message_count": 2,
        "role_counts": {"system": 2, "user": 1},
        "total_content_chars": 99,
    }
    assert diagnostics["selector"]["context_lineage"]["base_context_source"] == (
        "augmented_context"
    )
    assert diagnostics["selector"]["context_lineage"]["stage_added_message_count"] == 1
    assert diagnostics["selector"]["model_attempts"][0]["failure_kind"] == (
        "provider_unreachable"
    )
    assert diagnostics["selector"]["primary_fallback_failure_kind"] == (
        "provider_unreachable"
    )
    assert diagnostics["selector"]["fallback_failure_kind_counts"] == [
        {"name": "provider_unreachable", "count": 1}
    ]
    assert diagnostics["selector"]["model_attempts"][1]["response"]["text"] == (
        "#V#tool_calling_workflow"
    )
    assert diagnostics["selector"]["selection_resolution"] == (
        "candidate_label_exact_match"
    )
    assert diagnostics["selector"]["candidate_entries"][0]["concept_id"] == (
        "#V#meeting_invitation_testing_workflow"
    )
    assert diagnostics["discovery"]["excluded_candidates"][0]["concept_id"] == (
        "#V#meeting_invitation_testing_workflow"
    )
    assert diagnostics["discovery"]["match_absence_reason"] == (
        "no_routing_match_after_exclusions"
    )
    assert diagnostics["dispatch"]["pre_dispatch"]["step_count"] == 2
    assert diagnostics["dispatch"]["pre_dispatch"]["total_duration_ms"] == 18
    assert diagnostics["dispatch"]["pre_dispatch"]["slowest_step_id"] == (
        "selector_candidate_preparation"
    )
    assert diagnostics["dispatch"]["turn_contract_check"]["status"] == (
        "override_required"
    )
    assert diagnostics["dispatch"]["turn_contract_check"]["selected_workflow_id"] == (
        "#V#meeting_invitation_testing_workflow"
    )
    assert (
        diagnostics["dispatch"]["turn_contract_check"][
            "selected_workflow_can_satisfy_contract"
        ]
        is False
    )
    assert diagnostics["dispatch"]["turn_contract_check"]["required_tools"] == [
        "search_knowledge_base",
        "search_concepts",
        "search_web",
    ]
    assert diagnostics["dispatch"]["turn_contract_check"][
        "required_surface_families"
    ] == [
        "knowledge_base",
        "web",
    ]
    assert diagnostics["dispatch"]["turn_contract_check"][
        "external_surface_families"
    ] == ["web"]
    assert diagnostics["dispatch"]["turn_contract_check"]["override_reason"] == (
        "selected_custom_workflow_cannot_satisfy_multi_surface_turn_contract"
    )
    assert diagnostics["dispatch"]["turn_contract_check"][
        "turn_expected_outcome_contract"
    ] == {
        "success_target": "Grounded meeting invitation test plan.",
        "selector_guidance": (
            "Use represented meeting context and live web confirmation."
        ),
    }
    assert diagnostics["dispatch"]["selected_execution_mode"] == "tool_pipeline"
    assert diagnostics["dispatch"]["contract_resolution_status"] == "resolved"
    assert diagnostics["dispatch"]["failure_codes"] == ["tool_dispatch_not_started"]
    assert diagnostics["dispatch"]["zero_execution_primary_failure_code"] == (
        "tool_dispatch_not_started"
    )
    assert diagnostics["dispatch"]["last_successful_boundary"] == "contract_resolution"


def test_build_workflow_routing_diagnostics_preserves_local_handoff_failure_details() -> (
    None
):
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery={
            "query": "Run the testing workflow",
            "matches": [],
            "candidates": [],
        },
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "execution_mode_selected",
                "status": "selected",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "contract_resolution",
                "status": "resolved",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_handoff",
                "status": "failed",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
                "reason": "tool_pipeline_setup_exception",
                "error_class": "RuntimeError",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_terminal",
                "status": "failed",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
                "reason": "tool_pipeline_setup_exception",
                "error_class": "RuntimeError",
                "completed": False,
            },
        ],
        execution_summary={
            "tool_route_selected": True,
            "selected_execution_mode": "tool_pipeline",
            "dispatch_workflow_id": "#V#tool_calling_workflow",
            "dispatch_event_count": 4,
            "contract_resolution_status": "resolved",
            "workflow_handoff_started": False,
            "workflow_handoff_failure_reason": "tool_pipeline_setup_exception",
            "workflow_handoff_failure_error_class": "RuntimeError",
            "dispatch_terminal_status": "failed",
            "dispatch_terminal_final_state": None,
            "dispatch_terminal_completed": False,
            "dispatch_terminal_failure_reason": "tool_pipeline_setup_exception",
            "dispatch_terminal_failure_error_class": "RuntimeError",
            "planned_count": 1,
            "started_count": 0,
            "executed_count": 0,
            "zero_tools_executed": True,
            "failure_codes": [
                "tool_pipeline_setup_exception",
                "tool_dispatch_not_started",
            ],
            "last_successful_boundary": "workflow_terminal",
        },
    )

    assert diagnostics["dispatch"]["workflow_handoff_failure_reason"] == (
        "tool_pipeline_setup_exception"
    )
    assert diagnostics["dispatch"]["workflow_handoff_failure_error_class"] == (
        "RuntimeError"
    )
    assert diagnostics["dispatch"]["dispatch_terminal_failure_reason"] == (
        "tool_pipeline_setup_exception"
    )
    assert diagnostics["dispatch"]["zero_execution_primary_failure_code"] == (
        "tool_pipeline_setup_exception"
    )


def test_build_workflow_routing_diagnostics_derives_capability_index_timeout_cause() -> (
    None
):
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery={
            "query": "Download and represent https://arxiv.org/abs/2411.04983",
            "matches": [],
            "candidates": [],
            "errors": [
                "capability_index_wait_timed_out",
                "capability_index_build_in_progress",
            ],
        },
        workflow_routing={},
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[],
        execution_summary={},
    )

    assert diagnostics["discovery"]["match_absence_reason"] == (
        "capability_index_wait_timed_out_build_in_progress"
    )
    assert diagnostics["discovery"]["errors"] == [
        "capability_index_wait_timed_out",
        "capability_index_build_in_progress",
    ]


def test_build_turn_execution_correctness_summary_marks_successful_completion() -> None:
    summary = build_turn_execution_correctness_summary(
        completion_gate={
            "decision": "completed",
            "decision_reason": "No blocking effect detected.",
            "safe_to_claim_completion": True,
            "requires_follow_up": False,
        },
        required_effects=[],
        critic_summary={"not_verified_count": 0, "inconclusive_count": 0},
        final_response={
            "completion_claim_detected": False,
            "completion_claim_validated": True,
        },
        workflow_selection={
            "selected_workflow_id": "#V#chat_assistant_workflow",
            "selector_verdict": "plain_response",
            "selector_source": "default",
        },
        workflow_routing_diagnostics={},
    )

    assert summary["schema_version"] == TURN_EXECUTION_CORRECTNESS_SCHEMA_VERSION
    assert summary["overall_outcome"] == "successful_completion"
    assert summary["failure_mode"] == "completed_verified"
    assert summary["likely_failure_to_act"] is False
    assert summary["metric_labels"]["successful_completion"] is True
    assert summary["metric_labels"]["false_success"] is False


def test_build_turn_execution_correctness_summary_marks_plain_response_misrouting() -> (
    None
):
    summary = build_turn_execution_correctness_summary(
        completion_gate={
            "decision": "escalation_required",
            "decision_reason": "Required mutation was not executed.",
            "safe_to_claim_completion": False,
            "requires_follow_up": True,
        },
        required_effects=[{"effect_id": "effect_1", "status": "not_executed"}],
        critic_summary={"not_verified_count": 1},
        final_response={
            "completion_claim_detected": True,
            "completion_claim_validated": False,
        },
        workflow_selection={
            "selected_workflow_id": "#V#chat_assistant_workflow",
            "selector_verdict": "plain_response",
            "selector_source": "default",
        },
        workflow_routing_diagnostics={},
    )

    assert summary["schema_version"] == TURN_EXECUTION_CORRECTNESS_SCHEMA_VERSION
    assert summary["failure_mode"] == "mutation_not_executed"
    assert summary["overall_outcome"] == "tool_or_workflow_misrouting"
    assert summary["likely_failure_to_act"] is True
    assert summary["metric_labels"]["unresolved_follow_up_needed"] is True
    assert summary["metric_labels"]["tool_or_workflow_misrouting"] is True
    assert summary["selection_labels"]["plain_response_route_selected"] is True


def test_build_turn_execution_correctness_summary_marks_launchability_fallback_misrouting() -> (
    None
):
    summary = build_turn_execution_correctness_summary(
        completion_gate={
            "decision": "escalation_required",
            "decision_reason": "Selected workflow was not executed.",
            "safe_to_claim_completion": False,
            "requires_follow_up": True,
        },
        required_effects=[{"effect_id": "effect_1", "status": "not_executed"}],
        critic_summary={"not_verified_count": 1},
        final_response={
            "completion_claim_detected": True,
            "completion_claim_validated": False,
        },
        workflow_selection={
            "selected_workflow_id": "#V#tool_calling_workflow",
            "selector_verdict": "tool_contract_override",
            "selector_source": "selector_override",
        },
        workflow_routing_diagnostics={
            "selector": {
                "override_events": [
                    {
                        "reason": "selected_custom_workflow_launchability_requires_safe_general_fallback",
                        "prior_selected_workflow_id": "#V#arxiv_paper_representation_workflow",
                        "selected_workflow_id": "#V#tool_calling_workflow",
                        "custom_workflow_override_reason": "no_custom_workflow_candidates",
                        "launch_viability_probe": {
                            "prior_selected_workflow": {"launchable": False}
                        },
                    }
                ]
            }
        },
    )

    assert summary["failure_mode"] == "mutation_not_executed"
    assert summary["overall_outcome"] == "tool_or_workflow_misrouting"
    assert summary["likely_failure_to_act"] is True
    assert summary["metric_labels"]["tool_or_workflow_misrouting"] is True
    assert summary["selection_labels"]["tool_route_selected"] is True
    assert summary["selection_labels"]["launchability_degraded_tool_route"] is True


def test_build_turn_execution_correctness_summary_marks_submission_failure_false_success() -> (
    None
):
    summary = build_turn_execution_correctness_summary(
        completion_gate={
            "decision": "completed",
            "decision_reason": "No blocking effect detected.",
            "safe_to_claim_completion": True,
            "requires_follow_up": False,
        },
        required_effects=[],
        critic_summary={"not_verified_count": 0, "inconclusive_count": 0},
        final_response={
            "completion_claim_detected": True,
            "completion_claim_validated": True,
        },
        workflow_selection={
            "selected_workflow_id": "#V#conversation_turn_execution_workflow",
            "selector_verdict": "rag_selected",
            "selector_source": "workflow_owned",
        },
        workflow_routing_diagnostics={
            "dispatch": {
                "selected_execution_mode": "custom_workflow",
                "dispatch_workflow_id": "#V#conversation_turn_execution_workflow",
                "dispatch_terminal_status": "failed",
                "dispatch_terminal_failure_reason": "workflow_not_runnable",
                "dispatch_terminal_failure_detail": (
                    "Workflow submission failed before any tool or workflow execution."
                ),
                "failure_codes": ["workflow_not_runnable"],
            }
        },
    )

    assert summary["failure_mode"] == "false_completion_gate_state"
    assert summary["overall_outcome"] == "false_success"
    assert summary["metric_labels"]["false_success"] is True


def test_build_turn_execution_correctness_summary_marks_workflow_llm_timeout_false_success() -> (
    None
):
    summary = build_turn_execution_correctness_summary(
        completion_gate={
            "decision": "completed",
            "decision_reason": "No blocking effect detected.",
            "safe_to_claim_completion": True,
            "requires_follow_up": False,
        },
        required_effects=[],
        critic_summary={"not_verified_count": 0, "inconclusive_count": 0},
        final_response={
            "completion_claim_detected": True,
            "completion_claim_validated": True,
        },
        workflow_selection={
            "selected_workflow_id": "#V#conversation_turn_execution_workflow",
            "selector_verdict": "rag_selected",
            "selector_source": "workflow_owned",
        },
        workflow_routing_diagnostics={
            "dispatch": {
                "selected_execution_mode": "custom_workflow",
                "dispatch_workflow_id": "#V#conversation_turn_execution_workflow",
                "failure_codes": ["workflow_llm_step_timeout"],
                "dispatch_terminal_failure_detail": (
                    "LLM call timed out before workflow execution evidence was available."
                ),
            }
        },
    )

    assert summary["failure_mode"] == "false_completion_gate_state"
    assert summary["overall_outcome"] == "false_success"
    assert summary["metric_labels"]["false_success"] is True


def test_turn_record_fails_closed_on_nested_pre_dispatch_subworkflow_timeout() -> None:
    record = build_turn_execution_record(
        request_id="req-2494-timeout",
        session_id="session-2494-timeout",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Find the latest unrepresented paper in recent email.",
        response_text="",
        interaction_timestamp_utc="2026-07-15T00:00:00Z",
        workflow_failure_evidence={
            "last_action_failed": True,
            "last_action_error": (
                "subworkflow_failed:#V#turn_context_adjudication_workflow:"
                "workflow_llm_step_timeout:context_adjudication"
            ),
            "last_failed_action_outputs": {
                "subworkflow_invocation": {
                    "child_workflow_id": "#V#turn_context_adjudication_workflow",
                    "child_completed": False,
                    "child_error": (
                        "workflow_llm_step_timeout:LLM call timed out after 45s"
                    ),
                }
            },
        },
    )

    dispatch = record["workflow_routing_diagnostics"]["dispatch"]
    assert "workflow_llm_step_timeout" in dispatch["failure_codes"]
    assert "subworkflow_failed" in dispatch["failure_codes"]
    assert dispatch["dispatch_terminal_status"] == "failed"
    assert record["completion_gate"]["safe_to_claim_completion"] is False
    assert record["completion_gate"]["requires_follow_up"] is True
    assert (
        record["execution_correctness"]["metric_labels"]["successful_completion"]
        is False
    )


def test_turn_record_fails_closed_on_terminal_workflow_use_episode() -> None:
    record = build_turn_execution_record(
        request_id="req-2494-provider-failure",
        session_id="session-2494-provider-failure",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Retrieve represented information about an organisation.",
        response_text="The authoritative workflow failed before discovery.",
        interaction_timestamp_utc="2026-07-15T00:00:00Z",
        workflow_failure_evidence={
            "workflow_use_episodes": [
                {
                    "workflow_id": "#V#conversation_turn_execution_workflow",
                    "source": "conversation_turn_supervised",
                    "completed": False,
                    "final_state": "expected_outcome_inference",
                    "termination_reason": {
                        "code": "provider_unavailable",
                        "detail": "All represented model-policy candidates failed.",
                    },
                }
            ]
        },
    )

    dispatch = record["workflow_routing_diagnostics"]["dispatch"]
    assert "workflow_execution_failed" in dispatch["failure_codes"]
    assert dispatch["dispatch_terminal_status"] == "failed"
    assert dispatch["dispatch_terminal_failure_detail"] == (
        "provider_unavailable: All represented model-policy candidates failed."
    )
    assert record["completion_gate"]["safe_to_claim_completion"] is False
    assert record["completion_gate"]["requires_follow_up"] is True
    assert (
        record["execution_correctness"]["metric_labels"]["successful_completion"]
        is False
    )


def test_build_turn_execution_correctness_summary_marks_missing_custom_dispatch_false_success() -> (
    None
):
    summary = build_turn_execution_correctness_summary(
        completion_gate={
            "decision": "completed",
            "decision_reason": "No blocking effect detected.",
            "safe_to_claim_completion": True,
            "requires_follow_up": False,
        },
        required_effects=[],
        critic_summary={"not_verified_count": 0, "inconclusive_count": 0},
        final_response={
            "completion_claim_detected": True,
            "completion_claim_validated": True,
        },
        workflow_selection={
            "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
            "selector_verdict": "rag_selected",
            "selector_source": "selector",
        },
        workflow_routing_diagnostics={
            "dispatch": {
                "dispatch_event_count": 0,
                "workflow_handoff_started": False,
            }
        },
    )

    assert summary["failure_mode"] == "false_completion_gate_state"
    assert summary["overall_outcome"] == "false_success"
    assert summary["metric_labels"]["successful_completion"] is False
    assert summary["metric_labels"]["false_success"] is True


def test_build_turn_execution_correctness_summary_marks_answer_evidence_contradiction_false_success() -> (
    None
):
    summary = build_turn_execution_correctness_summary(
        completion_gate={
            "decision": "partial",
            "decision_reason": (
                "Required evidence retrieval returned positive results, but the "
                "answer remained a count-only result summary."
            ),
            "safe_to_claim_completion": False,
            "requires_follow_up": True,
            "blocking_failure_codes": [
                "prompt_required_evidence_positive_results_contradict_low_information_answer"
            ],
            "evidence_payload": {
                "required_evidence_answer_consistency_blocker": {
                    "effect_type": "required_evidence_answer_consistency",
                    "response_surface_kind": "count_only_result_summary",
                }
            },
        },
        required_effects=[],
        critic_summary={"not_verified_count": 0, "inconclusive_count": 0},
        final_response={
            "completion_claim_detected": False,
            "completion_claim_validated": True,
        },
        workflow_selection={
            "selected_workflow_id": "#V#tool_calling_workflow",
            "selector_verdict": "tool_seeking",
            "selector_source": "selector",
        },
        workflow_routing_diagnostics={},
    )

    assert summary["failure_mode"] == "false_completion_claim"
    assert summary["overall_outcome"] == "false_success"
    assert summary["likely_failure_to_act"] is True
    assert summary["metric_labels"]["false_success"] is True
    assert (
        summary["gate_labels"]["required_evidence_answer_consistency_blocked"] is True
    )


def test_turn_execution_record_rejects_search_only_kr_required_tool_run() -> None:
    invocations = [{"tool": "search_concepts", "status": "ok"}]
    ledger = build_required_tool_obligation_ledger(
        required_tools_by_source={"turn_expected_outcome_contract": _KR_REQUIRED_TOOLS},
        invocations=invocations,
        allowed_tools=_KR_REQUIRED_TOOLS,
        method_catalogue={tool_name: {} for tool_name in _KR_REQUIRED_TOOLS},
        max_tool_invocations=1,
    )

    record = build_turn_execution_record(
        request_id="req-kr-search-only",
        session_id="session-1",
        namespace="#V#user@test",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Represent these labels in the Vontology.",
        response_text="Done.",
        interaction_timestamp_utc="2026-05-02T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
        },
        tool_invocations=invocations,
        turn_expected_outcome_contract={"required_tools": _KR_REQUIRED_TOOLS},
        required_tool_obligation_ledger=ledger,
    )

    gate = record["completion_gate"]
    summary = record["execution"]["summary"]
    assert gate["safe_to_claim_completion"] is False
    assert (
        BLOCKER_TOOL_BUDGET_EXHAUSTED_BEFORE_REQUIRED_TOOLS
        in gate["blocking_failure_codes"]
    )
    assert (
        BLOCKER_TOOL_BUDGET_EXHAUSTED_BEFORE_REQUIRED_TOOLS
        in summary["required_tool_obligation_blocking_failure_codes"]
    )
    lineage = record["requested_evidence_lineage"]
    assert lineage["turn_expected_required_tools"] == _KR_REQUIRED_TOOLS
    assert lineage["unresolved_resolver_chains"]
    assert any(
        chain.get("effect_type") == "tool_execution"
        and BLOCKER_TOOL_BUDGET_EXHAUSTED_BEFORE_REQUIRED_TOOLS
        in (chain.get("failure_codes") or [])
        for chain in lineage["unresolved_resolver_chains"]
    )
    assert summary["required_tool_obligations"]["unsatisfied_required_tools"] == [
        "create_concepts",
        "add_relationship",
        "upsert_singleton_text_relation",
        "fetch_concept",
        "get_text_relations_summary",
    ]


def test_turn_execution_record_preserves_required_tool_allowed_policy_blocker() -> None:
    required_tools = ["search_concepts", "create_concepts"]
    ledger = build_required_tool_obligation_ledger(
        required_tools_by_source={"turn_expected_outcome_contract": required_tools},
        invocations=[],
        allowed_tools=["search_concepts"],
        method_catalogue={tool_name: {} for tool_name in required_tools},
    )

    record = build_turn_execution_record(
        request_id="req-kr-unallowed",
        session_id="session-1",
        namespace="#V#user@test",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Represent these labels in the Vontology.",
        response_text="I could not write.",
        interaction_timestamp_utc="2026-05-02T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
        },
        tool_invocations=[],
        turn_expected_outcome_contract={"required_tools": required_tools},
        required_tool_obligation_ledger=ledger,
    )

    gate = record["completion_gate"]
    diagnostics = record["workflow_routing_diagnostics"]
    assert gate["safe_to_claim_completion"] is False
    assert (
        BLOCKER_CONTRACT_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY
        in gate["blocking_failure_codes"]
    )
    assert (
        BLOCKER_CONTRACT_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY
        in diagnostics["dispatch"]["required_tool_obligation_blocking_failure_codes"]
    )


def test_turn_execution_record_projects_required_write_payload_validation_blocker() -> (
    None
):
    message = "create_concepts: Missing required field 'parent_id'."

    record = build_turn_execution_record(
        request_id="req-kr-invalid-write",
        session_id="session-1",
        namespace="#V#user@test",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Represent these labels in the Vontology.",
        response_text="The write payload was invalid.",
        interaction_timestamp_utc="2026-05-02T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
        },
        tool_invocations=[],
        turn_expected_outcome_contract={
            "required_tools": ["create_concepts", "fetch_concept"]
        },
        aux_llm_calls=[
            {
                "type": "tool_contract_attempt",
                "stage": "tool_calling.validate",
                "tool_calls": [
                    {
                        "tool": "create_concepts",
                        "payload": {"concepts": [{"name": "Reusable marker"}]},
                    }
                ],
                "validation_errors": [message],
                "diagnostics": [
                    {
                        "tool": "create_concepts",
                        "error_code": "schema_validation_failed",
                        "message": message,
                        "payload": {
                            "concepts": [{"name": "Reusable marker"}],
                        },
                    }
                ],
            }
        ],
    )

    summary = record["execution"]["summary"]
    ledger = summary["required_tool_obligations"]
    create_obligation = next(
        obligation
        for obligation in ledger["obligations"]
        if obligation["tool_name"] == "create_concepts"
    )
    assert create_obligation["blocking_reason"] == (
        BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED
    )
    assert create_obligation["last_attempt_status"] == "schema_validation_failed"
    assert create_obligation["last_attempt_message"] == message
    assert create_obligation["tool_call_validation_errors"][0]["message"] == message
    assert (
        BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED
        in (summary["required_tool_obligation_blocking_failure_codes"])
    )
    assert (
        BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED
        in (record["completion_gate"]["blocking_failure_codes"])
    )
    assert (
        BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED
        in (
            record["workflow_routing_diagnostics"]["dispatch"][
                "required_tool_obligation_blocking_failure_codes"
            ]
        )
    )


def test_turn_execution_record_counts_selected_workflow_action_for_required_tool(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service as metadata_service
    from src.backend.services.tool_metadata_service import ToolMetadata

    monkeypatch.setattr(
        metadata_service,
        "_load_from_vontology",
        lambda: {
            "get_paper_metadata": ToolMetadata(
                tool_name="get_paper_metadata",
                operation_category="read",
                evidence_role="verification",
            )
        },
    )
    metadata_service.invalidate_cache()
    try:
        record = build_turn_execution_record(
            request_id="req-workflow-action-required-tool",
            session_id="session-1",
            namespace="#V#user@test",
            user_id="#V#user",
            org_id="#V#org",
            prompt_text="Represent this paper: https://arxiv.org/abs/2106.03245",
            response_text="Paper concept: #V#paper.",
            interaction_timestamp_utc="2026-06-07T00:00:00Z",
            workflow_routing={
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "verdict": "rag_selected",
                "source": "selector",
            },
            tool_invocations=[],
            selected_workflow_trace={
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
                "child_workflow_completed": True,
                "child_workflow_final_state": "completed",
                "workflow_execution_summary": {
                    "schema_version": "workflow_execution_summary.v1",
                    "workflow_id": "#V#arxiv_paper_representation_workflow",
                    "completed": True,
                    "terminal_status": "completed",
                    "final_state": "completed",
                    "step_result_envelope_count": 1,
                    "action_started_count": 1,
                    "action_completed_count": 1,
                    "action_success_count": 1,
                    "action_failure_count": 0,
                    "action_unknown_count": 0,
                    "successful_action_ids": ["get_paper_metadata"],
                    "failed_action_ids": [],
                    "action_observations": [
                        {
                            "action_id": "get_paper_metadata",
                            "state_id": "fetch_arxiv_metadata",
                            "action_status": "success",
                            "outcome": "success",
                        }
                    ],
                    "runtime_event_count": 0,
                    "terminal_effect_count": 0,
                    "terminal_effects": [],
                    "durable_side_effect_count": 0,
                    "durable_side_effects": [],
                },
            },
            turn_expected_outcome_contract={
                "required_tools": ["get_paper_metadata"],
            },
            method_catalogue={"get_paper_metadata": {}},
        )
    finally:
        metadata_service.invalidate_cache()

    summary = record["execution"]["summary"]
    ledger = summary["required_tool_obligations"]
    obligation = ledger["obligations"][0]
    assert ledger["satisfied_count"] == 1
    assert ledger["unsatisfied_count"] == 0
    assert ledger["unsatisfied_required_tools"] == []
    assert obligation["tool_name"] == "get_paper_metadata"
    assert obligation["successful_count"] == 1
    assert obligation["blocking_reason"] == ""
    assert obligation["execution_surfaces"][0]["source"] == (
        "workflow_action_execution"
    )
    assert obligation["execution_surfaces"][0]["state_id"] == "fetch_arxiv_metadata"
    assert summary["required_tool_obligation_blocking_failure_codes"] == []


def test_turn_execution_record_preserves_failed_workflow_action_recovery_evidence(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service as metadata_service
    from src.backend.services.tool_metadata_service import ToolMetadata

    monkeypatch.setattr(
        metadata_service,
        "_load_from_vontology",
        lambda: {
            "import_url_file_copy": ToolMetadata(
                tool_name="import_url_file_copy",
                operation_category="write",
                evidence_role="mutation",
            )
        },
    )
    metadata_service.invalidate_cache()
    try:
        record = build_turn_execution_record(
            request_id="req-workflow-action-failed-required-tool",
            session_id="session-1",
            namespace="#V#user@test",
            user_id="#V#user",
            org_id="#V#org",
            prompt_text="Represent this paper: https://arxiv.org/abs/2106.03245",
            response_text="The paper download completed but registration timed out.",
            interaction_timestamp_utc="2026-06-07T00:00:00Z",
            workflow_routing={
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "verdict": "rag_selected",
                "source": "selector",
            },
            tool_invocations=[],
            selected_workflow_trace={
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
                "child_workflow_completed": False,
                "child_workflow_final_state": "failed",
                "workflow_execution_summary": {
                    "schema_version": "workflow_execution_summary.v1",
                    "workflow_id": "#V#arxiv_paper_representation_workflow",
                    "completed": False,
                    "terminal_status": "failed",
                    "final_state": "failed",
                    "step_result_envelope_count": 1,
                    "action_started_count": 1,
                    "action_completed_count": 1,
                    "action_success_count": 0,
                    "action_failure_count": 1,
                    "action_unknown_count": 0,
                    "successful_action_ids": [],
                    "failed_action_ids": ["import_url_file_copy"],
                    "action_observations": [
                        {
                            "action_id": "import_url_file_copy",
                            "state_id": "download_paper",
                            "action_status": "failed",
                            "outcome": "failed",
                            "error": "Registration exceeded the phase timeout.",
                            "error_code": "remote_file_copy_timeout",
                            "timeout_phase": "file_copy_registration",
                            "workflow_instance_id": "#V#wf_instance_1",
                            "execution_id": "trace-1",
                        }
                    ],
                    "runtime_event_count": 0,
                    "terminal_effect_count": 0,
                    "terminal_effects": [],
                    "durable_side_effect_count": 0,
                    "durable_side_effects": [],
                },
            },
            turn_expected_outcome_contract={
                "required_tools": ["import_url_file_copy"],
            },
            method_catalogue={"import_url_file_copy": {}},
        )
    finally:
        metadata_service.invalidate_cache()

    summary = record["execution"]["summary"]
    ledger = summary["required_tool_obligations"]
    obligation = ledger["obligations"][0]
    assert obligation["tool_name"] == "import_url_file_copy"
    assert obligation["attempted_count"] == 1
    assert obligation["successful_count"] == 0
    assert obligation["blocking_reason"] == BLOCKER_REQUIRED_TOOL_ATTEMPT_FAILED
    surface = obligation["execution_surfaces"][0]
    assert surface["source"] == "workflow_action_execution"
    assert surface["error_code"] == "remote_file_copy_timeout"
    assert surface["timeout_phase"] == "file_copy_registration"
    assert surface["workflow_instance_id"] == "#V#wf_instance_1"
    assert "import_url_file_copy" in summary["execution_surface_failed_tool_names"]
    assert (
        BLOCKER_REQUIRED_TOOL_ATTEMPT_FAILED
        in (summary["required_tool_obligation_blocking_failure_codes"])
    )
