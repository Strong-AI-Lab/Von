"""Integration test for the #V#email_resource_link_extraction workflow.

JVNAUTOSCI-2118 (Phase 1 vertical slice). Drives the *exact same workflow
definition* that the publishing script authors in Vontology through the real
``WorkflowExecutor``. The fetch step's tool action and the LLM call inside
``extract_signals_from_tool_result`` are the only stubbed surfaces; everything
in between -- the authoring spec, mappings, action registration, context flow,
hint-body resolution path -- is exercised against production code.

Why this is the "nearest real path":

* Live Gmail validation requires a real Zhan-inbox message containing an
  arXiv link, which is not currently available in the configured Gmail
  profile.
* This test validates the full workflow runtime, including:
    - ``build_workflow_definition_from_authoring_spec`` produces an executable
      definition matching what was published to Vontology.
    - ``fetch_payload`` step writes ``tool_payload`` into shared context via
      the canonical ``hasOutputContextMap`` edge.
    - ``extract_signals`` step receives ``tool_payload`` and the static
      ``source_tool_concept`` binding, calls into the real
      ``tool_result_hints.extract_signals_from_tool_result`` primitive, and
      writes ``signals`` back into context.
    - Workflow terminates at ``done`` (not ``failed``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.publish_email_resource_link_extraction_workflow import (
    _build_authoring_spec,
)
from src.backend.services.email_source_representation_convergence_workflow_vontology_service import (
    _REPO_SEED_ASSET_PATH,
    EMAIL_RESOURCE_LINK_EXTRACTION_WORKFLOW_ID,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.tool_result_hint_actions import (
    register_tool_result_hint_actions,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
)
from src.backend.workflows.workflow_concept_authority_service import (
    build_repo_seed_workflow_definitions,
)

_GMAIL_PAYLOAD = {
    "id": "test_message_id_001",
    "snippet": (
        "Hi team -- worth a look at this preprint: "
        "https://arxiv.org/abs/2501.12345 -- relevant to our routing work."
    ),
    "headers": {
        "Subject": "Re: routing paper",
        "From": "Zhan <zhan@example.com>",
    },
    "body": (
        "Hi team -- worth a look at this preprint: "
        "https://arxiv.org/abs/2501.12345 -- relevant to our routing work."
    ),
}


# Spec output schema (JVNAUTOSCI-2118 / 2112): list of items shaped as
# {type, identifier, url, intent_context, sender_ask}. The workflow itself is
# tool-agnostic; arxiv here is only a concrete instance of the generic
# {arxiv, doi, url, pdf} type set the hint covers.
_LLM_RESPONSE_JSON = (
    '{"items": ['
    '{"reference_kind": "arxiv",'
    ' "arxiv_id": "2501.12345",'
    ' "source_uri": "https://arxiv.org/abs/2501.12345",'
    ' "prompt": "worth a look at this preprint: '
    'https://arxiv.org/abs/2501.12345 -- relevant to our routing work."}'
    '], "arxiv_items": ['
    '{"reference_kind": "arxiv", "arxiv_id": "2501.12345",'
    ' "source_uri": "https://arxiv.org/abs/2501.12345"}'
    ']}'
)

_HINT_SEED_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "gmail_get_message_paper_signal_extraction_hint_seed.json"
)


class _FakeLLM:
    """Minimal LLM client matching the shape consumed by tool_result_hints."""

    def __init__(self, response: str = _LLM_RESPONSE_JSON) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = response

    def generate(
        self,
        *,
        prompt: str,
        context: Any = None,
        model: str | None = None,
    ) -> str:
        self.calls.append({"prompt": prompt, "context": context, "model": model})
        return self.response


def _gmail_get_message_handler(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    """Stub the gmail_get_message tool action with a synthetic payload."""

    inputs = dict(request.inputs or {})
    assert inputs.get("message_id") == "test_message_id_001"
    # Mirror the canonical MCP envelope: workflow expects ``result`` under
    # the action outputs (per the tool_output_context_mapping).
    return WorkflowActionResult(
        status="success",
        outputs={"result": _GMAIL_PAYLOAD},
    )


def test_email_resource_link_extraction_workflow_runs_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real authoring path -- same code the publish script calls.
    spec = _build_authoring_spec()
    definition = build_workflow_definition_from_authoring_spec(spec)
    assert definition.workflow_id == "#V#email_resource_link_extraction"
    assert definition.initial_state == "fetch_payload"
    assert "extract_signals" in definition.states
    assert "done" in definition.states
    assert "failed" in definition.states

    # Stub only the hint-resolution surface (Vontology read) so the test does
    # not require a populated test_von_db. The real action handler is exercised.
    def _fake_resolve_hint_body(
        tool_concept_id: str,
        hint_predicate_id: str,
        *,
        lang: str = "en-NZ",
    ) -> str:
        assert tool_concept_id == "#V#gmail_get_message_tool"
        assert hint_predicate_id == "#V#output_item_signal_extraction_hint"
        return str(json.loads(_HINT_SEED_PATH.read_text(encoding="utf-8"))["hint"])

    monkeypatch.setattr(
        "src.backend.services.tool_result_hints.resolve_hint_body",
        _fake_resolve_hint_body,
    )

    # Build registry: the generic action under test, plus a stub for the
    # tool-action used by the fetch step. This avoids needing the full MCP
    # gateway / fallback bridge.
    registry = ActionRegistry()
    register_tool_result_hint_actions(registry)
    registry.register(
        ActionSpec(
            action_id="gmail_get_message",
            handler=_gmail_get_message_handler,
        )
    )

    fake_llm = _FakeLLM()

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=fake_llm,
            user_namespace="#V#test_user",
        ),
        data={"message_id": "test_message_id_001"},
    )

    assert result.completed is True, (
        f"workflow did not complete; final_state={result.final_state!r} "
        f"data={result.data!r}"
    )
    assert result.final_state == "done", (
        f"workflow ended at unexpected state: {result.final_state!r}; "
        f"data={result.data!r}"
    )

    # Context propagation: fetch_payload wrote tool_payload, extract_signals
    # consumed it and wrote signals back.
    assert result.data.get("tool_payload") == _GMAIL_PAYLOAD
    signals = result.data.get("signals")
    assert isinstance(signals, dict)
    items = signals.get("items")
    assert isinstance(items, list) and len(items) == 1
    item = items[0]
    assert item["reference_kind"] == "arxiv"
    assert item["arxiv_id"] == "2501.12345"
    assert item["source_uri"] == "https://arxiv.org/abs/2501.12345"
    assert "routing" in item["prompt"].lower()

    # The LLM was called exactly once, with a prompt body that came from the
    # authored (stubbed) hint, not from Python literals -- and the prompt
    # carries the generic type set rather than any arxiv-specific schema.
    assert len(fake_llm.calls) == 1
    prompt = fake_llm.calls[0]["prompt"]
    assert "bare paper mention is sufficient representation intent" in prompt
    assert "Do not guess an arXiv ID" in prompt
    assert "reference_kind" in prompt


@pytest.mark.parametrize(
    ("message_id", "body", "items"),
    (
        (
            "bare-title-1",
            "Attention Is All You Need",
            [
                {
                    "reference_kind": "metadata",
                    "title": "Attention Is All You Need",
                    "prompt": "Attention Is All You Need",
                }
            ],
        ),
        (
            "bare-title-2",
            "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
            [
                {
                    "reference_kind": "metadata",
                    "title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
                    "prompt": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
                }
            ],
        ),
        (
            "bare-arxiv-3",
            "https://arxiv.org/abs/1706.03762",
            [
                {
                    "reference_kind": "arxiv",
                    "arxiv_id": "1706.03762",
                    "source_uri": "https://arxiv.org/abs/1706.03762",
                    "prompt": "https://arxiv.org/abs/1706.03762",
                }
            ],
        ),
        (
            "bare-doi-4",
            "10.1145/3743093.3770985",
            [
                {
                    "reference_kind": "doi",
                    "doi": "10.1145/3743093.3770985",
                    "prompt": "10.1145/3743093.3770985",
                }
            ],
        ),
        (
            "bare-multi-5",
            "Attention Is All You Need\nBERT: Pre-training of Deep Bidirectional Transformers for Language Understanding\nhttps://arxiv.org/abs/2005.14165",
            [
                {
                    "reference_kind": "metadata",
                    "title": "Attention Is All You Need",
                    "prompt": "Attention Is All You Need",
                },
                {
                    "reference_kind": "metadata",
                    "title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
                    "prompt": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
                },
                {
                    "reference_kind": "arxiv",
                    "arxiv_id": "2005.14165",
                    "source_uri": "https://arxiv.org/abs/2005.14165",
                    "prompt": "https://arxiv.org/abs/2005.14165",
                },
            ],
        ),
    ),
)
def test_reset_state_bare_mentions_are_all_extracted_without_sender_ask(
    monkeypatch: pytest.MonkeyPatch,
    message_id: str,
    body: str,
    items: list[dict[str, Any]],
) -> None:
    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[_REPO_SEED_ASSET_PATH],
        target_workflow_ids=[EMAIL_RESOURCE_LINK_EXTRACTION_WORKFLOW_ID],
    )[EMAIL_RESOURCE_LINK_EXTRACTION_WORKFLOW_ID]
    hint = str(json.loads(_HINT_SEED_PATH.read_text(encoding="utf-8"))["hint"])
    monkeypatch.setattr(
        "src.backend.services.tool_result_hints.resolve_hint_body",
        lambda *_args, **_kwargs: hint,
    )
    registry = ActionRegistry()
    register_tool_result_hint_actions(registry)

    def fetch_message(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "id": message_id,
                    "headers": {"Subject": body.splitlines()[0]},
                    "body": body,
                }
            },
        )

    registry.register(
        ActionSpec(action_id="workflow_mcp.invoke_tool", handler=fetch_message)
    )
    response = json.dumps(
        {
            "items": items,
            "arxiv_items": [
                item for item in items if item["reference_kind"] == "arxiv"
            ],
        }
    )
    result = WorkflowExecutor(registry=registry, max_transitions=8).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_FakeLLM(response),
            model="gpt-5.6-terra",
            user_namespace="#V#test_user",
        ),
        data={"message_id": message_id, "gmail_profile": "test-gmail"},
    )

    assert result.completed is True
    assert result.final_state == "done"
    assert result.data["resource_references"] == items
    assert len(result.data["resource_references"]) == len(items)
    assert all("sender_ask" not in item for item in result.data["resource_references"])
