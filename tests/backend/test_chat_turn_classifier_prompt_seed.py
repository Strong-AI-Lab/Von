"""Structural invariants for the canonical workflow-selector prompt seed.

JVNAUTOSCI-2131: The chat-turn-classifier prompt is the authoritative routing
surface for conversation turns. When the user expresses authoring intent
(create a predicate, define a type, make an instance, link concepts, extend
the ontology), the selector must prefer an authoring-role candidate over the
generic ``#V#tool_calling_workflow`` retrieval workflow. The failing turn
captured by JVNAUTOSCI-2131 (request id ``428a8b18-a264-4b83-bbe8-499df563bc14``)
demonstrated the prior prompt body silently routing an explicit predicate-pair
authoring request to ``#V#tool_calling_workflow``, which then ran read-only
inspection and produced no writes.

These tests pin the prompt body so future edits cannot regress the authoring-
intent routing rules without an explicit, visible change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PROMPT_SEED_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_chat_turn_classifier_seed.md"
)


@pytest.fixture(scope="module")
def prompt_text() -> str:
    text = _PROMPT_SEED_PATH.read_text(encoding="utf-8")
    assert text.strip(), "selector prompt seed must not be empty"
    return text


def test_prompt_seed_concept_header(prompt_text: str) -> None:
    assert prompt_text.lstrip().startswith("# chat_turn_classifier_prompt"), (
        "selector prompt seed must declare its canonical concept id in the header"
    )


def test_prompt_recognises_authoring_intent(prompt_text: str) -> None:
    """The prompt must teach the selector to recognise authoring intent."""

    lowered = prompt_text.lower()
    assert "authoring intent" in lowered, (
        "selector prompt must explicitly name authoring intent so the selector "
        "can distinguish creation/extension turns from retrieval turns"
    )
    # The Jira description (JVNAUTOSCI-2131) gives concrete authoring verbs
    # the prompt should recognise. Pin a representative subset so the rule is
    # not silently weakened to a vacuous mention.
    for verb in (
        "create",
        "add",
        "define",
        "extend",
        "link",
    ):
        assert verb in lowered, (
            f"selector prompt should mention the authoring verb {verb!r} so the "
            "selector recognises common authoring phrasings"
        )


def test_prompt_prefers_authoring_role_candidates(prompt_text: str) -> None:
    """Authoring-role candidates must be preferred when present."""

    lowered = prompt_text.lower()
    assert 'role = "authoring"' in lowered or "role=\"authoring\"" in lowered, (
        "selector prompt must reference the routing-profile role 'authoring' so "
        "the selector can identify authoring-role candidates from the candidate "
        "list metadata"
    )
    assert "authoring_intent_required" in lowered, (
        "selector prompt must reference the authoring_intent_required routing "
        "profile flag so the selector knows which candidates are gated on "
        "authoring intent"
    )


def test_prompt_excludes_tool_calling_workflow_for_authoring(
    prompt_text: str,
) -> None:
    """tool_calling_workflow must not be the chosen route for authoring turns."""

    # The exact wording matters less than the presence of an explicit rule
    # forbidding tool_calling_workflow as the authoring-turn destination when
    # an authoring-role candidate exists. Search for both the workflow id and
    # an authoring-related qualifier in close proximity.
    text = prompt_text
    tcw_index = text.find("#V#tool_calling_workflow")
    assert tcw_index != -1, "prompt seed must reference #V#tool_calling_workflow"

    # Find the rule that explicitly forbids tool_calling_workflow for authoring
    # intent. We look for the phrase "Do not select" near tool_calling_workflow
    # and an authoring marker.
    forbid_phrase = "do not select `#v#tool_calling_workflow`"
    lowered = text.lower()
    assert forbid_phrase in lowered, (
        "selector prompt must contain an explicit rule forbidding "
        "#V#tool_calling_workflow for authoring-intent turns"
    )
    # Confirm the forbid-rule is contextualised with authoring intent.
    forbid_index = lowered.find(forbid_phrase)
    window = lowered[forbid_index : forbid_index + 600]
    assert "authoring" in window, (
        "the forbid-rule for #V#tool_calling_workflow must be scoped to "
        "authoring-intent turns rather than blanket-banning the workflow"
    )


def test_prompt_retains_general_tool_calling_fallback_for_retrieval(
    prompt_text: str,
) -> None:
    """The non-authoring grounded-retrieval fallback to tool_calling_workflow
    must remain so plain retrieval turns continue to route correctly."""

    lowered = prompt_text.lower()
    # The general fallback rule should still allow tool_calling_workflow when
    # the request is *not* an authoring-intent turn.
    assert "grounded retrieval" in lowered, (
        "selector prompt must retain the grounded-retrieval fallback guidance"
    )
    assert "#V#tool_calling_workflow" in prompt_text, (
        "the general tool-calling workflow must remain a valid routing target "
        "for non-authoring grounded-retrieval turns"
    )


def test_prompt_includes_authoring_canonical_example(prompt_text: str) -> None:
    """A canonical valid-output example for an authoring turn must be present
    so the selector has a concrete shape to mimic."""

    # The example block uses #V#von_workflow_creation_workflow (the real
    # authoring-role workflow advertised by Vontology routing profiles today).
    assert "#V#von_workflow_creation_workflow" in prompt_text, (
        "selector prompt should include a canonical authoring-route example "
        "naming an authoring-role workflow id"
    )
    # The example reasoning must mention authoring intent so the canonical
    # output models the rule the selector is being taught.
    lowered = prompt_text.lower()
    creation_index = lowered.find("#v#von_workflow_creation_workflow")
    assert creation_index != -1
    window = lowered[creation_index : creation_index + 400]
    assert "authoring" in window, (
        "the canonical authoring-route example must mention authoring intent "
        "in its reasoning so the model has an aligned exemplar"
    )
