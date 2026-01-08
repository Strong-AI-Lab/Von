from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    _MissingToolCallDetectorSpec,
)


@dataclass(frozen=True)
class _TransportResult:
    payload: Any
    duration_ms: float


class _Gateway:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {
            "download_paper": {
                "category": "write",
                "description": "Download an arXiv paper and store as an artefact",
            },
            "finalise_cached_paper": {
                "category": "write",
                "description": "Upload cached arXiv PDF and register file copy",
            },
            "list_papers": {
                "category": "read",
                "description": "List cached papers",
            },
        }

    def invoke(self, tool_name: str, payload: Mapping[str, Any]) -> _TransportResult:
        self.invocations.append({"tool": tool_name, "payload": dict(payload)})
        return _TransportResult(payload={"success": True}, duration_ms=1.0)


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


def test_missing_tool_call_retry_forces_download_paper_over_list_papers():
    gateway = _Gateway()
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )

    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="fallback_missing_tool_call_detector",
        prompt_id=None,
        prompt_text=orchestrator._FALLBACK_MISSING_TOOL_CALL_PROMPT,
        model=None,
    )

    llm = _CapturingLLM(
        [
            "Here is the actual tool call.",
            "Done.",
        ]
    )

    orchestrator.run(
        prompt="Download arXiv:2506.16596 and store it as an artefact.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert any(call["tool"] == "download_paper" for call in gateway.invocations)
    assert not any(call["tool"] == "list_papers" for call in gateway.invocations)

    # The forced retry path should avoid an extra LLM call.
    assert len(llm.calls) == 2


def test_missing_tool_call_retry_forces_finalise_cached_paper_when_requested():
    gateway = _Gateway()
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )

    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="fallback_missing_tool_call_detector",
        prompt_id=None,
        prompt_text=orchestrator._FALLBACK_MISSING_TOOL_CALL_PROMPT,
        model=None,
    )

    llm = _CapturingLLM(
        [
            "Here is the actual tool call.",
            "Done.",
        ]
    )

    orchestrator.run(
        prompt="Finalise cached arXiv:2506.16596v2 and store it as an artefact.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert any(call["tool"] == "finalise_cached_paper" for call in gateway.invocations)
    assert not any(call["tool"] == "list_papers" for call in gateway.invocations)
