from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, cast

from flask import Flask

from orchestrator_test_harness import (
    _stub_stage_model_snapshot,
    _stub_stage_path,
    build_db_independent_orchestrator,
)
from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
)
from src.backend.services.workflow_capability_service import (
    ensure_workflow_capability_index_populated,
    get_workflow_capability_index_runtime_state,
    reset_workflow_capability_index,
)

SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID = (
    "#V#scholarly_paper_representation_workflow"
)
_TEST_BASE_PROMPT = (
    "You have access to internal MCP tools.\n\n"
    "{auth_status}\n"
    "Available tools:\n"
    "{listing}"
)


class _LiveWorkflowDiscoveryRetrievalBackend:
    _TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")

    def __init__(self) -> None:
        self.docs_by_namespace: dict[str, dict[str, dict[str, Any]]] = {}
        self.queries: list[dict[str, Any]] = []
        self.reset_calls: list[str] = []

    def reset_namespace(self, namespace: str | None = None) -> None:
        namespace_key = str(namespace or "")
        self.reset_calls.append(namespace_key)
        self.docs_by_namespace[namespace_key] = {}

    def upsert_documents(
        self,
        docs: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        *,
        namespace: str | None = None,
        allow_partial_failures: bool = True,
    ) -> tuple[int, int]:
        namespace_key = str(namespace or "")
        store = self.docs_by_namespace.setdefault(namespace_key, {})
        for doc in docs:
            store[str(doc["id"])] = {
                "id": str(doc["id"]),
                "text": str(doc.get("text") or ""),
                "metadata": dict(doc.get("metadata") or {}),
            }
        return (len(docs), 0)

    def query(
        self,
        query_text: str,
        *,
        top_k: int = 5,
        namespace: str | None = None,
        hybrid: bool = True,
        permissions_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        namespace_key = str(namespace or "")
        self.queries.append(
            {
                "query_text": query_text,
                "top_k": top_k,
                "namespace": namespace_key,
                "hybrid": hybrid,
                "permissions_context": dict(permissions_context or {}),
            }
        )
        query_tokens = set(self._TOKEN_RE.findall(str(query_text or "").lower()))
        rows: list[dict[str, Any]] = []
        for doc in self.docs_by_namespace.get(namespace_key, {}).values():
            metadata = dict(doc.get("metadata") or {})
            requested_type = str(
                (permissions_context or {}).get("type") or ""
            ).strip()
            if requested_type and str(metadata.get("type") or "").strip() != requested_type:
                continue
            text_tokens = set(self._TOKEN_RE.findall(str(doc.get("text") or "").lower()))
            overlap = len(query_tokens & text_tokens)
            if overlap <= 0:
                continue
            rows.append(
                {
                    "id": doc["id"],
                    "text": doc["text"],
                    "metadata": metadata,
                    "score": overlap / max(len(query_tokens), 1),
                }
            )
        rows.sort(
            key=lambda row: (
                -float(row.get("score") or 0.0),
                str((row.get("metadata") or {}).get("workflow_id") or ""),
            )
        )
        return rows[:top_k]


def _build_discovery_result(
    *,
    prompt_text: str,
    workflow_id: str,
    name: str,
    description: str,
    role: str = "execution",
) -> dict[str, Any]:
    entry = {
        "concept_id": workflow_id,
        "name": name,
        "description": description,
        "is_executable": True,
        "executability_reason": "executable_now",
        "is_policy_safe": True,
        "routing_eligible": True,
        "routing_profile": {"role": role},
    }
    return {
        "query": prompt_text,
        "requested_query": prompt_text,
        "search_sources": ["capability_index"],
        "candidate_count": 1,
        "match_count": 1,
        "matches": [dict(entry)],
        "candidates": [dict(entry)],
        "routing_matches": [dict(entry)],
    }


def _tool_calling_discovery(prompt_text: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return _build_discovery_result(
        prompt_text=prompt_text,
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        name="Tool Calling Workflow",
        description="General conversational tool workflow.",
    )


def _paper_representation_discovery(
    prompt_text: str,
    *_args: Any,
    **_kwargs: Any,
) -> dict[str, Any]:
    return _build_discovery_result(
        prompt_text=prompt_text,
        workflow_id=SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
        name="Scholarly Paper Representation Workflow",
        description=(
            "Canonical durable workflow for representing scholarly papers from "
            "file-copy artefacts, metadata, and verification requirements."
        ),
    )


class _IdentityLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt, context=None, model=None):
        self.calls.append(
            {"prompt": prompt, "context": list(context or []), "model": model}
        )
        if isinstance(prompt, str) and "expected-success inference policy" in prompt:
            return (
                '{"expected_outcome_summary":"Answer from grounded identity context only.",'
                '"grounding_requirement":"Only state identity details grounded in the authenticated context.",'
                '"precision_policy":"Prefer explicit uncertainty over speculation.",'
                '"selector_guidance":"Prefer retrieval or verification only when grounded identity context is insufficient.",'
                '"answering_guidance":"If grounded identity context is available, answer directly and concisely.",'
                '"reasoning":"Authenticated identity questions should be answered from grounded actor or organisation context."}'
            )
        if isinstance(prompt, str) and prompt.strip().startswith("Select workflow"):
            return (
                '{"workflow_id":"#V#chat_assistant_workflow",'
                '"confidence":0.91,'
                '"reasoning":"The authenticated identity request is a grounded direct-response turn."}'
            )
        if isinstance(prompt, str) and "which organisation am i in?" in prompt.lower():
            return "You are in Test Org (#V#test_org)."
        if isinstance(prompt, str) and "who am i?" in prompt.lower():
            return "You are Test User (#V#test_user)."
        return "You are Test User (#V#test_user)."


class _AuthorshipLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt, context=None, model=None):
        self.calls.append(
            {"prompt": prompt, "context": list(context or []), "model": model}
        )
        if isinstance(prompt, str) and "expected-success inference policy" in prompt:
            return (
                '{"expected_outcome_summary":"Answer only with papers that can be grounded to the user.",'
                '"grounding_requirement":"Only mention papers when authorship or ownership is grounded.",'
                '"precision_policy":"Prefer omission or explicit uncertainty over speculative recall.",'
                '"selector_guidance":"Prefer grounded retrieval or verification only when the current context is insufficient.",'
                '"answering_guidance":"List only grounded papers and say clearly when the available context is incomplete.",'
                '"reasoning":"Ownership-style paper questions are precision-sensitive and should not include unsupported papers."}'
            )
        if isinstance(prompt, str) and prompt.strip().startswith("Select workflow"):
            return (
                '{"workflow_id":"#V#chat_assistant_workflow",'
                '"confidence":0.88,'
                '"reasoning":"This can be answered directly from the currently accessible grounded context if the answer stays precise."}'
            )
        return (
            "I can only confirm papers that are grounded in the current context. "
            "From what I can verify here, the available context is incomplete, and "
            "I would rather say that clearly than guess."
        )


class _GatewayStub:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}


class _EntityLookupGatewayStub:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {
            "test.lookup_current_user_papers": {
                "description": "Return grounded paper records for the current user.",
                "category": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_concept_id": {"type": "string"},
                    },
                },
            }
        }

    def invoke(self, tool_name: str, payload: dict[str, Any]):
        self.invocations.append({"tool": tool_name, "payload": dict(payload)})
        if tool_name != "test.lookup_current_user_papers":
            raise AssertionError(f"Unexpected tool call: {tool_name}")
        return SimpleNamespace(
            payload={
                "success": True,
                "papers": [
                    {
                        "concept_id": "#V#paper_test_1",
                        "title": "Test Paper",
                    }
                ],
                "response_text": "Grounded paper retrieved for Test User.",
            },
            duration_ms=5,
        )


class _GroundedKbLookupGatewayStub:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {
            "search_knowledge_base": {
                "description": "Return represented knowledge matches for the current turn.",
                "category": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                },
            },
            "search_concepts": {
                "description": "Search represented concept surfaces for the current turn.",
                "category": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                },
            }
        }

    def invoke(self, tool_name: str, payload: dict[str, Any]):
        self.invocations.append({"tool": tool_name, "payload": dict(payload)})
        if tool_name == "search_knowledge_base":
            return SimpleNamespace(
                payload={
                    "success": True,
                    "query": payload.get("query"),
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
                            "score": 0.71,
                            "metadata": {
                                "type": "chat_message",
                                "item_kind": "rag_chunk",
                                "source_system": "mongo.chat_history",
                            },
                        },
                    ],
                },
                duration_ms=5,
            )
        if tool_name == "search_concepts":
            return SimpleNamespace(
                payload={
                    "success": True,
                    "query": payload.get("query"),
                    "count": 1,
                    "results": [
                        {
                            "concept_id": "#V#example_record",
                            "name": "Example Record",
                            "score": 0.92,
                        }
                    ],
                },
                duration_ms=5,
            )
        raise AssertionError(f"Unexpected tool call: {tool_name}")


class _MixedGroundedEvidenceGatewayStub:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {
            "search_knowledge_base": {
                "description": "Return represented knowledge matches for the current turn.",
                "category": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                },
            },
            "find_relations_with_argument": {
                "description": "Return relation instances for the requested concept.",
                "category": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "concept_id": {"type": "string"},
                    },
                },
            },
        }

    def invoke(self, tool_name: str, payload: dict[str, Any]):
        self.invocations.append({"tool": tool_name, "payload": dict(payload)})
        if tool_name == "search_knowledge_base":
            return SimpleNamespace(
                payload={
                    "success": True,
                    "query": payload.get("query"),
                    "count": 0,
                    "results": [],
                },
                duration_ms=5,
            )
        if tool_name == "find_relations_with_argument":
            return SimpleNamespace(
                payload={
                    "success": True,
                    "concept_id": payload.get("concept_id"),
                    "total_hits": 1,
                    "hits": [
                        {
                            "source_concept_id": "#V#example_record",
                            "predicate_concept_id": "#V#linked_to_user",
                            "relation_kind": "binary",
                            "argument_indexes": [2],
                            "target_value": "#V#test_user",
                            "source_concept_preview": {
                                "concept_id": "#V#example_record",
                                "name": "Example Record",
                                "kind": "individual",
                            },
                            "target_concept_preview": {
                                "concept_id": "#V#test_user",
                                "name": "Test User",
                                "kind": "individual",
                            },
                            "relation_metadata": {
                                "relation_id": "struct::example_record::linked_to_user",
                                "match_type": "exact",
                            },
                            "score": 1.0,
                            "is_asserted": True,
                            "relation_state": "asserted",
                        }
                    ],
                },
                duration_ms=5,
            )
        raise AssertionError(f"Unexpected tool call: {tool_name}")


class _AffiliationLookupGatewayStub:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {
            "test.lookup_entity_affiliation": {
                "description": "Return grounded affiliation facts for a named entity.",
                "category": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entity_name": {"type": "string"},
                    },
                },
            }
        }

    def invoke(self, tool_name: str, payload: dict[str, Any]):
        self.invocations.append({"tool": tool_name, "payload": dict(payload)})
        if tool_name != "test.lookup_entity_affiliation":
            raise AssertionError(f"Unexpected tool call: {tool_name}")
        return SimpleNamespace(
            payload={
                "success": True,
                "entity_name": "Michael Witbrock",
                "organisation": "Test Org",
                "response_text": "Michael Witbrock is affiliated with Test Org.",
            },
            duration_ms=5,
        )


class _EntityRelativeToolPipelineLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt, context=None, model=None):
        context_messages = list(context or [])
        self.calls.append(
            {"prompt": prompt, "context": context_messages, "model": model}
        )
        context_text = "\n".join(
            str(message.get("content") or "")
            for message in context_messages
            if isinstance(message, dict)
        )
        if isinstance(prompt, str) and "expected-success inference policy" in prompt:
            return (
                '{"expected_outcome_summary":"Answer using grounded represented facts about the authenticated user.",'
                '"grounding_requirement":"Use represented user-linked evidence before claiming authorship or ownership.",'
                '"precision_policy":"Prefer explicit uncertainty over unsupported attribution.",'
                '"selector_guidance":"Prefer tool-based concept or relation retrieval when grounded user-linked facts are not already explicit in context.",'
                '"answering_guidance":"Retrieve grounded user-linked facts before answering, and state clearly when no grounded facts are found.",'
                '"reasoning":"Entity-relative KB lookup turns should retrieve represented relations instead of asking the user for identifiers when authenticated context exists.",'
                '"required_tools":["test.lookup_current_user_papers"]}'
            )
        if isinstance(prompt, str) and prompt.strip().startswith("Select workflow"):
            return (
                '{"workflow_id":"#V#tool_calling_workflow",'
                '"confidence":0.93,'
                '"reasoning":"This is an entity-relative KB lookup that should execute grounded retrieval before answering."}'
            )
        if prompt == "What papers of mine do you know about?":
            if (
                "Expected answer contract for this turn" in context_text
                and "CURRENT USER CONTEXT: Test User (#V#test_user)" in context_text
            ):
                return (
                    '{"action":"call_tool","tool":"test.lookup_current_user_papers",'
                    '"payload":{"user_concept_id":"#V#test_user"}}'
                )
            return "I don’t have enough information to identify any of your papers yet."
        if isinstance(prompt, str) and prompt.startswith(
            "Provide a final answer to the user now that the tool result is available."
        ):
            if "Expected answer contract for this turn" not in context_text:
                return "I don’t have enough information to identify any of your papers yet."
            return "I know about one grounded paper for you: Test Paper."
        return "I know about one grounded paper for you: Test Paper."


class _GroundedKbLookupLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt, context=None, model=None):
        context_messages = list(context or [])
        self.calls.append(
            {"prompt": prompt, "context": context_messages, "model": model}
        )
        context_text = "\n".join(
            str(message.get("content") or "")
            for message in context_messages
            if isinstance(message, dict)
        )
        if isinstance(prompt, str) and "expected-success inference policy" in prompt:
            return (
                '{"expected_outcome_summary":"List grounded represented records linked to the current user.",'
                '"grounding_requirement":"Only surface represented records that are supported by retrieved evidence.",'
                '"precision_policy":"Prefer explicit uncertainty over unsupported linkage.",'
                '"selector_guidance":"Use search_concepts and represented-knowledge retrieval, and keep the authenticated actor context in scope.",'
                '"answering_guidance":"Answer from the retrieved evidence rather than returning only counts.",'
                '"reasoning":"Entity-relative represented lookup turns should preserve turn context into retrieval and response stages.",'
                '"required_tools":["search_knowledge_base","search_concepts"]}'
            )
        if isinstance(prompt, str) and prompt.strip().startswith("Select workflow"):
            return (
                '{"workflow_id":"#V#tool_calling_workflow",'
                '"confidence":0.94,'
                '"reasoning":"This is a grounded represented-knowledge lookup that should retrieve evidence before answering."}'
            )
        if prompt == "List grounded represented records linked to the current user.":
            if (
                "Expected answer contract for this turn" in context_text
                and "CURRENT USER CONTEXT: Test User (#V#test_user)" in context_text
            ):
                return (
                    '{"action":"call_tool","tool":"search_knowledge_base",'
                    '"payload":{"query":"current user records"}}'
                )
            return "I need grounded represented retrieval first."
        if isinstance(prompt, str) and prompt.startswith(
            "Provide a final answer to the user now that the tool result is available."
        ):
            if (
                "Expected answer contract for this turn" in context_text
                and "Source systems: mongo.chat_history (1); mongo.text_relations (1)."
                in context_text
                and "KB evidence excerpts" in context_text
                and "Example Record" in context_text
            ):
                return "I found one grounded represented record: Example Record."
            return "2 results"
        return "I found one grounded represented record: Example Record."


class _MixedGroundedEvidenceLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt, context=None, model=None):
        context_messages = list(context or [])
        self.calls.append(
            {"prompt": prompt, "context": context_messages, "model": model}
        )
        context_text = "\n".join(
            str(message.get("content") or "")
            for message in context_messages
            if isinstance(message, dict)
        )
        if isinstance(prompt, str) and "expected-success inference policy" in prompt:
            return (
                '{"expected_outcome_summary":"List grounded represented records linked to the current user.",'
                '"grounding_requirement":"Only surface represented records that are supported by retrieved evidence.",'
                '"precision_policy":"Prefer explicit uncertainty over unsupported linkage.",'
                '"selector_guidance":"Use grounded retrieval surfaces and keep the authenticated actor context in scope.",'
                '"answering_guidance":"Prefer concrete retrieved evidence over count-only or zero-result summaries.",'
                '"reasoning":"Mixed retrieval turns should not let an early zero-result surface suppress later grounded evidence.",'
                '"required_tools":["search_knowledge_base","find_relations_with_argument"]}'
            )
        if isinstance(prompt, str) and prompt.strip().startswith("Select workflow"):
            return (
                '{"workflow_id":"#V#tool_calling_workflow",'
                '"confidence":0.95,'
                '"reasoning":"This is a grounded represented-knowledge lookup that should execute retrieval before answering."}'
            )
        if prompt == "List grounded represented records linked to the current user.":
            if (
                "Expected answer contract for this turn" in context_text
                and "CURRENT USER CONTEXT: Test User (#V#test_user)" in context_text
            ):
                return (
                    '{"action":"call_tool","tool":"search_knowledge_base",'
                    '"payload":{"query":"current user represented links"}}'
                )
            return "I need grounded represented retrieval first."
        if isinstance(prompt, str) and prompt.startswith(
            "Provide a final answer to the user now that the tool result is available."
        ):
            if (
                "search_knowledge_base returned 0 results" in context_text
                and "Relation-bearing evidence excerpts" not in context_text
            ):
                return (
                    '{"action":"call_tool","tool":"find_relations_with_argument",'
                    '"payload":{"concept_id":"#V#test_user"}}'
                )
            positive_index = context_text.find(
                "Positive retrieval signals for this turn:"
            )
            zero_index = context_text.find(
                "Zero-result or inconclusive retrieval surfaces for this turn:"
            )
            if (
                positive_index != -1
                and zero_index != -1
                and positive_index < zero_index
                and "Treat zero-result notes as query-specific misses only." in context_text
                and "Relation-bearing evidence excerpts" in context_text
                and "Example Record" in context_text
            ):
                return (
                    "I found grounded represented evidence linking Example Record "
                    "to the current user."
                )
            return "I couldn't find any grounded represented links."
        return "I found grounded represented evidence linking Example Record to the current user."


class _FalseNegativeGroundedEvidenceLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt, context=None, model=None):
        context_messages = list(context or [])
        self.calls.append(
            {"prompt": prompt, "context": context_messages, "model": model}
        )
        context_text = "\n".join(
            str(message.get("content") or "")
            for message in context_messages
            if isinstance(message, dict)
        )
        if isinstance(prompt, str) and "expected-success inference policy" in prompt:
            return (
                '{"expected_outcome_summary":"List grounded represented records linked to the current user.",'
                '"grounding_requirement":"Only surface represented records that are supported by retrieved evidence.",'
                '"precision_policy":"Prefer explicit uncertainty over unsupported linkage.",'
                '"selector_guidance":"Use grounded retrieval surfaces and keep the authenticated actor context in scope.",'
                '"answering_guidance":"Prefer concrete retrieved evidence over count-only or zero-result summaries.",'
                '"reasoning":"Mixed retrieval turns should not let an early zero-result surface suppress later grounded evidence.",'
                '"required_tools":["search_knowledge_base","find_relations_with_argument"]}'
            )
        if isinstance(prompt, str) and prompt.strip().startswith("Select workflow"):
            return (
                '{"workflow_id":"#V#tool_calling_workflow",'
                '"confidence":0.95,'
                '"reasoning":"This is a grounded represented-knowledge lookup that should execute retrieval before answering."}'
            )
        if (
            isinstance(prompt, str)
            and "You are the postcondition critic for one completed Von turn." in prompt
        ):
            return (
                '{"verdict":"follow_up_required",'
                '"confidence":0.98,'
                '"assessment_summary":"The answer says no grounded links were found even though relation retrieval produced grounded evidence linking Example Record to the current user.",'
                '"required_evidence_answer_consistency_blocker":{'
                '"effect_id":"effect_prompt_required_evidence_answer_consistency",'
                '"effect_type":"required_evidence_answer_consistency",'
                '"status":"not_satisfied",'
                '"decision":"partial",'
                '"decision_reason":"The answer contradicts grounded positive relation evidence from the executed retrieval path.",'
                '"status_reason":"Positive relation evidence was retrieved after the zero-result search, so the low-information answer is not safely supported.",'
                '"failure_code":"prompt_required_evidence_positive_results_contradict_low_information_answer",'
                '"failure_codes":["prompt_required_evidence_positive_results_contradict_low_information_answer"],'
                '"repeat_eligible":true,'
                '"blocker_source":"critic_verdict",'
                '"response_surface_kind":"insufficiency_claim",'
                '"observed_result_signals":["positive_relation_hits"]'
                '},'
                '"recommendations":["Revise the answer to reflect the grounded relation evidence instead of claiming no grounded links were found."]}'
            )
        if prompt == "List grounded represented records linked to the current user.":
            if (
                "Expected answer contract for this turn" in context_text
                and "CURRENT USER CONTEXT: Test User (#V#test_user)" in context_text
            ):
                return (
                    '{"action":"call_tool","tool":"search_knowledge_base",'
                    '"payload":{"query":"current user represented links"}}'
                )
            return "I need grounded represented retrieval first."
        if isinstance(prompt, str) and prompt.startswith(
            "Provide a final answer to the user now that the tool result is available."
        ):
            if (
                "search_knowledge_base returned 0 results" in context_text
                and "Relation-bearing evidence excerpts" not in context_text
            ):
                return (
                    '{"action":"call_tool","tool":"find_relations_with_argument",'
                    '"payload":{"concept_id":"#V#test_user"}}'
                )
            return "I couldn't find any grounded represented links."
        return "I couldn't find any grounded represented links."


class _ExplicitEntityRelationLookupLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt, context=None, model=None):
        context_messages = list(context or [])
        self.calls.append(
            {"prompt": prompt, "context": context_messages, "model": model}
        )
        context_text = "\n".join(
            str(message.get("content") or "")
            for message in context_messages
            if isinstance(message, dict)
        )
        if isinstance(prompt, str) and "expected-success inference policy" in prompt:
            return (
                '{"expected_outcome_summary":"Answer with grounded represented facts about the named entity.",'
                '"grounding_requirement":"Only state relationships that are grounded in represented facts or retrieved evidence.",'
                '"precision_policy":"Prefer explicit uncertainty over unsupported relationship claims.",'
                '"selector_guidance":"Prefer concept, relation, or tool-based retrieval over generic chat when represented lookup is required.",'
                '"answering_guidance":"Retrieve the grounded relationship before answering, and say clearly when no grounded relation is found.",'
                '"reasoning":"Explicit entity-relation lookup turns should retrieve represented facts instead of answering from unsupported recall.",'
                '"required_tools":["test.lookup_entity_affiliation"]}'
            )
        if isinstance(prompt, str) and prompt.strip().startswith("Select workflow"):
            return (
                '{"workflow_id":"#V#tool_calling_workflow",'
                '"confidence":0.94,'
                '"reasoning":"This is a grounded represented-knowledge lookup that should retrieve the relation before answering."}'
            )
        if (
            prompt
            == "Which organisation is Michael Witbrock affiliated with in the represented knowledge?"
        ):
            if "Expected answer contract for this turn" in context_text:
                return (
                    '{"action":"call_tool","tool":"test.lookup_entity_affiliation",'
                    '"payload":{"entity_name":"Michael Witbrock"}}'
                )
            return "I don’t have enough grounded information yet."
        if isinstance(prompt, str) and prompt.startswith(
            "Provide a final answer to the user now that the tool result is available."
        ):
            if "Expected answer contract for this turn" not in context_text:
                return "I don’t have enough grounded information yet."
            return "Michael Witbrock is affiliated with Test Org."
        return "Michael Witbrock is affiliated with Test Org."


class _PredicateExtentRoutingGatewayStub:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {
            "get_predicate_incidence": {
                "description": (
                    "Summarise distinct predicates around a concept before choosing a "
                    "predicate-specific extent or filtered relation lookup."
                ),
                "category": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "concept_id": {"type": "string"},
                        "predicate_filter": {"type": "array"},
                    },
                },
            },
            "find_relations_with_argument": {
                "description": "Return grounded relation hits for a concept argument.",
                "category": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "concept_id": {"type": "string"},
                        "predicate_filter": {"type": "array"},
                    },
                },
            },
        }

    def invoke(self, tool_name: str, payload: dict[str, Any]):
        self.invocations.append({"tool": tool_name, "payload": dict(payload)})
        if tool_name == "get_predicate_incidence":
            return SimpleNamespace(
                payload={
                    "mode": "entity",
                    "concept_id": "#V#test_user",
                    "total_predicates": 1,
                    "predicates": [
                        {
                            "predicate_concept_id": "#V#author_of",
                            "predicate_preview": {
                                "name": "author of",
                                "kind": "predicate",
                            },
                            "relation_hit_count": 2,
                            "grounding_count": 2,
                            "subject_argument_hit_count": 2,
                            "object_argument_hit_count": 0,
                            "argument_indexes": [1],
                            "sample_groundings": [
                                {
                                    "grounding_kind": "concept",
                                    "concept_id": "#V#test_paper_one",
                                    "name": "Test Paper One",
                                },
                                {
                                    "grounding_kind": "concept",
                                    "concept_id": "#V#test_paper_two",
                                    "name": "Test Paper Two",
                                },
                            ],
                        }
                    ],
                    "paging": {
                        "limit": 50,
                        "offset": 0,
                        "returned": 1,
                        "total_available": 1,
                    },
                },
                duration_ms=5,
            )
        if tool_name == "find_relations_with_argument":
            return SimpleNamespace(
                payload={
                    "concept_id": payload.get("concept_id"),
                    "total_hits": 2,
                    "hits": [
                        {
                            "source_concept_id": "#V#test_user",
                            "predicate_concept_id": "#V#author_of",
                            "relation_kind": "binary",
                            "argument_indexes": [1],
                            "target_value": "#V#test_paper_one",
                            "source_concept_preview": {
                                "concept_id": "#V#test_user",
                                "name": "Test User",
                                "kind": "individual",
                            },
                            "target_concept_preview": {
                                "concept_id": "#V#test_paper_one",
                                "name": "Test Paper One",
                                "kind": "individual",
                            },
                            "relation_metadata": {
                                "relation_id": "struct::test_user::author_of::paper_one",
                                "match_type": "exact",
                            },
                            "score": 1.0,
                            "is_asserted": True,
                            "relation_state": "asserted",
                        },
                        {
                            "source_concept_id": "#V#test_user",
                            "predicate_concept_id": "#V#author_of",
                            "relation_kind": "binary",
                            "argument_indexes": [1],
                            "target_value": "#V#test_paper_two",
                            "source_concept_preview": {
                                "concept_id": "#V#test_user",
                                "name": "Test User",
                                "kind": "individual",
                            },
                            "target_concept_preview": {
                                "concept_id": "#V#test_paper_two",
                                "name": "Test Paper Two",
                                "kind": "individual",
                            },
                            "relation_metadata": {
                                "relation_id": "struct::test_user::author_of::paper_two",
                                "match_type": "exact",
                            },
                            "score": 1.0,
                            "is_asserted": True,
                            "relation_state": "asserted",
                        },
                    ],
                    "paging": {
                        "limit": 20,
                        "offset": 0,
                        "returned": 2,
                        "total_available": 2,
                    },
                },
                duration_ms=5,
            )
        raise AssertionError(f"Unexpected tool call: {tool_name}")


class _PredicateExtentRoutingLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt, context=None, model=None):
        context_messages = list(context or [])
        self.calls.append(
            {"prompt": prompt, "context": context_messages, "model": model}
        )
        context_text = "\n".join(
            str(message.get("content") or "")
            for message in context_messages
            if isinstance(message, dict)
        )
        if isinstance(prompt, str) and "expected-success inference policy" in prompt:
            return (
                '{"expected_outcome_summary":"Identify the represented concepts that stand in the requested predicate relation to the authenticated user.",'
                '"grounding_requirement":"Use ontology-native predicate incidence or relation evidence before naming related concepts.",'
                '"precision_policy":"Prefer explicit insufficiency over unsupported relation claims.",'
                '"selector_guidance":"Prefer ontology-native retrieval surfaces that narrow by predicate before broad relation-hit paging.",'
                '"answering_guidance":"Use the predicate incidence evidence to choose the relevant predicate, then answer from grounded relation hits.",'
                '"reasoning":"Explicit predicate-relative turns should not rely on broad unfiltered relation paging when authenticated actor context exists.",'
                '"required_tools":["get_predicate_incidence","find_relations_with_argument"]}'
            )
        if isinstance(prompt, str) and prompt.strip().startswith("Select workflow"):
            return (
                '{"workflow_id":"#V#tool_calling_workflow",'
                '"confidence":0.95,'
                '"reasoning":"This is a grounded ontology relation turn that should use the tool pipeline."}'
            )
        if prompt == "What concepts am I in in a #V#author_of relation with?":
            if (
                "Expected answer contract for this turn" in context_text
                and "CURRENT USER CONTEXT: Test User (#V#test_user)" in context_text
            ):
                return (
                    '{"action":"call_tool","tool":"get_predicate_incidence",'
                    '"payload":{"concept_id":"#V#test_user","predicate_filter":["#V#author_of"]}}'
                )
            return "I need grounded predicate incidence evidence first."
        if isinstance(prompt, str) and prompt.startswith(
            "Provide a final answer to the user now that the tool result is available."
        ):
            if (
                "Predicate incidence summary" in context_text
                and "#V#author_of" in context_text
                and "Relation-bearing evidence excerpts" not in context_text
            ):
                return (
                    '{"action":"call_tool","tool":"find_relations_with_argument",'
                    '"payload":{"concept_id":"#V#test_user","predicate_filter":["#V#author_of"],"limit":20}}'
                )
            if (
                "Predicate incidence summary" in context_text
                and "Relation-bearing evidence excerpts" in context_text
                and "Test Paper One" in context_text
                and "Test Paper Two" in context_text
            ):
                return (
                    "You are in a represented #V#author_of relation with "
                    "Test Paper One and Test Paper Two."
                )
            return "I couldn't find any grounded #V#author_of relations yet."
        return "You are in a represented #V#author_of relation with Test Paper One and Test Paper Two."


def _make_app(
    monkeypatch,
    *,
    llm: (
        _IdentityLLM
        | _AuthorshipLLM
        | _EntityRelativeToolPipelineLLM
        | _GroundedKbLookupLLM
        | _MixedGroundedEvidenceLLM
        | _FalseNegativeGroundedEvidenceLLM
        | _ExplicitEntityRelationLookupLLM
        | _PredicateExtentRoutingLLM
    ),
    gateway_override: Any | None = None,
    discovery_override: Any | None = None,
    use_live_discovery: bool = False,
    max_tool_invocations: int = 1,
) -> Flask:
    import src.backend.workflows.durable.registry_factory as registry_factory

    monkeypatch.setattr(registry_factory, "discover_workflow_ids", lambda: [])
    monkeypatch.setattr(
        registry_factory, "_launch_deferred_registry_work", lambda **_kwargs: None
    )

    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setenv("VON_INTERNAL_MCP_ALLOW_USER_TOOL_CALLS", "0")
    monkeypatch.setenv("VON_WORKFLOW_DISCOVERY_ENABLE", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.build_conversation_turn_stage_model_snapshot",
        _stub_stage_model_snapshot,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.build_conversation_turn_stage_path",
        _stub_stage_path,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: llm,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_buttonify_model_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda window_session_id, session_snapshot, user_concept_id: {
            "organisation_id": "#V#test_org",
            "chat_session_id": "window-session",
            "role": "member",
            "source": "test",
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _user_id, **_kwargs: [],
    )
    if use_live_discovery and discovery_override is not None:
        raise AssertionError(
            "use_live_discovery and discovery_override are mutually exclusive"
        )
    if not use_live_discovery:
        monkeypatch.setattr(
            "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
            discovery_override
            or (
                lambda prompt_text, *_args, **_kwargs: _build_discovery_result(
                    prompt_text=prompt_text,
                    workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
                    name="Chat Assistant Workflow",
                    description="General conversational workflow.",
                )
            ),
            raising=False,
        )
    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        lambda concept_id: {
            "#V#test_user": {"name": "Test User"},
            "#V#test_org": {"name": "Test Org"},
        }.get(concept_id),
    )
    gateway = gateway_override if gateway_override is not None else _GatewayStub()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        selector_enabled=True,
        max_tool_invocations=max_tool_invocations,
    )
    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda preferred_language=None: (
            _TEST_BASE_PROMPT,
            "#V#test_base_prompt",
        ),
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = True
    app.config["CONTEXT"] = []
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator
    app.config["INTERNAL_MCP_GATEWAY"] = gateway
    app.register_blueprint(von_bp, url_prefix="/von")
    return app


def test_generate_authenticated_identity_turn_uses_direct_response_and_records_plain_response_telemetry(
    monkeypatch,
) -> None:
    llm = _IdentityLLM()
    app = _make_app(monkeypatch, llm=llm)

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "Who am I?"})
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("response") == "You are Test User (#V#test_user)."

    llm_debug = body.get("llm_debug") or {}
    workflow_routing = llm_debug.get("workflow_routing") or {}
    assert workflow_routing.get("workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    assert workflow_routing.get("verdict") in {"rag_selected", "rag_default"}
    assert workflow_routing.get("source") == "selector"

    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    workflow_routing_diagnostics = diagnostics.get("workflow_routing_diagnostics") or {}
    dispatch = workflow_routing_diagnostics.get("dispatch") or {}
    assert dispatch.get("selected_execution_mode") == "direct_response"
    assert dispatch.get("dispatch_workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    assert dispatch.get("dispatch_terminal_failure_reason") in {None, ""}

    turn_record = llm_debug.get("turn_execution_record") or {}
    completion_report = turn_record.get("completion_report") or {}
    assert completion_report.get("workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    assert completion_report.get("response_text") == "You are Test User (#V#test_user)."
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "direct_response"
    assert selected_workflow_trace.get("child_workflow_final_state") == "plain_response"

    assert len(llm.calls) >= 2


def test_generate_authenticated_organisation_turn_uses_direct_response_and_preserves_org_context_telemetry(
    monkeypatch,
) -> None:
    llm = _IdentityLLM()
    app = _make_app(monkeypatch, llm=llm)

    client = app.test_client()
    response = client.post(
        "/von/generate", json={"prompt": "Which organisation am I in?"}
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("response") == "You are in Test Org (#V#test_org)."

    llm_debug = body.get("llm_debug") or {}
    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    workflow_routing_diagnostics = diagnostics.get("workflow_routing_diagnostics") or {}
    dispatch = workflow_routing_diagnostics.get("dispatch") or {}
    assert dispatch.get("selected_execution_mode") == "direct_response"
    assert dispatch.get("dispatch_workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID

    turn_record = llm_debug.get("turn_execution_record") or {}
    completion_report = turn_record.get("completion_report") or {}
    assert (
        completion_report.get("response_text") == "You are in Test Org (#V#test_org)."
    )
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "direct_response"
    assert selected_workflow_trace.get("child_workflow_final_state") == "plain_response"

    context_text = "\n".join(
        str(message.get("content") or "")
        for call in llm.calls
        for message in (call.get("context") or [])
        if isinstance(message, dict)
    )
    assert "CURRENT ORGANISATION CONTEXT: Test Org (#V#test_org)" in context_text


def test_generate_authorship_turn_prefers_grounded_omission_over_unsupported_paper_inclusion(
    monkeypatch,
) -> None:
    llm = _AuthorshipLLM()
    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
        lambda prompt_text, *_args, **_kwargs: {
            "query": prompt_text,
            "requested_query": prompt_text,
            "search_sources": ["capability_index"],
            "candidate_count": 1,
            "match_count": 1,
            "matches": [
                {
                    "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                    "name": "Chat Assistant Workflow",
                    "description": "General conversational workflow.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "routing_profile": {"role": "execution"},
                }
            ],
            "candidates": [
                {
                    "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                    "name": "Chat Assistant Workflow",
                    "description": "General conversational workflow.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "routing_profile": {"role": "execution"},
                }
            ],
            "routing_matches": [
                {
                    "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                    "name": "Chat Assistant Workflow",
                    "description": "General conversational workflow.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "routing_profile": {"role": "execution"},
                }
            ],
        },
        raising=False,
    )
    app = _make_app(monkeypatch, llm=llm)
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [
            {"role": "user", "content": "Check my papers"},
            {"role": "assistant", "content": "I need more grounded context first."},
        ],
    )

    client = app.test_client()
    response = client.post(
        "/von/generate", json={"prompt": "What papers of mine do you know about?"}
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    response_text = str(body.get("response") or "")
    assert "Titans" not in response_text
    assert "incomplete" in response_text.lower()

    llm_debug = body.get("llm_debug") or {}
    workflow_routing = llm_debug.get("workflow_routing") or {}
    assert workflow_routing.get("workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    assert workflow_routing.get("source") == "selector"

    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    stage_model = diagnostics.get("workflow_stage_model") or {}
    stage_entries = stage_model.get("stages") or []
    stage_by_id = {
        str(entry.get("stage_id")): entry
        for entry in stage_entries
        if isinstance(entry, dict) and entry.get("stage_id")
    }
    assert "expected_outcome_inference" in stage_by_id
    assert "selector_preparation" in stage_by_id
    assert "selector_decision" in stage_by_id
    stage_diagnostics = diagnostics.get("stage_diagnostics") or []
    if stage_diagnostics:
        stage_diagnostic_ids = {
            str(entry.get("stage_id"))
            for entry in stage_diagnostics
            if isinstance(entry, dict) and entry.get("stage_id")
        }
        assert "expected_outcome_inference" in stage_diagnostic_ids
        assert "selector_preparation" in stage_diagnostic_ids
        assert "selector_decision" in stage_diagnostic_ids
    turn_record = llm_debug.get("turn_execution_record") or {}
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "direct_response"
    direct_response_context_lineage = (
        selected_workflow_trace.get("direct_response_context_lineage") or {}
    )
    assert direct_response_context_lineage.get("base_context_source") == (
        "augmented_context"
    )
    assert (direct_response_context_lineage.get("stage_added_message_count") or 0) >= 1
    assert any(
        "Current turn request" in str(message.get("content_preview") or "")
        and "What papers of mine do you know about?"
        in str(message.get("content_preview") or "")
        for message in (
            direct_response_context_lineage.get("stage_added_messages") or []
        )
        if isinstance(message, dict)
    )
    assert any(
        "Expected answer contract for this turn"
        in str(message.get("content_preview") or "")
        for message in (
            direct_response_context_lineage.get("stage_added_messages") or []
        )
        if isinstance(message, dict)
    )
    direct_response_call = next(
        call
        for call in llm.calls
        if call.get("prompt") == "What papers of mine do you know about?"
    )
    direct_response_context_text = "\n".join(
        str(message.get("content") or "")
        for message in (direct_response_call.get("context") or [])
        if isinstance(message, dict)
    )
    assert "Check my papers" in direct_response_context_text
    assert "Current turn request" in direct_response_context_text
    assert "What papers of mine do you know about?" in direct_response_context_text
    expected_outcome_contract = (
        selected_workflow_trace.get("expected_outcome_contract") or {}
    )
    assert expected_outcome_contract.get("precision_policy") == (
        "Prefer omission or explicit uncertainty over speculative recall."
    )


def test_generate_entity_relative_tool_pipeline_threads_expected_contract_into_tool_planning(
    monkeypatch,
) -> None:
    llm = _EntityRelativeToolPipelineLLM()
    gateway = _EntityLookupGatewayStub()
    app = _make_app(
        monkeypatch,
        llm=llm,
        gateway_override=gateway,
        discovery_override=_tool_calling_discovery,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate", json={"prompt": "What papers of mine do you know about?"}
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    response_text = str(body.get("response") or "")
    assert "Test Paper" in response_text

    llm_debug = body.get("llm_debug") or {}
    tool_invocations = llm_debug.get("tool_invocations") or []
    lookup_records = [
        record
        for record in tool_invocations
        if isinstance(record, dict)
        and (record.get("tool") or record.get("method"))
        == "test.lookup_current_user_papers"
    ]
    assert lookup_records

    assert gateway.invocations

    turn_record = llm_debug.get("turn_execution_record") or {}
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "tool_pipeline"

    tool_plan_call = next(
        call
        for call in llm.calls
        if call.get("prompt") == "What papers of mine do you know about?"
    )
    tool_plan_context_text = "\n".join(
        str(message.get("content") or "")
        for message in (tool_plan_call.get("context") or [])
        if isinstance(message, dict)
    )
    assert "Expected answer contract for this turn" in tool_plan_context_text
    assert "CURRENT USER CONTEXT: Test User (#V#test_user)" in tool_plan_context_text


def test_generate_grounded_kb_lookup_preserves_turn_context_into_payload_and_summary(
    monkeypatch,
) -> None:
    llm = _GroundedKbLookupLLM()
    gateway = _GroundedKbLookupGatewayStub()
    app = _make_app(
        monkeypatch,
        llm=llm,
        gateway_override=gateway,
        discovery_override=_tool_calling_discovery,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate",
        json={"prompt": "List grounded represented records linked to the current user."},
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    response_text = str(body.get("response") or "")
    assert "Example Record" in response_text
    assert response_text != "2 results"

    assert gateway.invocations
    invocation = gateway.invocations[0]
    assert invocation["tool"] == "search_knowledge_base"
    payload = invocation["payload"]
    assert payload.get("mode") == "concepts"
    assert payload.get("query") != "current user records"
    assert "Turn-intent routing guidance:" in str(payload.get("query") or "")
    assert "Authenticated actor context:" in str(payload.get("query") or "")

    summariser_call = next(
        call
        for call in llm.calls
        if isinstance(call.get("prompt"), str)
        and call["prompt"].startswith(
            "Provide a final answer to the user now that the tool result is available."
        )
    )
    summariser_context_text = "\n".join(
        str(message.get("content") or "")
        for message in (summariser_call.get("context") or [])
        if isinstance(message, dict)
    )
    assert "Source systems: mongo.chat_history (1); mongo.text_relations (1)." in (
        summariser_context_text
    )
    assert "Document types: chat_message (1); text_relation (1)." in (
        summariser_context_text
    )
    assert "KB evidence excerpts" in summariser_context_text
    assert "Example Record" in summariser_context_text

    llm_debug = body.get("llm_debug") or {}
    turn_record = llm_debug.get("turn_execution_record") or {}
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "tool_pipeline"
    contract_state = selected_workflow_trace.get("expected_outcome_contract_state") or {}
    assert contract_state.get("schema_version") == "turn_expected_outcome_contract.v1"
    assert contract_state.get("fields", {}).get("summary") == (
        "List grounded represented records linked to the current user."
    )
    turn_record_contract_state = (
        turn_record.get("turn_expected_outcome_contract_state") or {}
    )
    assert turn_record_contract_state.get("fields", {}).get("summary") == (
        "List grounded represented records linked to the current user."
    )
    routing_contract_state = (
        (turn_record.get("workflow_routing_diagnostics") or {}).get(
            "turn_expected_outcome_contract_state"
        )
        or {}
    )
    assert routing_contract_state.get("fields", {}).get("summary") == (
        "List grounded represented records linked to the current user."
    )


def test_generate_grounded_follow_up_prefers_positive_relation_evidence_over_zero_result_surface(
    monkeypatch,
) -> None:
    llm = _MixedGroundedEvidenceLLM()
    gateway = _MixedGroundedEvidenceGatewayStub()
    app = _make_app(
        monkeypatch,
        llm=llm,
        gateway_override=gateway,
        discovery_override=_tool_calling_discovery,
        max_tool_invocations=3,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate",
        json={"prompt": "List grounded represented records linked to the current user."},
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    response_text = str(body.get("response") or "")
    assert response_text == (
        "I found grounded represented evidence linking Example Record to the current user."
    )
    assert response_text != "I couldn't find any grounded represented links."

    assert [row["tool"] for row in gateway.invocations] == [
        "search_knowledge_base",
        "find_relations_with_argument",
    ]

    summariser_calls = [
        call
        for call in llm.calls
        if isinstance(call.get("prompt"), str)
        and call["prompt"].startswith(
            "Provide a final answer to the user now that the tool result is available."
        )
    ]
    assert len(summariser_calls) >= 2
    final_summariser_context_text = "\n".join(
        str(message.get("content") or "")
        for message in (summariser_calls[-1].get("context") or [])
        if isinstance(message, dict)
    )
    assert "Positive retrieval signals for this turn:" in final_summariser_context_text
    assert (
        "Zero-result or inconclusive retrieval surfaces for this turn:"
        in final_summariser_context_text
    )
    assert (
        final_summariser_context_text.find("Positive retrieval signals for this turn:")
        < final_summariser_context_text.find(
            "Zero-result or inconclusive retrieval surfaces for this turn:"
        )
    )
    assert (
        "Treat zero-result notes as query-specific misses only."
        in final_summariser_context_text
    )
    assert "Relation-bearing evidence excerpts" in final_summariser_context_text
    assert "Example Record via #V#linked_to_user -> Test User" in (
        final_summariser_context_text
    )


def test_generate_grounded_false_negative_turn_is_recorded_as_false_success(
    monkeypatch,
) -> None:
    llm = _FalseNegativeGroundedEvidenceLLM()
    gateway = _MixedGroundedEvidenceGatewayStub()
    app = _make_app(
        monkeypatch,
        llm=llm,
        gateway_override=gateway,
        discovery_override=_tool_calling_discovery,
        max_tool_invocations=3,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate",
        json={"prompt": "List grounded represented records linked to the current user."},
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("response") == "I couldn't find any grounded represented links."

    observed_tools = [row["tool"] for row in gateway.invocations]
    assert observed_tools[0] == "search_knowledge_base"
    assert "find_relations_with_argument" in observed_tools

    llm_debug = body.get("llm_debug") or {}
    turn_record = llm_debug.get("turn_execution_record") or {}
    execution_correctness = turn_record.get("execution_correctness") or {}
    assert execution_correctness.get("overall_outcome") == "false_success"
    assert execution_correctness.get("failure_mode") == "false_completion_claim"
    gate_labels = execution_correctness.get("gate_labels") or {}
    assert gate_labels.get("required_evidence_answer_consistency_blocked") is True

    completion_gate = turn_record.get("completion_gate") or {}
    assert (
        "prompt_required_evidence_positive_results_contradict_low_information_answer"
        in (completion_gate.get("blocking_failure_codes") or [])
    )
    blocker = (
        (completion_gate.get("evidence_payload") or {}).get(
            "required_evidence_answer_consistency_blocker"
        )
        or {}
    )
    assert blocker.get("response_surface_kind") == "insufficiency_claim"


def test_generate_entity_relative_tool_pipeline_uses_live_workflow_retrieval_surface(
    monkeypatch,
) -> None:
    llm = _EntityRelativeToolPipelineLLM()
    gateway = _EntityLookupGatewayStub()
    retrieval_backend = _LiveWorkflowDiscoveryRetrievalBackend()

    reset_workflow_capability_index()
    try:
        monkeypatch.setattr(
            "src.backend.services.workflow_capability_service._get_workflow_capability_rag_service",
            lambda: retrieval_backend,
        )
        monkeypatch.setattr(
            "src.backend.workflows.vontology_loader.batch_fetch_workflow_routing_metadata",
            lambda workflow_ids: {
                TOOL_CALLING_WORKFLOW_ID: {
                    "description_text": (
                        "Grounded represented-knowledge retrieval workflow for "
                        "entity-relative paper and relation lookups."
                    ),
                    "description_source": "text_relation:#V#hasDescription",
                    "discovery_exemplars": {
                        "schema_version": "workflow_discovery_exemplars.v1",
                        "keywords": [
                            "papers of mine",
                            "grounded retrieval",
                            "represented knowledge",
                        ],
                        "examples": [
                            "What papers of mine do you know about?",
                            "Find grounded represented facts about my papers.",
                        ],
                    },
                    "discovery_exemplars_source": (
                        "text_relation:#V#hasWorkflowDiscoveryExemplarsJson"
                    ),
                },
                CHAT_ASSISTANT_WORKFLOW_ID: {
                    "description_text": "Direct conversational response workflow.",
                    "description_source": "text_relation:#V#hasDescription",
                },
            },
        )
        monkeypatch.setattr(
            "src.backend.workflows.vontology_loader.resolve_workflow_description",
            lambda _workflow_id, **kwargs: (
                str(
                    kwargs.get("registration_purpose")
                    or kwargs.get("definition_purpose")
                    or ""
                ).strip(),
                "text_relation:#V#hasDescription",
            ),
        )
        monkeypatch.setattr(
            "src.backend.workflows.vontology_loader.resolve_workflow_discovery_exemplars",
            lambda _workflow_id: (None, ""),
        )
        monkeypatch.setattr(
            "src.backend.services.workflow_discovery_service._search_workflows_semantic",
            lambda *_args, **_kwargs: [],
        )
        monkeypatch.setattr(
            "src.backend.services.workflow_discovery_service._search_workflows_vontology",
            lambda *_args, **_kwargs: [],
        )
        monkeypatch.setattr(
            "src.backend.services.workflow_discovery_service._search_workflows_name_fallback",
            lambda *_args, **_kwargs: [],
        )
        monkeypatch.setattr(
            "src.backend.services.workflow_discovery_service.SEARCH_TIMEOUT_SECONDS",
            2.0,
        )

        app = _make_app(
            monkeypatch,
            llm=llm,
            gateway_override=gateway,
            use_live_discovery=True,
        )
        orchestrator = app.config["INTERNAL_MCP_ORCHESTRATOR"]
        ensure_workflow_capability_index_populated(
            workflow_registry=orchestrator._workflow_registry
        )

        client = app.test_client()
        response = client.post(
            "/von/generate", json={"prompt": "What papers of mine do you know about?"}
        )
        assert response.status_code == 200

        body = response.get_json()
        assert isinstance(body, dict)
        assert "Test Paper" in str(body.get("response") or "")
        assert gateway.invocations
        assert retrieval_backend.reset_calls == ["workflow_capabilities"]
        assert retrieval_backend.queries
        assert retrieval_backend.queries[-1]["namespace"] == "workflow_capabilities"
        assert retrieval_backend.queries[-1]["permissions_context"] == {
            "type": "workflow_capability"
        }

        llm_debug = body.get("llm_debug") or {}
        workflow_routing = llm_debug.get("workflow_routing") or {}
        assert workflow_routing.get("workflow_id") == TOOL_CALLING_WORKFLOW_ID
        assert workflow_routing.get("source") == "selector"

        diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
        routing_diagnostics = diagnostics.get("workflow_routing_diagnostics") or {}
        discovery = routing_diagnostics.get("discovery") or {}
        assert "capability_index" in list(discovery.get("search_sources") or [])
        assert TOOL_CALLING_WORKFLOW_ID in list(discovery.get("candidate_ids") or [])

        turn_record = llm_debug.get("turn_execution_record") or {}
        execution = turn_record.get("execution") or {}
        selected_workflow_trace = execution.get("selected_workflow_trace") or {}
        assert selected_workflow_trace.get("selected_execution_mode") == "tool_pipeline"

        runtime_state = get_workflow_capability_index_runtime_state()
        assert runtime_state.get("surface") == "workflow_retrieval"
        assert runtime_state.get("namespace") == "workflow_capabilities"
        assert runtime_state.get("ready") is True
        assert int(runtime_state.get("size") or 0) > 0
    finally:
        reset_workflow_capability_index()


def test_generate_entity_relative_lookup_excludes_non_launchable_representation_workflow(
    monkeypatch,
) -> None:
    llm = _EntityRelativeToolPipelineLLM()
    gateway = _EntityLookupGatewayStub()
    app = _make_app(
        monkeypatch,
        llm=llm,
        gateway_override=gateway,
        discovery_override=_paper_representation_discovery,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate", json={"prompt": "What papers of mine do you know about?"}
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    response_text = str(body.get("response") or "")
    assert "Test Paper" in response_text
    assert gateway.invocations

    llm_debug = body.get("llm_debug") or {}
    tool_invocations = llm_debug.get("tool_invocations") or []
    assert any(
        isinstance(record, dict)
        and (record.get("tool") or record.get("method"))
        == "test.lookup_current_user_papers"
        for record in tool_invocations
    )

    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    routing_diagnostics = diagnostics.get("workflow_routing_diagnostics") or {}
    discovery = routing_diagnostics.get("discovery") or {}
    assert SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID in (
        discovery.get("candidate_ids") or []
    )

    turn_record = llm_debug.get("turn_execution_record") or {}
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "tool_pipeline"
    assert SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID not in (
        selected_workflow_trace.get("selector_candidate_ids") or []
    )
    assert SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID in (
        selected_workflow_trace.get("excluded_candidate_ids") or []
    )


def test_generate_explicit_entity_relation_lookup_uses_grounded_tool_pipeline(
    monkeypatch,
) -> None:
    llm = _ExplicitEntityRelationLookupLLM()
    gateway = _AffiliationLookupGatewayStub()
    app = _make_app(
        monkeypatch,
        llm=llm,
        gateway_override=gateway,
        discovery_override=_tool_calling_discovery,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate",
        json={
            "prompt": (
                "Which organisation is Michael Witbrock affiliated with in the "
                "represented knowledge?"
            )
        },
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("response") == "Michael Witbrock is affiliated with Test Org."

    llm_debug = body.get("llm_debug") or {}
    tool_invocations = llm_debug.get("tool_invocations") or []
    assert any(
        isinstance(record, dict)
        and (record.get("tool") or record.get("method"))
        == "test.lookup_entity_affiliation"
        for record in tool_invocations
    )

    turn_record = llm_debug.get("turn_execution_record") or {}
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "tool_pipeline"


def test_generate_explicit_predicate_relative_turn_uses_predicate_incidence_before_relation_hits(
    monkeypatch,
) -> None:
    llm = _PredicateExtentRoutingLLM()
    gateway = _PredicateExtentRoutingGatewayStub()
    app = _make_app(
        monkeypatch,
        llm=llm,
        gateway_override=gateway,
        discovery_override=_tool_calling_discovery,
        max_tool_invocations=3,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate",
        json={"prompt": "What concepts am I in in a #V#author_of relation with?"},
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("response") == (
        "You are in a represented #V#author_of relation with Test Paper One and Test Paper Two."
    )

    assert gateway.invocations
    assert gateway.invocations[0]["tool"] == "get_predicate_incidence"
    first_payload = gateway.invocations[0]["payload"]
    assert first_payload["concept_id"] == "#V#test_user"
    assert first_payload["predicate_filter"] == ["#V#author_of"]
    assert first_payload["namespace"] == "#V#test_user@test_org"
    if len(gateway.invocations) > 1:
        assert gateway.invocations[1]["tool"] == "find_relations_with_argument"
        second_payload = gateway.invocations[1]["payload"]
        assert second_payload["concept_id"] == "#V#test_user"
        assert second_payload["predicate_filter"] == ["#V#author_of"]
        assert second_payload["limit"] == 20
        assert second_payload["namespace"] == "#V#test_user@test_org"

    summariser_calls = [
        call
        for call in llm.calls
        if isinstance(call.get("prompt"), str)
        and call["prompt"].startswith(
            "Provide a final answer to the user now that the tool result is available."
        )
    ]
    assert summariser_calls
    final_summariser_context_text = "\n".join(
        str(message.get("content") or "")
        for message in (summariser_calls[-1].get("context") or [])
        if isinstance(message, dict)
    )
    assert "Predicate incidence summary" in final_summariser_context_text
    assert "#V#author_of" in final_summariser_context_text
    assert "Test Paper One" in final_summariser_context_text
    assert "Test Paper Two" in final_summariser_context_text
    if len(gateway.invocations) > 1:
        assert "Relation-bearing evidence excerpts" in final_summariser_context_text

    llm_debug = body.get("llm_debug") or {}
    tool_invocations = llm_debug.get("tool_invocations") or []
    recorded_tools = [
        (record.get("tool") or record.get("method"))
        for record in tool_invocations
        if isinstance(record, dict)
    ]
    assert recorded_tools
    assert "get_predicate_incidence" in recorded_tools
    if len(gateway.invocations) > 1:
        assert "find_relations_with_argument" in recorded_tools


def test_generate_threads_window_session_header_into_conversation_session_resolution(
    monkeypatch,
) -> None:
    llm = _IdentityLLM()
    app = _make_app(monkeypatch, llm=llm)

    captured: dict[str, Any] = {}

    def _capture_generate_session(**kwargs: Any) -> tuple[str, str | None, bool]:
        captured.update(kwargs)
        return "window-session-generated", None, False

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._ensure_generate_conversation_session",
        _capture_generate_session,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate",
        headers={"X-Von-Window-Session": "ws-browser-1910"},
        json={"prompt": "Who am I?"},
    )

    assert response.status_code == 200
    assert captured["window_session_id"] == "ws-browser-1910"
    assert captured["request_conversation_session_id"] is None


def test_generate_threads_resolved_namespace_into_chat_history_reads(
    monkeypatch,
) -> None:
    llm = _IdentityLLM()
    app = _make_app(monkeypatch, llm=llm)

    resolved_namespace = "#V#test_user@test_org"
    captured_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._resolve_generate_namespace_context",
        lambda **_kwargs: {
            "namespace": resolved_namespace,
            "namespace_source": "test_override",
            "effective_context_source": "test",
            "effective_context_namespace": resolved_namespace,
            "session_namespace": None,
            "candidates": [
                {"namespace": resolved_namespace, "source": "test_override"}
            ],
            "mismatch_detected": False,
            "org_scope_preferred": True,
        },
    )

    def _capture_chat_history(
        user_id: str,
        session_id: str,
        *,
        namespace: str | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        captured_calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "namespace": namespace,
            }
        )
        return []

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        _capture_chat_history,
    )

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "Who am I?"})

    assert response.status_code == 200
    assert len(captured_calls) >= 2
    assert all(call["namespace"] == resolved_namespace for call in captured_calls)


def test_generate_live_response_persistence_skips_best_effort_rag_indexing(
    monkeypatch,
) -> None:
    llm = _IdentityLLM()
    app = _make_app(monkeypatch, llm=llm)

    captured_calls: list[dict[str, Any]] = []

    def _capture_add_message_to_history(
        user_id: str,
        session_id: str,
        message: dict[str, Any],
        llm_debug_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        captured_calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "message": dict(message),
                "llm_debug_data": llm_debug_data,
                "skip_rag_indexing": kwargs.get("skip_rag_indexing"),
            }
        )

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        _capture_add_message_to_history,
    )

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "Who am I?"})

    assert response.status_code == 200
    assert captured_calls
    early_user_call = next(
        call for call in captured_calls if call["message"].get("role") == "user"
    )
    assert early_user_call["skip_rag_indexing"] is True
    assistant_call = next(
        call for call in captured_calls if call["message"].get("role") == "assistant"
    )
    assert assistant_call["skip_rag_indexing"] is True


def test_generate_tool_pipeline_persistence_skips_best_effort_rag_indexing(
    monkeypatch,
) -> None:
    llm = _EntityRelativeToolPipelineLLM()
    gateway = _EntityLookupGatewayStub()
    app = _make_app(
        monkeypatch,
        llm=llm,
        gateway_override=gateway,
        discovery_override=_tool_calling_discovery,
    )

    captured_calls: list[dict[str, Any]] = []

    def _capture_add_message_to_history(
        user_id: str,
        session_id: str,
        message: dict[str, Any],
        llm_debug_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        captured_calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "message": dict(message),
                "llm_debug_data": llm_debug_data,
                "skip_rag_indexing": kwargs.get("skip_rag_indexing"),
            }
        )

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        _capture_add_message_to_history,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate", json={"prompt": "What papers of mine do you know about?"}
    )

    assert response.status_code == 200
    assert captured_calls
    assert all(call["skip_rag_indexing"] is True for call in captured_calls)
    assert any(call["message"].get("role") == "assistant" for call in captured_calls)
