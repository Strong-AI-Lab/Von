from src.backend.services.turn_execution_record_service import (
    build_search_tool_evidence,
    build_turn_execution_record,
)


def test_build_search_tool_evidence_preserves_search_concepts_result() -> None:
    tool_invocations = [
        {
            "tool": "search_concepts",
            "payload": {
                "query": "under preparation paper",
                "include_hierarchy_path": True,
            },
            "effective_payload": {
                "success": True,
                "results": [
                    {
                        "concept_id": "#V#under_preparation_paper",
                        "name": "Under Preparation Paper",
                        "kind": "type",
                    }
                ],
                "total_count": 1,
            },
            "status": "ok",
            "result_summary": "Found 1 concept for 'under preparation paper'",
            "call_id": "call-search-1",
        }
    ]

    evidence = build_search_tool_evidence(tool_invocations)

    assert len(evidence) == 1
    assert evidence[0]["tool"] == "search_concepts"
    assert evidence[0]["status"] == "ok"
    assert evidence[0]["query"] == "under preparation paper"
    assert evidence[0]["arguments"]["include_hierarchy_path"] is True
    assert evidence[0]["result"]["total_count"] == 1
    assert evidence[0]["result"]["results"][0]["concept_id"] == "#V#under_preparation_paper"
    assert evidence[0]["result_truncated"] is False


def test_build_turn_execution_record_carries_search_evidence_into_execution_payload() -> None:
    tool_invocations = [
        {
            "tool": "search_concepts",
            "payload": {"query": "bathroom closet"},
            "effective_payload": {
                "success": True,
                "results": [{"concept_id": "#V#bathroom_closet", "name": "Bathroom Closet"}],
                "total_count": 1,
            },
            "status": "ok",
            "result_summary": "Found 1 concept for 'bathroom closet'",
        }
    ]

    record = build_turn_execution_record(
        request_id="req-search-1",
        session_id="session-search-1",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Search for bathroom closet",
        response_text="Found one result.",
        interaction_timestamp_utc="2026-03-29T04:57:00Z",
        workflow_discovery={"matches": []},
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
            "source": "selector_override",
        },
        tool_invocations=tool_invocations,
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 1, "tools_completed": 1},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[],
    )

    execution = record["execution"]
    assert execution["summary"]["search_evidence_count"] == 1
    assert execution["search_evidence"][0]["tool"] == "search_concepts"
    assert execution["search_evidence"][0]["arguments"]["query"] == "bathroom closet"
    assert execution["search_evidence"][0]["result"]["results"][0]["name"] == "Bathroom Closet"
