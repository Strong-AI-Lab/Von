from src.backend.services.required_tool_obligation_service import (
    BLOCKER_CONTRACT_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY,
    BLOCKER_MUTATION_SUCCEEDED_READBACK_MISSING,
    BLOCKER_READBACK_ATTEMPTED_BUT_NOT_VERIFIED,
    BLOCKER_REQUIRED_TOOL_NOT_PLANNED,
    BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED,
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


def test_schema_validation_failure_marks_required_mutation_payload_unresolved() -> (
    None
):
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
    assert (
        BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED
        in ledger["blocking_failure_codes"]
    )


def test_absent_required_mutation_still_reports_not_planned() -> None:
    ledger = build_required_tool_obligation_ledger(
        required_tools_by_source={"turn_expected_outcome_contract": ["create_concepts"]},
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
