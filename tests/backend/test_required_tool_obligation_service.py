from src.backend.services.required_tool_obligation_service import (
    BLOCKER_CONTRACT_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY,
    BLOCKER_MUTATION_SUCCEEDED_READBACK_MISSING,
    BLOCKER_READBACK_ATTEMPTED_BUT_NOT_VERIFIED,
    BLOCKER_REQUIRED_TOOL_METADATA_MISSING,
    BLOCKER_REQUIRED_TOOL_NOT_PLANNED,
    BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED,
    BLOCKER_TARGET_REQUIRED_TOOL_ATTEMPT_FAILED,
    BLOCKER_TOOL_BUDGET_EXHAUSTED_BEFORE_REQUIRED_TOOLS,
    build_required_tool_obligation_ledger,
    required_tool_obligation_effect,
)


_KR_REQUIRED_TOOLS = [
    "search_concepts",
    "create_concepts",
    "add_relationship",
    "upsert_singleton_text_relation",
    "fetch_concept",
    "get_text_relations_summary",
]


def _catalogue(tool_names: list[str]) -> dict[str, dict]:
    return {tool_name: {} for tool_name in tool_names}


def _obligation_for_tool(ledger: dict, tool_name: str) -> dict:
    for obligation in ledger["obligations"]:
        if obligation["tool_name"] == tool_name:
            return obligation
    raise AssertionError(f"missing obligation for {tool_name}")


def test_required_tool_operation_and_target_closure_use_represented_metadata(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service as metadata_service
    from src.backend.services.tool_metadata_service import ToolMetadata

    monkeypatch.setattr(
        metadata_service,
        "_load_from_vontology",
        lambda: {
            "semantic_lookup": ToolMetadata(
                tool_name="semantic_lookup",
                operation_category="read",
                evidence_role="verification",
                target_concept_argument_name="focal_entity",
            ),
            "semantic_mutator": ToolMetadata(
                tool_name="semantic_mutator",
                operation_category="write",
            ),
        },
    )
    metadata_service.invalidate_cache()
    try:
        ledger = build_required_tool_obligation_ledger(
            required_tools_by_source={
                "represented_contract": ["semantic_lookup", "semantic_mutator"]
            },
            invocations=[
                {
                    "tool": "semantic_lookup",
                    "status": "error",
                    "arguments": {"focal_entity": "#V#target_a"},
                    "payload": {"success": False, "error": "not found"},
                },
                {
                    "tool": "semantic_lookup",
                    "status": "ok",
                    "arguments": {"focal_entity": "#V#target_b"},
                    "payload": {"success": True, "focal_entity": "#V#target_b"},
                },
            ],
            planned_tool_calls=[
                {
                    "tool": "semantic_mutator",
                    "payload": {"name": "Missing required payload"},
                }
            ],
            tool_call_validation_errors=[
                {
                    "tool": "semantic_mutator",
                    "error_code": "schema_validation_failed",
                    "message": "semantic_mutator payload unresolved.",
                }
            ],
            allowed_tools=["semantic_lookup", "semantic_mutator"],
            method_catalogue=_catalogue(["semantic_lookup", "semantic_mutator"]),
        )
    finally:
        metadata_service.invalidate_cache()

    lookup_obligation = _obligation_for_tool(ledger, "semantic_lookup")
    assert lookup_obligation["operation_class"] == "verification_read"
    assert lookup_obligation["operation_metadata_present"] is True
    assert lookup_obligation["target_closure"]["unresolved_failed_targets"] == [
        "#V#target_a"
    ]
    assert lookup_obligation["blocking_reason"] == (
        BLOCKER_TARGET_REQUIRED_TOOL_ATTEMPT_FAILED
    )

    mutator_obligation = _obligation_for_tool(ledger, "semantic_mutator")
    assert mutator_obligation["operation_class"] == "mutation_write"
    assert mutator_obligation["blocking_reason"] == (
        BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED
    )


def test_prefix_like_required_tool_without_metadata_fails_closed(monkeypatch) -> None:
    from src.backend.services import tool_metadata_service as metadata_service

    monkeypatch.setattr(metadata_service, "_load_from_vontology", lambda: {})
    metadata_service.invalidate_cache()
    try:
        ledger = build_required_tool_obligation_ledger(
            required_tools_by_source={
                "represented_contract": ["search_unrepresented_probe"]
            },
            invocations=[
                {
                    "tool": "search_unrepresented_probe",
                    "status": "ok",
                    "arguments": {"concept_id": "#V#target_a"},
                    "payload": {"success": True, "concept_id": "#V#target_a"},
                }
            ],
            allowed_tools=["search_unrepresented_probe"],
            method_catalogue=_catalogue(["search_unrepresented_probe"]),
        )
    finally:
        metadata_service.invalidate_cache()

    obligation = _obligation_for_tool(ledger, "search_unrepresented_probe")
    assert obligation["successful_count"] == 1
    assert obligation["operation_class"] == "external_side_effect"
    assert obligation["operation_metadata_present"] is False
    assert obligation["satisfied"] is False
    assert obligation["blocking_reason"] == BLOCKER_REQUIRED_TOOL_METADATA_MISSING
    assert BLOCKER_REQUIRED_TOOL_METADATA_MISSING in ledger["blocking_failure_codes"]


def test_schema_validation_failure_marks_required_mutation_payload_unresolved() -> None:
    message = "create_concepts: Missing required field 'parent_id'."

    ledger = build_required_tool_obligation_ledger(
        required_tools_by_source={
            "turn_expected_outcome_contract": ["create_concepts", "fetch_concept"]
        },
        planned_tool_calls=[
            {
                "tool": "create_concepts",
                "payload": {"concepts": [{"name": "Reusable marker"}]},
            }
        ],
        tool_call_validation_failure_context={
            "failures_by_tool": {
                "create_concepts": {
                    "tool": "create_concepts",
                    "errors": [
                        {
                            "tool": "create_concepts",
                            "error_code": "schema_validation_failed",
                            "message": message,
                        }
                    ],
                }
            }
        },
        allowed_tools=["create_concepts", "fetch_concept"],
        method_catalogue=_catalogue(["create_concepts", "fetch_concept"]),
    )

    create_obligation = _obligation_for_tool(ledger, "create_concepts")
    assert create_obligation["planned_count"] == 1
    assert create_obligation["attempted_count"] == 1
    assert create_obligation["successful_count"] == 0
    assert create_obligation["last_attempt_status"] == "schema_validation_failed"
    assert create_obligation["last_attempt_message"] == message
    assert create_obligation["blocking_reason"] == (
        BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED
    )
    assert create_obligation["failure_class"] == (
        BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED
    )
    assert create_obligation["tool_call_validation_errors"][0]["message"] == message
    assert BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED in ledger["blocking_failure_codes"]


def test_absent_required_mutation_still_reports_not_planned() -> None:
    ledger = build_required_tool_obligation_ledger(
        required_tools_by_source={
            "turn_expected_outcome_contract": ["create_concepts"]
        },
        invocations=[],
        allowed_tools=["create_concepts"],
        method_catalogue=_catalogue(["create_concepts"]),
    )

    create_obligation = _obligation_for_tool(ledger, "create_concepts")
    assert create_obligation["planned_count"] == 0
    assert create_obligation["attempted_count"] == 0
    assert create_obligation["blocking_reason"] == BLOCKER_REQUIRED_TOOL_NOT_PLANNED
    assert ledger["blocking_failure_codes"] == [BLOCKER_REQUIRED_TOOL_NOT_PLANNED]


def test_search_only_budget_exhaustion_marks_unsatisfied_kr_writes_and_readback() -> (
    None
):
    ledger = build_required_tool_obligation_ledger(
        required_tools_by_source={"turn_expected_outcome_contract": _KR_REQUIRED_TOOLS},
        invocations=[{"tool": "search_concepts", "status": "ok"}],
        allowed_tools=_KR_REQUIRED_TOOLS,
        method_catalogue=_catalogue(_KR_REQUIRED_TOOLS),
        max_tool_invocations=1,
    )

    assert ledger["unsatisfied_count"] == 5
    assert (
        BLOCKER_TOOL_BUDGET_EXHAUSTED_BEFORE_REQUIRED_TOOLS
        in ledger["blocking_failure_codes"]
    )
    assert _obligation_for_tool(ledger, "create_concepts")["blocking_reason"] == (
        BLOCKER_TOOL_BUDGET_EXHAUSTED_BEFORE_REQUIRED_TOOLS
    )


def test_allowed_tool_exclusion_stays_visible_as_typed_obligation_blocker() -> None:
    ledger = build_required_tool_obligation_ledger(
        required_tools_by_source={
            "turn_expected_outcome_contract": ["search_concepts", "create_concepts"]
        },
        invocations=[],
        allowed_tools=["search_concepts"],
        method_catalogue=_catalogue(["search_concepts", "create_concepts"]),
    )

    create_obligation = _obligation_for_tool(ledger, "create_concepts")
    assert create_obligation["allowed_by_workflow_policy"] is False
    assert create_obligation["blocking_reason"] == (
        BLOCKER_CONTRACT_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY
    )
    assert (
        BLOCKER_CONTRACT_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY
        in ledger["blocking_failure_codes"]
    )


def test_successful_mutation_requires_required_readback_to_satisfy_ledger() -> None:
    ledger = build_required_tool_obligation_ledger(
        required_tools_by_source={
            "turn_expected_outcome_contract": ["create_concepts", "fetch_concept"]
        },
        invocations=[{"tool": "create_concepts", "status": "ok"}],
        allowed_tools=["create_concepts", "fetch_concept"],
        method_catalogue=_catalogue(["create_concepts", "fetch_concept"]),
    )

    fetch_obligation = _obligation_for_tool(ledger, "fetch_concept")
    assert fetch_obligation["blocking_reason"] == (
        BLOCKER_MUTATION_SUCCEEDED_READBACK_MISSING
    )
    assert (
        BLOCKER_MUTATION_SUCCEEDED_READBACK_MISSING in ledger["blocking_failure_codes"]
    )


def test_failed_readback_after_mutation_gets_specific_not_verified_blocker() -> None:
    ledger = build_required_tool_obligation_ledger(
        required_tools_by_source={
            "turn_expected_outcome_contract": ["create_concepts", "fetch_concept"]
        },
        invocations=[
            {"tool": "create_concepts", "status": "ok"},
            {"tool": "fetch_concept", "status": "error", "error": "not found"},
        ],
        allowed_tools=["create_concepts", "fetch_concept"],
        method_catalogue=_catalogue(["create_concepts", "fetch_concept"]),
    )

    fetch_obligation = _obligation_for_tool(ledger, "fetch_concept")
    assert fetch_obligation["blocking_reason"] == (
        BLOCKER_READBACK_ATTEMPTED_BUT_NOT_VERIFIED
    )


def test_obligation_effect_is_absent_only_when_all_required_tools_satisfied() -> None:
    ledger = build_required_tool_obligation_ledger(
        required_tools_by_source={
            "turn_expected_outcome_contract": ["create_concepts", "fetch_concept"]
        },
        invocations=[
            {"tool": "create_concepts", "status": "ok"},
            {"tool": "fetch_concept", "status": "ok"},
        ],
        allowed_tools=["create_concepts", "fetch_concept"],
        method_catalogue=_catalogue(["create_concepts", "fetch_concept"]),
    )

    assert ledger["unsatisfied_count"] == 0
    assert required_tool_obligation_effect(ledger) is None


def test_verification_read_success_on_other_target_does_not_close_failed_target() -> (
    None
):
    ledger = build_required_tool_obligation_ledger(
        required_tools_by_source={"turn_expected_outcome_contract": ["fetch_concept"]},
        invocations=[
            {
                "tool": "fetch_concept",
                "status": "error",
                "arguments": {"concept_id": "#V#target_a"},
                "payload": {"success": False, "error": "not found"},
            },
            {
                "tool": "fetch_concept",
                "status": "ok",
                "arguments": {"concept_id": "#V#target_b"},
                "payload": {"success": True, "concept_id": "#V#target_b"},
            },
        ],
        allowed_tools=["fetch_concept"],
        method_catalogue=_catalogue(["fetch_concept"]),
    )

    fetch_obligation = _obligation_for_tool(ledger, "fetch_concept")
    assert fetch_obligation["attempted_count"] == 2
    assert fetch_obligation["successful_count"] == 1
    assert fetch_obligation["satisfied"] is False
    assert (
        fetch_obligation["blocking_reason"]
        == BLOCKER_TARGET_REQUIRED_TOOL_ATTEMPT_FAILED
    )
    assert fetch_obligation["target_closure"] == {
        "attempted_target_count": 2,
        "successful_target_count": 1,
        "failed_target_count": 1,
        "unresolved_failed_target_count": 1,
        "unresolved_failed_targets": ["#V#target_a"],
    }
    assert BLOCKER_TARGET_REQUIRED_TOOL_ATTEMPT_FAILED in (
        ledger["blocking_failure_codes"]
    )
