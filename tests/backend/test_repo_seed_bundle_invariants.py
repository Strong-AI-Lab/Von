"""Structural invariants for repo seed bundles.

JVNAUTOSCI-2132 L0 deliverable. These assertions are intentionally narrow:
they only check structural properties of the seed-bundle JSON and prompt
seed Markdown files in ``src/backend/workflows/repo_seed_bundles/``. They
do not bootstrap, mutate, or otherwise treat the bundles as authoritative;
Vontology remains the authority surface, and bundles only seed missing
state at runtime.

The point is to fail fast on edits that break representation mechanics such as
a missing ``seed_version``, dangling ``to_state`` references, or a referenced
prompt with no seed source. It deliberately does not prescribe a universal
conversation-turn workflow, stage sequence, selector, critic, or completion
gate.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SEED_BUNDLE_DIR = PROJECT_ROOT / "src/backend/workflows/repo_seed_bundles"
WORKFLOW_BUNDLE_SCHEMA = "repo_seed_workflow_bundle.v1"

CANONICAL_BUNDLE_PATH = (
    SEED_BUNDLE_DIR / "canonical_workflow_publication_seed_bundle.json"
)


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
        candidate_names = [f"{bare}_seed.md", f"{bare}_seed.json"]
        if bare.startswith("prompt_"):
            alternate = f"{bare[len('prompt_') :]}_prompt_seed"
            candidate_names.extend([f"{alternate}.md", f"{alternate}.json"])
        if not any((SEED_BUNDLE_DIR / name).exists() for name in candidate_names):
            missing.append(
                f"{prompt_id} -> expected one of {', '.join(candidate_names)}"
            )

    assert missing == [], (
        "canonical bundle references prompt concepts with no seed source "
        f"(neither standalone _seed.md nor prompt seed bundle): {missing}"
    )


def test_legacy_general_mail_review_is_not_an_ordinary_routing_candidate() -> None:
    payload = json.loads(CANONICAL_BUNDLE_PATH.read_text(encoding="utf-8"))
    workflow = next(
        item
        for item in payload.get("workflows", [])
        if item.get("workflow_id") == "#V#general_mail_review_workflow"
    )
    routing_relation = next(
        item
        for item in workflow.get("text_relations", [])
        if item.get("predicate") == "#V#hasWorkflowRoutingProfileJson"
    )
    routing_profile = json.loads(routing_relation["text"])

    assert routing_profile["routing_eligible"] is False
    assert routing_profile["ordinary_mail_review"] is False
    assert routing_profile["prefer_existing_capability"] is False
    assert routing_profile["retained_for_explicit_durable_review_only"] is True


def test_repo_seeded_gmail_list_steps_use_semantic_mailbox_scope() -> None:
    canonical = json.loads(CANONICAL_BUNDLE_PATH.read_text(encoding="utf-8"))
    mail_review = next(
        item
        for item in canonical.get("workflows", [])
        if item.get("workflow_id") == "#V#general_mail_review_workflow"
    )
    mail_review_steps = {
        step["state_id"]: step
        for step in mail_review["publication_spec"]["steps"]
        if isinstance(step, dict) and isinstance(step.get("state_id"), str)
    }
    list_bindings = dict(
        mail_review_steps["list_recent_mail_messages"]["static_input_bindings"]
    )
    assert list_bindings["scope"] == "whole_mailbox"
    assert "bypass_profile_query_prefix" not in list_bindings

    prompt_bindings = dict(
        mail_review_steps["prepare_mail_review_tool_prompt"][
            "static_input_bindings"
        ]
    )
    prompt_template = prompt_bindings["assignments"][0]["template"]
    assert "scope whole_mailbox" in prompt_template
    assert "bypass_profile_query_prefix true" not in prompt_template

    convergence_path = (
        SEED_BUNDLE_DIR
        / "email_source_representation_convergence_workflow_seed_bundle.json"
    )
    convergence = json.loads(convergence_path.read_text(encoding="utf-8"))
    zhan_workflow = next(
        item
        for item in convergence.get("workflows", [])
        if item.get("workflow_id") == "#V#zhan_gmail_arxiv_ingestion_workflow"
    )
    list_step = next(
        step
        for step in zhan_workflow["publication_spec"]["steps"]
        if step.get("state_id") == "list_messages"
    )
    tool_arguments = dict(list_step["static_input_bindings"])["tool_arguments"]
    assert tool_arguments["scope"] == "whole_mailbox"
    assert "bypass_profile_query_prefix" not in tool_arguments


def test_explicit_workflow_experience_prelude_callers_keep_their_seed_definition() -> (
    None
):
    """Retain the shared prelude while repo-seeded explicit workflows invoke it."""

    prelude_id = "#V#workflow_experience_context_prelude"
    canonical = json.loads(CANONICAL_BUNDLE_PATH.read_text(encoding="utf-8"))
    canonical_workflow_ids = {
        workflow.get("workflow_id")
        for workflow in canonical.get("workflows", [])
        if isinstance(workflow, dict)
    }

    caller_paths = (
        SEED_BUNDLE_DIR
        / "operational_certification_evaluator_workflow_seed_bundle.json",
        SEED_BUNDLE_DIR
        / "operational_learning_candidate_behaviour_workflow_seed_bundle.json",
        SEED_BUNDLE_DIR
        / "operational_learning_release_authority_workflow_seed_bundle.json",
    )
    callers: list[str] = []
    for path in caller_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for workflow in payload.get("workflows", []):
            if not isinstance(workflow, dict):
                continue
            for step in (
                (workflow.get("publication_spec") or {}).get("steps", [])
            ):
                if (
                    isinstance(step, dict)
                    and step.get("invoked_workflow_id") == prelude_id
                ):
                    callers.append(
                        f"{workflow.get('workflow_id')}:{step.get('state_id')}"
                    )

    assert callers
    assert prelude_id in canonical_workflow_ids
