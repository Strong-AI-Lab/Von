from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    _MissingToolCallDetectorSpec,
)


class _Gateway:
    @staticmethod
    def describe_methods() -> dict[str, Any]:
        return {
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
    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="fallback_missing_tool_call_detector",
        prompt_id=None,
        prompt_text=InternalMCPChatOrchestrator._FALLBACK_MISSING_TOOL_CALL_PROMPT,
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
        llm.calls, orchestrator._FALLBACK_MISSING_TOOL_CALL_PROMPT
    )
    assert llm.calls == []


def test_missing_tool_call_retry_forces_download_paper_over_list_papers():
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
    assert forced is not None
    assert any(call["tool"] == "download_paper" for call in forced)
    assert not any(call["tool"] == "list_papers" for call in forced)


def test_missing_tool_call_retry_forces_finalise_cached_paper_when_requested():
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
    assert forced is not None
    assert any(call["tool"] == "finalise_cached_paper" for call in forced)
    assert not any(call["tool"] == "list_papers" for call in forced)


def test_missing_tool_call_retry_forces_explicit_scholarly_materialisation_tool():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="Represent the corresponding paper from #V#uploaded_file_copy_abc123.",
        missing_required_tools=[
            "materialise_scholarly_representation_for_file_copy"
        ],
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
