import json
from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from workflow_test_support import bootstrap_authoritative_conversation_turn_workflows


class _StubResult:
    def __init__(self, payload, duration_ms=1.0):
        self.payload = payload
        self.duration_ms = duration_ms


class _StubGateway:
    enabled = True

    def describe_methods(self):
        return {}

    def invoke(self, tool_name, payload=None):
        # Large payload to exercise truncation.
        return _StubResult({"content": "x" * 50_000, "ok": True})


class _CapturingLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate(self, prompt, *, context=None, model=None):
        self.calls.append({"prompt": prompt, "context": context, "model": model})
        if self._responses:
            return self._responses.pop(0)
        return "ok"


def _bootstrap_authoritative_workflows() -> None:
    report = bootstrap_authoritative_conversation_turn_workflows()
    assert not report.get("graph_publication_errors")


def _stub_base_system_prompt(orchestrator: InternalMCPChatOrchestrator) -> None:
    cast(Any, orchestrator)._load_base_system_prompt_from_vontology = lambda **_kwargs: (
        "You are Von.",
        "#V#test_base_system_prompt",
    )


def _total_context_chars(context):
    if not context:
        return 0
    total = 0
    for msg in context:
        content = msg.get("content")
        total += len(content) if isinstance(content, str) else len(str(content))
    return total


def test_orchestrator_limits_context_by_chars():
    _bootstrap_authoritative_workflows()
    gateway = cast(Any, _StubGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=1,
        max_context_chars=30_000,
    )
    _stub_base_system_prompt(orchestrator)

    # Create a very large chat history with unique contents so trimming is testable.
    context = [
        {"role": "user", "content": f"msg-{i}:" + ("a" * 2_000)} for i in range(100)
    ]

    llm = _CapturingLLM(["hello world"])
    result = orchestrator.run(prompt="hi", context=context, llm_client=llm, model=None)

    assert result.response_text == "hello world"
    assert llm.calls

    sent_context = llm.calls[0]["context"]
    assert sent_context and sent_context[0]["role"] == "system"

    # Should be trimmed well below the original 100 messages.
    assert len(sent_context) < len(context) + 1

    # Context budget is approximate because system message is always retained.
    assert _total_context_chars(sent_context) <= 30_000 + 20_000

    # The newest user message should be present; the oldest should be trimmed.
    assert sent_context[-1]["content"].startswith("msg-99:")
    # After trimming, the earliest preserved message index should be > 0.
    first_preserved = next(msg for msg in sent_context[1:] if msg.get("role") == "user")
    assert first_preserved["content"].startswith("msg-")
    first_idx = int(first_preserved["content"].split(":", 1)[0].split("-", 1)[1])
    assert first_idx > 0


def test_orchestrator_preserves_presenter_protocol_when_trimming_context():
    _bootstrap_authoritative_workflows()
    gateway = cast(Any, _StubGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=1,
        max_context_chars=8_000,
    )
    _stub_base_system_prompt(orchestrator)

    presenter_protocol = {
        "role": "system",
        "content": (
            "PRESENTER MODE PROTOCOL:\n"
            "- Output EXACTLY TWO tagged blocks and nothing else:\n"
            "  <spoken>...brief talk track...</spoken>\n"
            "  <screen>...full on-screen content...</screen>\n"
        ),
    }

    # Force trimming with many large messages; presenter protocol would normally
    # be at risk of being dropped if it were not merged into the retained system
    # instruction message.
    context = [presenter_protocol] + [
        {"role": "user", "content": f"msg-{i}:" + ("a" * 2_000)} for i in range(30)
    ]

    llm = _CapturingLLM(["ok"])
    result = orchestrator.run(prompt="hi", context=context, llm_client=llm, model=None)

    assert result.response_text == "ok"
    sent_context = llm.calls[0]["context"]
    assert sent_context and sent_context[0]["role"] == "system"
    assert "PRESENTER MODE PROTOCOL:" in sent_context[0]["content"]


def test_orchestrator_truncates_tool_payload_in_context():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=1,
        max_tool_result_chars=5_000,
        max_tool_result_field_chars=1_500,
        max_context_chars=80_000,
    )

    encoded = orchestrator._format_tool_result(
        "dummy",
        {"content": "x" * 50_000, "ok": True},
        1.0,
        "ok",
    )

    assert len(encoded) <= 5_500

    # Ensure the tool output is still valid JSON.
    parsed = json.loads(encoded)
    assert parsed["tool"] == "dummy"
    assert parsed["status"] == "ok"


def test_format_tool_result_shapes_search_concepts_payload_for_live_follow_up():
    _bootstrap_authoritative_workflows()
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=1,
        max_tool_result_chars=5_000,
        max_tool_result_field_chars=1_500,
        max_context_chars=80_000,
    )

    encoded = orchestrator._format_tool_result(
        "search_concepts",
        {
            "results": [
                {
                    "concept_id": "#V#under_preparation_paper",
                    "name": "Under Preparation Paper",
                    "kind": "type",
                    "relevance_score": 96.0,
                    "hierarchy": {
                        "primary_path": [
                            "#V#thing",
                            "#V#document",
                            "#V#paper",
                            "#V#under_preparation_paper",
                        ]
                    },
                },
                {
                    "concept_id": "#V#paper_draft",
                    "name": "Paper Draft",
                    "kind": "type",
                    "relevance_score": 88.0,
                },
                {
                    "concept_id": "#V#research_manuscript",
                    "name": "Research Manuscript",
                    "kind": "type",
                    "relevance_score": 82.0,
                },
                {
                    "concept_id": "#V#bathroom_closet",
                    "name": "Bathroom Closet",
                    "kind": "type",
                    "relevance_score": 51.0,
                },
                {
                    "concept_id": "#V#linen_cupboard",
                    "name": "Linen Cupboard",
                    "kind": "type",
                    "relevance_score": 50.0,
                },
            ],
            "total_count": 5,
            "match_types_used": ["substring"],
            "query_info": {
                "query": "under preparation paper",
                "match_type": "substring",
                "has_more": False,
            },
        },
        1.0,
        "ok",
    )

    parsed = json.loads(encoded)
    assert parsed["tool"] == "search_concepts"
    payload = parsed["payload"]
    assert payload["_llm_view"] == "search_concepts_results.v1"
    assert payload["query_info"]["query"] == "under preparation paper"
    assert payload["total_count"] == 5
    assert payload["returned_count"] == 5
    assert payload["omitted_low_signal_results"] == 2
    assert payload["retrieval_diagnostics"]["scope_only"] is False
    assert payload["retrieval_diagnostics"]["top_relevance_score"] == 96.0
    assert "omitted" in payload["retrieval_diagnostics"]["note"].lower()
    assert [item["name"] for item in payload["results"]] == [
        "Under Preparation Paper",
        "Paper Draft",
        "Research Manuscript",
    ]
    assert all(item["name"] != "Bathroom Closet" for item in payload["results"])


def test_format_tool_result_marks_search_concepts_payload_weak_when_matches_are_noisy():
    _bootstrap_authoritative_workflows()
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=1,
        max_tool_result_chars=5_000,
        max_tool_result_field_chars=1_500,
        max_context_chars=80_000,
    )

    encoded = orchestrator._format_tool_result(
        "search_concepts",
        {
            "results": [
                {
                    "concept_id": "#V#bathroom_closet",
                    "name": "Bathroom Closet",
                    "kind": "type",
                    "relevance_score": 58.0,
                },
                {
                    "concept_id": "#V#linen_cupboard",
                    "name": "Linen Cupboard",
                    "kind": "type",
                    "relevance_score": 54.0,
                },
            ],
            "total_count": 2,
            "query_info": {
                "query": "under preparation paper",
                "match_type": "substring",
                "has_more": False,
            },
        },
        1.0,
        "ok",
    )

    parsed = json.loads(encoded)
    payload = parsed["payload"]
    assert payload["retrieval_diagnostics"]["scope_only"] is False
    assert payload["retrieval_diagnostics"]["top_relevance_score"] == 58.0
    assert "weak relevance scores" in payload["retrieval_diagnostics"]["note"].lower()


def test_format_tool_result_shapes_search_arxiv_payload_for_live_follow_up():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=1,
        max_tool_result_chars=5_000,
        max_tool_result_field_chars=1_500,
        max_context_chars=80_000,
    )

    encoded = orchestrator._format_tool_result(
        "search_arxiv",
        {
            "query": "agent memory symbolic reasoning",
            "total_results": 2,
            "papers": [
                {
                    "id": "2604.12345",
                    "title": "Agent Memory With Symbolic Grounding",
                    "authors": [f"Author {index}" for index in range(12)],
                    "abstract": "A" * 1200,
                    "categories": ["cs.AI", "cs.LG"],
                    "published": "2026-04-01T00:00:00+00:00",
                    "url": "https://arxiv.org/abs/2604.12345",
                    "resource_uri": "arxiv://2604.12345",
                }
            ],
        },
        1.0,
        "ok",
    )

    parsed = json.loads(encoded)
    payload = parsed["payload"]
    assert payload["_llm_view"] == "search_arxiv_results.v1"
    assert payload["query"] == "agent memory symbolic reasoning"
    assert payload["papers"][0]["title"] == "Agent Memory With Symbolic Grounding"
    assert payload["papers"][0]["author_count"] == 12
    assert len(payload["papers"][0]["authors_preview"]) == 6
    assert len(payload["papers"][0]["abstract_preview"]) == 480


def test_format_tool_result_shapes_search_web_payload_for_live_follow_up():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=1,
        max_tool_result_chars=5_000,
        max_tool_result_field_chars=1_500,
        max_context_chars=80_000,
    )

    encoded = orchestrator._format_tool_result(
        "search_web",
        {
            "query": "agent memory workflow orchestration open source",
            "results": [
                {
                    "title": "Project Alpha",
                    "url": "https://example.com/alpha",
                    "content": "B" * 800,
                }
            ],
        },
        1.0,
        "ok",
    )

    parsed = json.loads(encoded)
    payload = parsed["payload"]
    assert payload["_llm_view"] == "search_web_results.v1"
    assert payload["query"] == "agent memory workflow orchestration open source"
    assert payload["results"][0]["title"] == "Project Alpha"
    assert payload["results"][0]["url"] == "https://example.com/alpha"
    assert len(payload["results"][0]["snippet"]) == 320


def test_format_tool_result_shapes_search_knowledge_base_payload_for_live_follow_up():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=1,
        max_tool_result_chars=5_000,
        max_tool_result_field_chars=1_500,
        max_context_chars=80_000,
    )

    encoded = orchestrator._format_tool_result(
        "search_knowledge_base",
        {
            "query": "grounded represented records linked to the current user",
            "count": 2,
            "results": [
                {
                    "id": "text_relation:1",
                    "text": "Grounded represented record: Example Record.",
                    "score": 0.93,
                    "metadata": {
                        "type": "text_relation",
                        "predicate": "hasName",
                        "concept_id": "#V#example_record",
                        "item_kind": "rag_chunk",
                        "source_system": "mongo.text_relations",
                    },
                },
                {
                    "id": "chat_history:1",
                    "text": "Previous conversational mention of an indexed record.",
                    "score": 0.72,
                    "metadata": {
                        "type": "chat_message",
                        "item_kind": "rag_chunk",
                        "source_system": "mongo.chat_history",
                    },
                },
            ],
        },
        1.0,
        "ok",
    )

    parsed = json.loads(encoded)
    payload = parsed["payload"]
    assert payload["_llm_view"] == "search_knowledge_base_results.v1"
    assert payload["query"] == "grounded represented records linked to the current user"
    assert payload["count"] == 2
    assert payload["predicates"] == ["hasName"]
    assert payload["concept_ids"] == ["#V#example_record"]
    assert {row["source_system"] for row in payload["source_system_counts"]} == {
        "mongo.chat_history",
        "mongo.text_relations",
    }
    assert {row["type"] for row in payload["type_counts"]} == {
        "chat_message",
        "text_relation",
    }
    assert payload["results"][0]["concept_id"] == "#V#example_record"
    assert "Example Record" in payload["results"][0]["text_preview"]
    assert "source systems" in payload["retrieval_diagnostics"]["note"].lower()


def test_format_tool_result_shapes_jira_search_payload_for_live_follow_up():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=1,
        max_tool_result_chars=5_000,
        max_tool_result_field_chars=1_500,
        max_context_chars=80_000,
    )

    encoded = orchestrator._format_tool_result(
        "jira_search",
        {
            "jql": "project = JVNAUTOSCI ORDER BY updated DESC",
            "total": 1,
            "issues": [
                {
                    "key": "JVNAUTOSCI-1903",
                    "fields": {
                        "summary": "Replay and record research briefing result",
                        "status": {"name": "In Progress"},
                        "issuetype": {"name": "Subtask"},
                        "assignee": {"displayName": "Michael Witbrock"},
                        "updated": "2026-04-18T08:00:00.000+0000",
                        "labels": ["real-path-testing", "multi-surface"],
                    },
                }
            ],
        },
        1.0,
        "ok",
    )

    parsed = json.loads(encoded)
    payload = parsed["payload"]
    assert payload["_llm_view"] == "jira_search_results.v1"
    assert payload["issues"][0]["key"] == "JVNAUTOSCI-1903"
    assert (
        payload["issues"][0]["summary"] == "Replay and record research briefing result"
    )
    assert payload["issues"][0]["status"] == "In Progress"
    assert payload["issues"][0]["issue_type"] == "Subtask"


def test_format_tool_result_shapes_text_relations_summary_payload_for_live_follow_up():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=1,
        max_tool_result_chars=5_000,
        max_tool_result_field_chars=1_500,
        max_context_chars=80_000,
    )

    encoded = orchestrator._format_tool_result(
        "get_text_relations_summary",
        {
            "concept_id": "#V#sail_student_group",
            "groups_found": 3,
            "total_relations_scanned": 9,
            "groups": [
                {
                    "predicate": "#V#has_phd_supervisor",
                    "language": "en-NZ",
                    "count": 5,
                    "relation_ids": ["r1", "r2", "r3", "r4"],
                    "latest_relation_id": "r4",
                },
                {
                    "predicate": "#V#member_of_organisation",
                    "language": "en-NZ",
                    "count": 3,
                    "relation_ids": ["r5", "r6", "r7"],
                    "latest_relation_id": "r7",
                },
                {
                    "predicate": "#V#has_research_topic",
                    "language": "en",
                    "count": 1,
                    "relation_ids": ["r8"],
                    "latest_relation_id": "r8",
                },
            ],
        },
        1.0,
        "ok",
    )

    parsed = json.loads(encoded)
    payload = parsed["payload"]
    assert payload["_llm_view"] == "text_relations_summary.v1"
    assert payload["concept_id"] == "#V#sail_student_group"
    assert payload["groups_found"] == 3
    assert payload["total_relations_scanned"] == 9
    assert payload["predicates"] == [
        "#V#has_phd_supervisor",
        "#V#member_of_organisation",
        "#V#has_research_topic",
    ]
    assert payload["groups"][0]["predicate"] == "#V#has_phd_supervisor"
    assert payload["groups"][0]["sample_relation_ids"] == ["r1", "r2", "r3"]
    assert "distinct predicate" in payload["retrieval_diagnostics"]["note"]


def test_format_tool_result_shapes_get_predicate_incidence_payload_for_live_follow_up():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=1,
        max_tool_result_chars=5_000,
        max_tool_result_field_chars=1_500,
        max_context_chars=80_000,
    )

    encoded = orchestrator._format_tool_result(
        "get_predicate_incidence",
        {
            "mode": "entity",
            "concept_id": "#V#michael_witbrock",
            "total_predicates": 2,
            "predicates": [
                {
                    "predicate_concept_id": "#V#author_of",
                    "predicate_preview": {
                        "name": "author of",
                        "kind": "predicate",
                    },
                    "relation_hit_count": 12,
                    "grounding_count": 7,
                    "binary_relation_hit_count": 12,
                    "subject_argument_hit_count": 12,
                    "object_argument_hit_count": 0,
                    "argument_indexes": [1],
                    "argument_type_counts": [
                        {
                            "argument_index": 2,
                            "argument_role": "object",
                            "type_concept_id": "#V#scholarly_article",
                            "concept_count": 2,
                            "relation_hit_count": 2,
                            "sample_concepts": [
                                {"concept_id": "#V#paper_one", "name": "Paper One"}
                            ],
                        }
                    ],
                    "sample_groundings": [
                        {
                            "grounding_kind": "concept",
                            "concept_id": "#V#paper_one",
                            "name": "Paper One",
                            "type_ids": ["#V#scholarly_article"],
                        },
                        {
                            "grounding_kind": "text",
                            "text_preview": "Grounded textual mention of authorship evidence.",
                        },
                    ],
                    "role_expansion": {
                        "anchor_predicate_concept_id": "#V#has_author",
                        "anchor_direction": "incoming",
                        "reified_node_count": 1,
                        "reified_node_type_counts": [
                            {
                                "type_concept_id": "#V#authorship_event",
                                "concept_count": 1,
                            }
                        ],
                        "role_filler_type_counts": [
                            {
                                "role_predicate_concept_id": "#V#has_work",
                                "type_concept_id": "#V#scholarly_article",
                                "concept_count": 1,
                                "relation_hit_count": 1,
                                "sample_fillers": [
                                    {
                                        "concept_id": "#V#paper_one",
                                        "name": "Paper One",
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
            "typed_predicate_incidence_diagnostics": {
                "include_argument_type_counts": True,
                "type_count_mode": "direct_asserted",
                "role_expansion_mode": "explicit",
            },
            "paging": {
                "limit": 50,
                "offset": 0,
                "returned": 1,
                "total_available": 2,
            },
        },
        1.0,
        "ok",
    )

    parsed = json.loads(encoded)
    payload = parsed["payload"]
    assert payload["_llm_view"] == "predicate_incidence_results.v1"
    assert payload["mode"] == "entity"
    assert payload["concept_id"] == "#V#michael_witbrock"
    assert payload["total_predicates"] == 2
    assert payload["shown_predicate_count"] == 1
    assert payload["predicates"][0]["predicate_concept_id"] == "#V#author_of"
    assert payload["predicates"][0]["predicate_name"] == "author of"
    assert payload["predicates"][0]["relation_hit_count"] == 12
    assert payload["predicates"][0]["grounding_count"] == 7
    assert (
        payload["predicates"][0]["sample_groundings"][0]["concept_id"] == "#V#paper_one"
    )
    assert payload["predicates"][0]["sample_groundings"][0]["type_ids"] == [
        "#V#scholarly_article"
    ]
    assert (
        payload["predicates"][0]["argument_type_counts"][0]["type_concept_id"]
        == "#V#scholarly_article"
    )
    assert (
        payload["predicates"][0]["role_expansion"]["reified_node_type_counts"][0][
            "type_concept_id"
        ]
        == "#V#authorship_event"
    )
    assert (
        payload["typed_predicate_incidence_diagnostics"]["role_expansion_mode"]
        == "explicit"
    )
    assert "predicate row" in payload["retrieval_diagnostics"]["note"]


def test_format_tool_result_shapes_find_relations_payload_with_target_type_ids():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=1,
        max_tool_result_chars=5_000,
        max_tool_result_field_chars=1_500,
        max_context_chars=80_000,
    )

    encoded = orchestrator._format_tool_result(
        "find_relations_with_argument",
        {
            "concept_id": "#V#michael_witbrock",
            "total_hits": 2,
            "hits": [
                {
                    "source_concept_id": "#V#michael_witbrock",
                    "predicate_concept_id": "#V#author_of",
                    "relation_kind": "binary",
                    "argument_indexes": [1],
                    "target_value": "#V#paper_one",
                    "source_concept_preview": {
                        "concept_id": "#V#michael_witbrock",
                        "name": "Michael Witbrock",
                        "kind": "individual",
                    },
                    "target_concept_preview": {
                        "concept_id": "#V#paper_one",
                        "name": "Paper One",
                        "kind": "individual",
                        "type_ids": ["#V#scholarly_article"],
                    },
                },
                {
                    "source_concept_id": "#V#michael_witbrock",
                    "predicate_concept_id": "#V#author_of",
                    "relation_kind": "binary",
                    "argument_indexes": [1],
                    "target_value": "#V#diary_one",
                    "source_concept_preview": {
                        "concept_id": "#V#michael_witbrock",
                        "name": "Michael Witbrock",
                        "kind": "individual",
                    },
                    "target_concept_preview": {
                        "concept_id": "#V#diary_one",
                        "name": "Diary One",
                        "kind": "individual",
                        "type_ids": ["#V#diary_entry_about_michael_witbrocks_work"],
                    },
                },
            ],
        },
        1.0,
        "ok",
    )

    parsed = json.loads(encoded)
    payload = parsed["payload"]
    assert payload["_llm_view"] == "find_relations_with_argument_results.v1"
    assert payload["predicates"] == ["#V#author_of"]
    assert payload["hits"][0]["target_type_ids"] == ["#V#scholarly_article"]
    assert payload["hits"][1]["target_type_ids"] == [
        "#V#diary_entry_about_michael_witbrocks_work"
    ]


def test_turn_scoped_tool_payload_support_applies_workflow_tool_argument_defaults():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=1,
        max_context_chars=80_000,
    )

    payload = {"concept_id": "#V#michael_witbrock"}
    orchestrator._apply_turn_scoped_tool_payload_support(
        tool_name="get_predicate_incidence",
        payload=payload,
        data={
            "tool_argument_defaults": {
                "get_predicate_incidence": {
                    "argument_index": "subject",
                    "relation_kind": "binary",
                    "limit": 12,
                }
            }
        },
    )

    assert payload["argument_index"] == "subject"
    assert payload["relation_kind"] == "binary"
    assert payload["limit"] == 12

    payload_with_explicit_values = {
        "concept_id": "#V#michael_witbrock",
        "limit": 3,
    }
    orchestrator._apply_turn_scoped_tool_payload_support(
        tool_name="get_predicate_incidence",
        payload=payload_with_explicit_values,
        data={
            "tool_argument_defaults": {
                "get_predicate_incidence": {
                    "argument_index": "subject",
                    "relation_kind": "binary",
                    "limit": 12,
                }
            }
        },
    )

    assert payload_with_explicit_values["argument_index"] == "subject"
    assert payload_with_explicit_values["relation_kind"] == "binary"
    assert payload_with_explicit_values["limit"] == 3


def test_turn_scoped_tool_payload_support_uses_represented_predicate_follow_up_profile():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=1,
        max_context_chars=80_000,
    )

    payload = {"concept_id": "#V#michael_witbrock"}
    data: dict[str, Any] = {
        "turn_expected_outcome_profile": {"target_type_ids": ["#V#scholarly_article"]},
        "tool_argument_defaults": {
            "find_relations_with_argument": {
                "limit": 20,
                "__derive_predicate_filter_from_recent_incidence": {
                    "enabled": True,
                    "max_predicates": 2,
                    "selection_profile": {
                        "profile_id": "#V#paper_extent_follow_up_profile",
                        "activation": {"target_type_ids": ["#V#scholarly_article"]},
                        "preferred_predicate_ids": ["#V#author_of"],
                        "preferred_argument_type_ids": ["#V#scholarly_article"],
                        "match_mode": "predicate_and_type",
                    },
                },
            }
        },
        "invocations": [
            {
                "tool": "get_predicate_incidence",
                "effective_payload": {
                    "predicates": [
                        {
                            "predicate_concept_id": (
                                "#V#has_paper_recommendation_assertion"
                            ),
                            "argument_type_counts": [
                                {
                                    "type_concept_id": (
                                        "#V#paper_recommendation_assertion"
                                    )
                                }
                            ],
                        },
                        {
                            "predicate_concept_id": "#V#author_of",
                            "argument_type_counts": [
                                {"type_concept_id": "#V#scholarly_article"}
                            ],
                        },
                    ]
                },
            }
        ],
    }

    orchestrator._apply_turn_scoped_tool_payload_support(
        tool_name="find_relations_with_argument",
        payload=payload,
        data=data,
    )

    assert payload["predicate_filter"] == ["#V#author_of"]
    assert data["tool_payload_support_events"][0]["selection_profile_ids"] == [
        "#V#paper_extent_follow_up_profile"
    ]


def test_turn_scoped_tool_payload_support_leaves_broad_profile_relation_lookup_unfiltered():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=1,
        max_context_chars=80_000,
    )

    payload = {"concept_id": "#V#michael_witbrock"}
    data: dict[str, Any] = {
        "turn_expected_outcome_profile": {},
        "tool_argument_defaults": {
            "find_relations_with_argument": {
                "__derive_predicate_filter_from_recent_incidence": {
                    "enabled": True,
                    "selection_profile": {
                        "profile_id": "#V#paper_extent_follow_up_profile",
                        "activation": {"target_type_ids": ["#V#scholarly_article"]},
                        "preferred_predicate_ids": ["#V#author_of"],
                        "preferred_argument_type_ids": ["#V#scholarly_article"],
                    },
                }
            }
        },
        "invocations": [
            {
                "tool": "get_predicate_incidence",
                "effective_payload": {
                    "predicates": [
                        {
                            "predicate_concept_id": "#V#author_of",
                            "argument_type_counts": [
                                {"type_concept_id": "#V#scholarly_article"}
                            ],
                        },
                        {"predicate_concept_id": "#V#member_of_organisation"},
                    ]
                },
            }
        ],
    }

    orchestrator._apply_turn_scoped_tool_payload_support(
        tool_name="find_relations_with_argument",
        payload=payload,
        data=data,
    )

    assert "predicate_filter" not in payload
    assert "tool_payload_support_events" not in data


def test_format_tool_result_shapes_related_concepts_payload_for_live_follow_up():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=1,
        max_tool_result_chars=5_000,
        max_tool_result_field_chars=1_500,
        max_context_chars=80_000,
    )

    encoded = orchestrator._format_tool_result(
        "get_related_concepts",
        {
            "concept_id": "#V#sail_student_group",
            "seed_text": "Strong AI Lab students and their relationships.",
            "count": 2,
            "fallback_used": True,
            "fallback_mode": "graph_text",
            "results": [
                {
                    "id": "graph_text::1",
                    "score": 0.93,
                    "text": (
                        "Timothy Pistotti has relation #V#has_phd_supervisor with "
                        "SAIL Student Group. Timothy Pistotti: PhD student in SAIL."
                    ),
                    "metadata": {
                        "item_kind": "concept_relation_fallback",
                        "source_system": "vontology.graph",
                        "predicate": "#V#has_phd_supervisor",
                        "direction": "incoming",
                        "concept_id": "#V#timothy_pistotti",
                        "subject_concept_id": "#V#sail_student_group",
                    },
                },
                {
                    "id": "graph_text::2",
                    "score": 0.89,
                    "text": (
                        "SAIL Student Group has relation #V#member_of_organisation "
                        "with University of Auckland Strong AI Lab."
                    ),
                    "metadata": {
                        "item_kind": "concept_relation_fallback",
                        "source_system": "vontology.graph",
                        "predicate": "#V#member_of_organisation",
                        "direction": "outgoing",
                        "concept_id": ("#V#university_of_auckland_strong_ai_lab"),
                        "subject_concept_id": "#V#sail_student_group",
                    },
                },
            ],
        },
        1.0,
        "ok",
    )

    parsed = json.loads(encoded)
    payload = parsed["payload"]
    assert payload["_llm_view"] == "related_concepts_results.v1"
    assert payload["concept_id"] == "#V#sail_student_group"
    assert payload["count"] == 2
    assert payload["fallback_used"] is True
    assert payload["fallback_mode"] == "graph_text"
    assert payload["related_concept_ids"] == [
        "#V#timothy_pistotti",
        "#V#university_of_auckland_strong_ai_lab",
    ]
    assert payload["predicates"] == [
        "#V#has_phd_supervisor",
        "#V#member_of_organisation",
    ]
    assert "Timothy Pistotti has relation" in payload["results"][0]["text_preview"]


def test_extract_result_summary_uses_total_count_and_query_for_search_concepts():
    summary = InternalMCPChatOrchestrator._extract_result_summary(
        "search_concepts",
        {
            "total_count": 7,
            "results": [
                {
                    "concept_id": "#V#under_preparation_paper",
                    "name": "Under Preparation Paper",
                }
            ],
            "query_info": {
                "query": "under preparation paper",
                "match_type": "substring",
            },
        },
    )

    assert summary == "Found 7 concepts for under preparation paper"
