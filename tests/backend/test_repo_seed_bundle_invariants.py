"""Structural invariants for repo seed bundles.

JVNAUTOSCI-2132 L0 deliverable. These assertions are intentionally narrow:
they only check structural properties of the seed-bundle JSON and prompt
seed Markdown files in ``src/backend/workflows/repo_seed_bundles/``. They
do not bootstrap, mutate, or otherwise treat the bundles as authoritative;
Vontology remains the authority surface, and bundles only seed missing
state at runtime.

The point is to fail fast on edits that break documented invariants
(missing ``seed_version``, dangling ``to_state`` references, regressions
of the canonical conversation-turn workflow's completion-gate /
recovery-decision wiring, or removal of the ``Thinking Card Mode``
adaptation rules from the recovery prompt seed) before they reach the
runtime seed-version gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SEED_BUNDLE_DIR = PROJECT_ROOT / "src/backend/workflows/repo_seed_bundles"
WORKFLOW_BUNDLE_SCHEMA = "repo_seed_workflow_bundle.v1"

CANONICAL_BUNDLE_PATH = (
    SEED_BUNDLE_DIR / "canonical_workflow_publication_seed_bundle.json"
)
RECOVERY_PROMPT_SEED_PATH = (
    SEED_BUNDLE_DIR / "prompt_turn_execution_recovery_decision_seed.md"
)
RECOVERY_PROMPT_CONCEPT_ID = "#V#prompt_turn_execution_recovery_decision"
# Workflow whose completion_gate / recovery_decision wiring is enforced by
# the JVNAUTOSCI-2130 invariants below. The conversation-turn execution
# workflow owns the post-response completion_gate that, on a missing
# user-facing response, must route to recovery_decision.
CONVERSATION_TURN_WORKFLOW_ID = "#V#conversation_turn_execution_workflow"


def _all_seed_bundles() -> list[Path]:
    return sorted(SEED_BUNDLE_DIR.glob("*.json"))


def _workflow_bundles() -> list[Path]:
    bundles: list[Path] = []
    for path in _all_seed_bundles():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and (
            payload.get("schema_version") == WORKFLOW_BUNDLE_SCHEMA
        ):
            bundles.append(path)
    return bundles


def _iter_workflow_steps(bundle_payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for workflow in bundle_payload.get("workflows", []) or []:
        if not isinstance(workflow, dict):
            continue
        spec = workflow.get("publication_spec") or {}
        if not isinstance(spec, dict):
            continue
        for step in spec.get("steps", []) or []:
            if isinstance(step, dict):
                yield step


def _find_state(bundle_payload: dict[str, Any], state_id: str) -> dict[str, Any] | None:
    for step in _iter_workflow_steps(bundle_payload):
        if step.get("state_id") == state_id:
            return step
    return None


def _find_workflow(
    bundle_payload: dict[str, Any], workflow_id: str
) -> dict[str, Any] | None:
    for workflow in bundle_payload.get("workflows", []) or []:
        if isinstance(workflow, dict) and workflow.get("workflow_id") == workflow_id:
            return workflow
    return None


def _find_state_in_workflow(
    workflow: dict[str, Any], state_id: str
) -> dict[str, Any] | None:
    spec = workflow.get("publication_spec") or {}
    if not isinstance(spec, dict):
        return None
    for step in spec.get("steps") or []:
        if isinstance(step, dict) and step.get("state_id") == state_id:
            return step
    return None


def _bundled_prompt_concept_ids() -> set[str]:
    """Concept IDs whose prompt body is shipped inline in a prompt seed bundle.

    Prompt seed bundles use ``schema_version`` ending in
    ``_prompt_seed_bundle.v1`` and list prompts with ``concept_id`` and
    ``content``. Such prompts do not need a sibling ``_seed.md`` file.
    """

    bundled: set[str] = set()
    for path in _all_seed_bundles():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        schema = payload.get("schema_version") or ""
        if not isinstance(schema, str) or "prompt_seed_bundle" not in schema:
            continue
        for prompt in payload.get("prompts") or []:
            if isinstance(prompt, dict):
                concept_id = prompt.get("concept_id")
                if isinstance(concept_id, str):
                    bundled.add(concept_id)
    return bundled


@pytest.mark.parametrize("bundle_path", _all_seed_bundles(), ids=lambda p: p.name)
def test_seed_bundle_json_parses(bundle_path: Path) -> None:
    """Every seed-bundle JSON file must be valid JSON."""

    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), (
        f"{bundle_path.name}: top-level JSON must be an object"
    )


@pytest.mark.parametrize("bundle_path", _workflow_bundles(), ids=lambda p: p.name)
def test_workflow_seed_bundle_declares_seed_version(bundle_path: Path) -> None:
    """Workflow seed bundles must declare a non-empty ``seed_version`` string."""

    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    seed_version = payload.get("seed_version")
    assert isinstance(seed_version, str) and seed_version.strip(), (
        f"{bundle_path.name}: missing or empty seed_version"
    )


@pytest.mark.parametrize("bundle_path", _workflow_bundles(), ids=lambda p: p.name)
def test_workflow_state_transitions_reference_existing_states(
    bundle_path: Path,
) -> None:
    """Every ``to_state`` referenced by a transition must name a defined state.

    A state is "defined" when it appears as ``state_id`` somewhere in the
    same workflow's ``publication_spec.steps`` list. Dangling transitions
    silently break workflow execution at runtime, and the seed-version
    gate alone will not catch them.
    """

    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    failures: list[str] = []
    for workflow in payload.get("workflows", []) or []:
        if not isinstance(workflow, dict):
            continue
        spec = workflow.get("publication_spec") or {}
        if not isinstance(spec, dict):
            continue
        steps = [s for s in (spec.get("steps") or []) if isinstance(s, dict)]
        defined_state_ids = {
            s.get("state_id") for s in steps if s.get("state_id") is not None
        }
        workflow_id = (
            workflow.get("workflow_id")
            or workflow.get("workflow_concept_id")
            or workflow.get("name")
            or "<unknown-workflow>"
        )
        initial_state = spec.get("initial_state")
        if initial_state is not None and initial_state not in defined_state_ids:
            failures.append(
                f"{workflow_id}: initial_state={initial_state!r} is not defined"
            )
        for step in steps:
            owner_state = step.get("state_id") or "<unnamed>"
            for transition in step.get("conditional_transitions") or []:
                if not isinstance(transition, dict):
                    continue
                target = transition.get("to_state")
                if target is None:
                    continue
                if target not in defined_state_ids:
                    failures.append(
                        f"{workflow_id}: state {owner_state!r} transitions "
                        f"to undefined state {target!r}"
                    )

    assert failures == [], (
        f"{bundle_path.name}: dangling state transitions: {failures}"
    )


def test_canonical_completion_gate_routes_empty_response_to_recovery() -> None:
    """JVNAUTOSCI-2130 wiring must remain intact in the seed bundle.

    The canonical conversation-turn workflow's ``completion_gate`` state
    must route empty/missing ``response_text`` and the
    ``completion_gate_requires_follow_up`` flag to ``recovery_decision``,
    and otherwise transition to ``completed``.
    """

    payload = json.loads(CANONICAL_BUNDLE_PATH.read_text(encoding="utf-8"))
    workflow = _find_workflow(payload, CONVERSATION_TURN_WORKFLOW_ID)
    assert workflow is not None, (
        f"{CONVERSATION_TURN_WORKFLOW_ID} not found in canonical bundle"
    )
    completion_gate = _find_state_in_workflow(workflow, "completion_gate")
    assert completion_gate is not None, (
        f"completion_gate state not found in {CONVERSATION_TURN_WORKFLOW_ID}"
    )

    transitions = completion_gate.get("conditional_transitions") or []
    assert transitions, "completion_gate has no conditional_transitions"

    reasons_to_targets: dict[str, str] = {}
    for transition in transitions:
        reason = transition.get("reason")
        target = transition.get("to_state")
        if isinstance(reason, str) and isinstance(target, str):
            reasons_to_targets[reason] = target

    assert reasons_to_targets.get("missing_user_facing_response") == "recovery_decision"
    assert reasons_to_targets.get("follow_up_required") == "recovery_decision"
    assert reasons_to_targets.get("completion_gate_decided") == "completed"

    # Verify the missing-response branch genuinely keys on response_text
    # being null or empty rather than some unrelated condition.
    missing_response_branch = next(
        (t for t in transitions if t.get("reason") == "missing_user_facing_response"),
        None,
    )
    assert missing_response_branch is not None
    spec = missing_response_branch.get("condition_spec") or {}
    assert spec.get("kind") == "any"
    sub_kinds = {
        (sub.get("kind"), sub.get("key"))
        for sub in spec.get("conditions") or []
        if isinstance(sub, dict)
    }
    assert ("context_is_null", "response_text") in sub_kinds
    assert ("context_value_equals", "response_text") in sub_kinds


def test_canonical_recovery_decision_exposes_thinking_card_mode_context() -> None:
    """The ``recovery_decision`` LLM step must surface the context fields the
    JVNAUTOSCI-2130 prompt depends on: ``thinking_card_mode``,
    ``invocations``, ``tool_messages``, and ``response_text``.
    """

    payload = json.loads(CANONICAL_BUNDLE_PATH.read_text(encoding="utf-8"))
    workflow = _find_workflow(payload, CONVERSATION_TURN_WORKFLOW_ID)
    assert workflow is not None
    recovery_decision = _find_state_in_workflow(workflow, "recovery_decision")
    assert recovery_decision is not None, (
        f"recovery_decision state not found in {CONVERSATION_TURN_WORKFLOW_ID}"
    )

    llm_policy = recovery_decision.get("llm_policy") or {}
    context_fields = llm_policy.get("context_fields") or []
    context_keys = {
        field.get("context_key")
        for field in context_fields
        if isinstance(field, dict)
    }
    required = {
        "thinking_card_mode",
        "invocations",
        "tool_messages",
        "response_text",
    }
    missing = required - context_keys
    assert not missing, (
        "recovery_decision.llm_policy.context_fields missing required keys: "
        f"{sorted(missing)}"
    )

    prompt_concept_ids = recovery_decision.get("prompt_concept_ids") or []
    assert RECOVERY_PROMPT_CONCEPT_ID in prompt_concept_ids


def test_recovery_prompt_seed_teaches_thinking_card_modes() -> None:
    """The recovery-decision prompt seed must keep teaching the JVNAUTOSCI-2130
    thinking-card-mode adaptation rules (default / expert / debug).
    """

    text = RECOVERY_PROMPT_SEED_PATH.read_text(encoding="utf-8")
    assert "Thinking Card Mode" in text
    assert "`default`" in text
    assert "`expert` or `debug`" in text
    assert "`debug`" in text
    # The prompt must instruct the model to draw the partial-progress
    # summary from the context fields the workflow exposes.
    assert "Tool Invocations Observed" in text
    assert "Tool Messages Observed" in text


def test_referenced_prompt_seed_files_exist_for_canonical_bundle() -> None:
    """Each ``prompt_concept_ids`` entry in the canonical bundle should have a
    matching ``prompt_<id>_seed.md`` file in the seed-bundle directory.

    This is a soft filename convention check; missing seed files cause
    runtime authority drift because the prompt body falls back to whatever
    is already in Vontology (or nothing) instead of the repo seed.
    """

    payload = json.loads(CANONICAL_BUNDLE_PATH.read_text(encoding="utf-8"))
    referenced: set[str] = set()
    for step in _iter_workflow_steps(payload):
        for prompt_id in step.get("prompt_concept_ids") or []:
            if isinstance(prompt_id, str):
                referenced.add(prompt_id)

    bundled_inline = _bundled_prompt_concept_ids()
    missing: list[str] = []
    for prompt_id in sorted(referenced):
        if not prompt_id.startswith("#V#"):
            continue
        if prompt_id in bundled_inline:
            continue
        bare = prompt_id[len("#V#") :]
        candidate = SEED_BUNDLE_DIR / f"{bare}_seed.md"
        if not candidate.exists():
            missing.append(f"{prompt_id} -> expected {candidate.name}")

    assert missing == [], (
        "canonical bundle references prompt concepts with no seed source "
        f"(neither standalone _seed.md nor prompt seed bundle): {missing}"
    )
