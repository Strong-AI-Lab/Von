from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Mapping, Optional, Sequence, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    MISSING_TOOL_CALL_WORKFLOW_ID,
    _MissingToolCallDetectorSpec,
)

_TEST_CLASSIFIER_PROMPT = "Answer YES or NO for: {response}"


class _Gateway:
    @staticmethod
    def describe_methods() -> dict[str, Any]:
        return {
            "search_concepts": {
                "category": "read",
                "description": "Search represented concepts by name and description",
            },
            "search_knowledge_base": {
                "category": "read",
                "description": "Semantic search over represented knowledge",
            },
            "search_web": {
                "category": "read",
                "description": "Search the public web",
            },
            "search_arxiv": {
                "category": "read",
                "description": "Search arXiv papers",
            },
            "download_paper": {
                "category": "write",
                "description": "Download an arXiv paper and store as an artefact",
            },
            "finalise_cached_paper": {
                "category": "write",
                "description": "Upload cached arXiv PDF and register file copy",
            },
            "materialise_scholarly_representation_for_file_copy": {
                "category": "write",
                "description": "Materialise scholarly-paper representation from file copy",
            },
            "list_papers": {
                "category": "read",
                "description": "List cached papers",
            },
        }


class _CapturingLLM:
    def __init__(self, responses: Sequence[str]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]] = None,
        model=None,
    ):
        self.calls.append(
            {"prompt": prompt, "context": list(context or []), "model": model}
        )
        if not self._responses:
            raise AssertionError("LLM called more times than expected")
        return self._responses.pop(0)


def _build_orchestrator_stub() -> InternalMCPChatOrchestrator:
    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    orchestrator._logger = logging.getLogger(__name__)
    orchestrator._gateway = cast(Any, _Gateway())
    orchestrator._follow_up_context_chars = 4000
    orchestrator._max_missing_tool_call_retries_per_turn = 3
    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="fallback_missing_tool_call_detector",
        prompt_id=None,
        prompt_text=_TEST_CLASSIFIER_PROMPT,
        model=None,
    )
    return orchestrator


def _uses_missing_tool_call_classifier_prompt(
    calls: Sequence[Mapping[str, Any]],
    classifier_prompt: str,
) -> bool:
    classifier_prefix = classifier_prompt.split("{response}", 1)[0].strip()
    for call in calls:
        prompt = call.get("prompt")
        if not isinstance(prompt, str):
            continue
        if classifier_prefix and classifier_prefix in prompt:
            return True
    return False


def test_missing_tool_call_assessment_skips_classifier_when_fallback_detector_matches():
    orchestrator = _build_orchestrator_stub()

    llm = _CapturingLLM(
        [
            "Here is the actual tool call.",
        ]
    )

    response_text = "Here is the actual tool call."
    assessment = orchestrator._assess_missing_tool_call(
        response_text=response_text,
        use_structured=False,
        interpretation=orchestrator._interpret_model_turn(response_text),
        llm_client=llm,
        model=None,
        classifier_model=None,
        aux_log=[],
        tool_call_parse_error=None,
        allow_semantic_retry=True,
    )

    assert assessment.retry_reason == "heuristic missing tool call"
    assert assessment.classifier_invoked is False
    assert not _uses_missing_tool_call_classifier_prompt(
        llm.calls, _TEST_CLASSIFIER_PROMPT
    )
    assert llm.calls == []


def test_missing_tool_call_retry_does_not_force_domain_specific_download_tool():
    orchestrator = _build_orchestrator_stub()

    prompt = "Download arXiv:2506.16596 and store it as an artefact."
    requirements = orchestrator._derive_prompt_tool_requirements(
        prompt,
        method_catalogue=_Gateway.describe_methods(),
        context_messages=[],
    )
    assert requirements["required_tools"] == []

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt=prompt,
        missing_required_tools=["download_paper"],
    )
    assert forced is None


def test_missing_tool_call_retry_does_not_force_domain_specific_finalise_tool():
    orchestrator = _build_orchestrator_stub()

    prompt = "Finalise cached arXiv:2506.16596v2 and store it as an artefact."
    requirements = orchestrator._derive_prompt_tool_requirements(
        prompt,
        method_catalogue=_Gateway.describe_methods(),
        context_messages=[],
    )
    assert requirements["required_tools"] == []

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt=prompt,
        missing_required_tools=["finalise_cached_paper"],
    )
    assert forced is None


def test_missing_tool_call_retry_forces_explicit_scholarly_materialisation_tool():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="Represent the corresponding paper from #V#uploaded_file_copy_abc123.",
        missing_required_tools=["materialise_scholarly_representation_for_file_copy"],
        missing_required_scholarly_representation_file_copy_ids=[
            "#V#uploaded_file_copy_abc123"
        ],
    )

    assert forced is not None
    assert forced == [
        {
            "action": "call_tool",
            "tool": "materialise_scholarly_representation_for_file_copy",
            "payload": {"concept_id": "#V#uploaded_file_copy_abc123"},
        }
    ]


def test_missing_tool_call_retry_forces_guided_kb_and_web_search() -> None:
    orchestrator = _build_orchestrator_stub()
    prompt = (
        "What open-source projects released recently look most aligned with the "
        "research themes already in my KB?"
    )

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [
            {
                "role": "system",
                "content": (
                    "Selector guidance: first use search_knowledge_base to extract "
                    "research themes from the user's Knowledge Base, then use web "
                    "search to find recent projects."
                ),
            }
        ],
        user_prompt=prompt,
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "search_knowledge_base",
            "payload": {"query": prompt, "top_k": 5},
        },
        {
            "action": "call_tool",
            "tool": "search_web",
            "payload": {"query": prompt, "max_results": 5},
        },
    ]


def test_missing_tool_call_retry_forces_guided_kb_and_web_search_from_turn_contract():
    orchestrator = _build_orchestrator_stub()
    prompt = (
        "What open-source projects released recently look most aligned with the "
        "research themes already in my KB?"
    )

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt=prompt,
        turn_expected_outcome_contract={
            "summary": "Return recent open-source projects grounded against the user's represented research themes.",
            "selector_guidance": (
                "First use search_knowledge_base to extract current research themes "
                "from the KB, then use search_web to identify recent open-source "
                "projects aligned with those themes."
            ),
            "grounding_requirement": (
                "Ground the shortlist in represented themes plus current public web evidence."
            ),
        },
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "search_knowledge_base",
            "payload": {"query": prompt, "top_k": 5},
        },
        {
            "action": "call_tool",
            "tool": "search_web",
            "payload": {"query": prompt, "max_results": 5},
        },
    ]


def test_missing_tool_call_retry_forces_guided_concept_search_when_contract_names_it():
    orchestrator = _build_orchestrator_stub()
    prompt = (
        "What open-source projects released recently look most aligned with the "
        "research themes already in my KB?"
    )

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt=prompt,
        turn_expected_outcome_contract={
            "selector_guidance": (
                "First, use search_knowledge_base and search_concepts to extract "
                "the user's research themes. Second, use search_web to identify "
                "recent open-source releases."
            ),
        },
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "search_knowledge_base",
            "payload": {"query": prompt, "top_k": 5},
        },
        {
            "action": "call_tool",
            "tool": "search_concepts",
            "payload": {
                "query": prompt,
                "match_type": "all",
                "include_description": True,
                "limit": 8,
            },
        },
        {
            "action": "call_tool",
            "tool": "search_web",
            "payload": {"query": prompt, "max_results": 5},
        },
    ]


def test_run_missing_tool_call_recovery_workflow_projects_turn_contract_from_trace():
    orchestrator = _build_orchestrator_stub()

    class _WorkflowRegistry:
        @staticmethod
        def get(workflow_id: str) -> object | None:
            if workflow_id == MISSING_TOOL_CALL_WORKFLOW_ID:
                return object()
            return None

    captured: dict[str, Any] = {}

    def _execute_workflow(workflow_id: str, **kwargs: Any) -> Any:
        captured["workflow_id"] = workflow_id
        captured["data"] = kwargs.get("data")
        return SimpleNamespace(data={})

    orchestrator._workflow_registry = cast(Any, _WorkflowRegistry())
    orchestrator.execute_workflow = cast(Any, _execute_workflow)
    orchestrator._build_follow_up_llm_context = cast(
        Any, lambda context, max_chars: list(context)
    )
    orchestrator._project_missing_tool_call_aux_telemetry = cast(
        Any, lambda aux_log, recovery_data: None
    )

    request = SimpleNamespace(trace=None)
    environment = SimpleNamespace(
        llm_client=object(),
        user_namespace="#V#test_user",
        auxiliary_system_prompt=None,
        max_tool_invocations=4,
        max_tool_result_chars=8000,
        max_tool_result_field_chars=2000,
        default_gmail_profile=None,
        user_concept_id="#V#test_user",
        org_concept_id="#V#sail",
    )
    turn_contract = {
        "summary": "Return recent open-source projects aligned to the user's represented themes.",
        "selector_guidance": (
            "First use search_knowledge_base to extract the current research themes, "
            "then use search_web to find recent aligned projects."
        ),
        "grounding_requirement": (
            "Ground the answer in represented themes and current web evidence."
        ),
        "answering_guidance": "Return a grounded shortlist rather than a refusal.",
    }

    result = orchestrator._run_missing_tool_call_recovery_workflow(
        request=cast(Any, request),
        environment=cast(Any, environment),
        data={
            "prompt": (
                "What open-source projects released recently look most aligned with "
                "the research themes already in my KB?"
            ),
            "augmented_context": [],
            "selected_workflow_trace": {"expected_outcome_contract": turn_contract},
            "aux_llm_calls": [],
            "model_for_stage": lambda stage: "recovery-model",
            "record_llm_call": None,
            "policy_state": None,
            "prefer_default_model": False,
            "registry_snapshot": {},
            "emit_progress": None,
        },
        response_text="I will look that up.",
        interpretation=None,
        use_structured=True,
        tool_call_parse_error=None,
        tool_calls=None,
        default_model="gemma4:26b",
    )

    assert result == {}
    assert captured["workflow_id"] == MISSING_TOOL_CALL_WORKFLOW_ID
    workflow_context = captured["data"]
    assert isinstance(workflow_context, Mapping)
    assert workflow_context.get("turn_expected_outcome_contract") == turn_contract
    assert (
        workflow_context.get("turn_expected_outcome_summary")
        == turn_contract["summary"]
    )
    assert (
        workflow_context.get("turn_selector_guidance")
        == turn_contract["selector_guidance"]
    )
    assert (
        workflow_context.get("turn_expected_grounding_requirement")
        == turn_contract["grounding_requirement"]
    )
    assert (
        workflow_context.get("turn_answering_guidance")
        == turn_contract["answering_guidance"]
    )


def test_tool_calling_plan_applies_parent_guided_retry_fallback_when_nested_recovery_returns_no_calls():
    orchestrator = _build_orchestrator_stub()

    orchestrator._build_stage_llm_context = cast(
        Any, lambda **kwargs: (list(kwargs.get("base_context") or []), {})
    )
    orchestrator._run_llm_with_fallbacks = cast(
        Any, lambda **kwargs: ("I will search the KB and web now.", "model", None)
    )
    orchestrator._run_missing_tool_call_recovery_workflow = cast(
        Any, lambda **kwargs: {}
    )
    orchestrator._store_prompt_requirement_evaluation = cast(
        Any, lambda data, prompt_requirements: None
    )

    class _PromptRequirements:
        required_tools: list[str] = []
        required_fetch_concept_ids: list[str] = []
        required_read_file_copy_ids: list[str] = []
        required_scholarly_representation_file_copy_ids: list[str] = []
        required_create_type_name: str | None = None
        required_url_extraction_tool: str | None = None
        required_url_extraction_url: str | None = None
        missing_tools: list[str] = []
        missing_fetch_concept_ids: list[str] = []
        missing_read_file_copy_ids: list[str] = []
        missing_scholarly_representation_file_copy_ids: list[str] = []
        missing_retry_reason: str | None = None

    orchestrator._evaluate_prompt_requirements = cast(
        Any, lambda **kwargs: _PromptRequirements()
    )

    request = SimpleNamespace(
        data={
            "prompt": (
                "What open-source projects released recently look most aligned with "
                "the research themes already in my KB?"
            ),
            "augmented_context": [],
            "policy_state": SimpleNamespace(enabled=False, policy=None),
            "registry_snapshot": {},
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#sail",
            "model_for_stage": lambda stage: "gemma4:26b",
            "record_llm_call": lambda **kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "emit_progress": None,
            "emit_phase_transition": None,
            "turn_selector_guidance": (
                "First, use retrieval tools (search_knowledge_base, "
                "get_text_relations_summary) to extract the user's current "
                "research themes. Second, use web search (search_web) to find "
                "recent open-source releases. Third, use an evaluation or "
                "comparison step to match the two."
            ),
            "turn_expected_grounding_requirement": (
                "Alignment must be justified by explicit links between the "
                "properties/themes of the identified projects and the specific "
                "concepts or relations retrieved from the user's KB/Vontology."
            ),
            "turn_expected_outcome_summary": (
                "Identify recent open-source projects that demonstrate high "
                "semantic or topical alignment with the research themes, "
                "entities, and relationships already represented in the user's "
                "authenticated knowledge base."
            ),
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gemma4:26b",
            max_tool_invocations=4,
        ),
        trace=None,
        workflow_id="#V#tool_calling_workflow",
        workflow_state_id="respond",
        workflow_state_metadata={},
        action_id="tool_calling.respond",
    )

    result = orchestrator._action_tool_calling_plan(cast(Any, request))

    assert result.outputs["tool_calls_present"] is True
    assert result.outputs["direct_response"] is False
    assert result.outputs["missing_tool_call_recovery_outcome"] == (
        "retry_succeeded_parent_fallback"
    )
    assert result.outputs["tool_calls"] == [
        {
            "action": "call_tool",
            "tool": "search_knowledge_base",
            "payload": {
                "query": request.data["prompt"],
                "top_k": 5,
            },
        },
        {
            "action": "call_tool",
            "tool": "search_web",
            "payload": {
                "query": request.data["prompt"],
                "max_results": 5,
            },
        },
    ]


def test_missing_tool_call_retry_does_not_force_guided_retrieval_without_guidance():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What is the capital of France?",
    )

    assert forced is None
