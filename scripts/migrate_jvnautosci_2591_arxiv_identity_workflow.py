"""Preview or apply the UC-03 canonical arXiv identity workflow migration.

The runtime primitive that creates an arXiv paper concept is deliberately kept
in support code.  This migration places the durable routing decision in the
authoritative Vontology workflow and publishes through Workflow Studio's
optimistic-concurrency and validation path.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from dotenv import load_dotenv


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
load_dotenv(_PROJECT_ROOT / ".env", override=False)

from src.backend.security.access_control import override_current_actor  # noqa: E402
from src.backend.security.visibility_predicates import (  # noqa: E402
    get_specific_to_org_values,
    get_specific_to_user_values,
    set_specific_to_org_values,
    set_specific_to_user_values,
)
from src.backend.services import concept_service  # noqa: E402
from src.backend.services.concept_service import (  # noqa: E402
    _find_raw_concept_by_exact_concept_id,
)
from src.backend.workflows.vontology_loader import (  # noqa: E402
    load_workflow_definition_from_vontology,
)
from src.backend.workflows.workflow_authoring_service import (  # noqa: E402
    serialise_workflow_definition_to_authoring_spec,
)
from src.backend.workflows.workflow_definition_identity_service import (  # noqa: E402
    build_workflow_definition_identity,
)
from src.backend.workflows.workflow_studio_service import (  # noqa: E402
    apply_workflow_authoring_spec,
    preview_workflow_authoring_spec,
)
from src.backend.workflows.workflow_concept_authority_service import (  # noqa: E402
    workflow_child_visibility_covers_parent,
)


WORKFLOW_ID = "#V#scholarly_article_metadata_representation_workflow"
NORMALISE_STATE_SUFFIX = "_normalise_metadata_context"
ATTACH_STATE_SUFFIX = "_attach_metadata"
ENSURE_STATE_SUFFIX = "_ensure_arxiv_paper_concept"
ENSURE_ACTION_ID = "scholarly_paper.ensure_paper_concept"
DEFAULT_ACTOR_USER_ID = "#V#zhan_von_witbrock"
DEFAULT_VERIFICATION_ACTOR_USER_ID = "#V#michael_witbrock"
DEFAULT_ACTOR_ORGANISATION_ID = "#V#university_of_auckland_strong_ai_lab"


def _clean_text(value: Any) -> str:
    return str(value or "").strip() if isinstance(value, str) else ""


def _step_with_suffix(
    steps: list[dict[str, Any]], suffix: str
) -> dict[str, Any]:
    matches = [
        step
        for step in steps
        if _clean_text(step.get("state_id")).endswith(suffix)
    ]
    if len(matches) != 1:
        raise ValueError(f"workflow_state_match_count:{suffix}:{len(matches)}")
    return matches[0]


def rewrite_metadata_workflow_for_canonical_arxiv_identity(
    authoring_spec: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Return an idempotently rewritten authoring spec and whether it changed."""

    spec = copy.deepcopy(dict(authoring_spec))
    raw_steps = spec.get("steps")
    if not isinstance(raw_steps, list):
        raise ValueError("workflow_authoring_spec_missing_steps")
    steps = [step for step in raw_steps if isinstance(step, dict)]
    if len(steps) != len(raw_steps):
        raise ValueError("workflow_authoring_spec_contains_invalid_step")

    normalise_step = _step_with_suffix(steps, NORMALISE_STATE_SUFFIX)
    attach_step = _step_with_suffix(steps, ATTACH_STATE_SUFFIX)
    attach_state_id = _clean_text(attach_step.get("state_id"))

    ensure_matches = [
        step
        for step in steps
        if _clean_text(step.get("state_id")).endswith(ENSURE_STATE_SUFFIX)
    ]
    if len(ensure_matches) > 1:
        raise ValueError("workflow_ensure_arxiv_state_ambiguous")
    if ensure_matches:
        ensure_state_id = _clean_text(ensure_matches[0].get("state_id"))
        expected_action = _clean_text(ensure_matches[0].get("action_id"))
        if expected_action != ENSURE_ACTION_ID:
            raise ValueError("workflow_ensure_arxiv_state_action_conflict")
    else:
        normalise_state_id = _clean_text(normalise_step.get("state_id"))
        ensure_state_id = normalise_state_id[: -len(NORMALISE_STATE_SUFFIX)] + (
            ENSURE_STATE_SUFFIX
        )
        ensure_step_concept_id = ensure_state_id
        workflow_slug = WORKFLOW_ID.removeprefix("#V#")
        mapping_prefix = (
            "#V#workflow_mapping_tool_field_"
            f"{workflow_slug}_ensure_arxiv_paper_concept"
        )
        ensure_step = {
            "state_id": ensure_state_id,
            "terminal": False,
            "action_id": ENSURE_ACTION_ID,
            "execution_mode": "deterministic",
            "concept_id": ensure_step_concept_id,
            "writes_context_keys": [
                "paper_concept_id",
                "paper_concept_created",
            ],
            "tool_output_context_mappings": [
                {
                    "mapping_concept_id": (
                        f"{mapping_prefix}_paper_concept_id_to_paper_concept_id"
                    ),
                    "tool_output_field": "paper_concept_id",
                    "context_key": "paper_concept_id",
                },
                {
                    "mapping_concept_id": (
                        f"{mapping_prefix}_paper_concept_created_to_"
                        "paper_concept_created"
                    ),
                    "tool_output_field": "paper_concept_created",
                    "context_key": "paper_concept_created",
                },
            ],
            "mutation_authority": {
                "schema_version": "workflow_step_mutation_authority.v1",
                "maximum_level": "additive_vontology",
                "reason_code": "canonical_arxiv_paper_identity_additive_writes",
            },
            "next_state_key": attach_state_id,
            "on_failure_state_key": next(
                (
                    _clean_text(step.get("state_id"))
                    for step in steps
                    if _clean_text(step.get("state_id")).endswith("_failed")
                ),
                "",
            ),
        }
        if not ensure_step["on_failure_state_key"]:
            raise ValueError("workflow_failed_state_missing")
        normalise_index = steps.index(normalise_step)
        steps.insert(normalise_index + 1, ensure_step)

    transitions = normalise_step.setdefault("conditional_transitions", [])
    if not isinstance(transitions, list):
        raise ValueError("workflow_normalise_transitions_invalid")
    matching = [
        transition
        for transition in transitions
        if isinstance(transition, Mapping)
        and _clean_text(transition.get("reason"))
        == "canonical_arxiv_identity_available"
    ]
    if len(matching) > 1:
        raise ValueError("workflow_arxiv_transition_ambiguous")
    if matching:
        if _clean_text(matching[0].get("to_state")) != ensure_state_id:
            raise ValueError("workflow_arxiv_transition_target_conflict")
    else:
        transitions.append(
            {
                "to_state": ensure_state_id,
                "reason": "canonical_arxiv_identity_available",
                "condition_spec": {
                    "kind": "all",
                    "conditions": [
                        {
                            "kind": "context_exists",
                            "key": "arxiv_id",
                            "expected": True,
                        },
                        {
                            "kind": "context_is_null",
                            "key": "arxiv_id",
                            "expected": False,
                        },
                        {
                            "kind": "not",
                            "condition": {
                                "kind": "context_value_equals",
                                "key": "arxiv_id",
                                "value": "",
                            },
                        },
                    ],
                },
            }
        )

    spec["steps"] = steps
    return spec, spec != dict(authoring_spec)


def _definition_identity(definition: Any) -> dict[str, Any]:
    return build_workflow_definition_identity(
        workflow_id=WORKFLOW_ID,
        source="vontology",
        definition=definition,
        authoritative_definition=definition,
    )


def _ensure_visibility_child_ids(authoring_spec: Mapping[str, Any]) -> tuple[str, ...]:
    raw_steps = authoring_spec.get("steps")
    steps = (
        [dict(step) for step in raw_steps if isinstance(step, Mapping)]
        if isinstance(raw_steps, list)
        else []
    )
    ensure_step = _step_with_suffix(steps, ENSURE_STATE_SUFFIX)
    child_ids = [
        _clean_text(ensure_step.get("concept_id"))
        or _clean_text(ensure_step.get("state_id"))
    ]
    raw_mappings = ensure_step.get("tool_output_context_mappings")
    if isinstance(raw_mappings, list):
        child_ids.extend(
            _clean_text(mapping.get("mapping_concept_id"))
            for mapping in raw_mappings
            if isinstance(mapping, Mapping)
        )
    return tuple(dict.fromkeys(child_id for child_id in child_ids if child_id))


def repair_ensure_arxiv_visibility_closure(
    authoring_spec: Mapping[str, Any],
    *,
    apply: bool,
) -> dict[str, Any]:
    """Align the incident's three child artefacts with the root audience."""

    workflow_doc = _find_raw_concept_by_exact_concept_id(WORKFLOW_ID)
    if not isinstance(workflow_doc, Mapping):
        raise ValueError(f"workflow_not_found:{WORKFLOW_ID}")
    workflow_relationships = (
        dict(workflow_doc.get("relationships") or {})
        if isinstance(workflow_doc.get("relationships"), Mapping)
        else {}
    )
    parent_users = get_specific_to_user_values(workflow_relationships)
    parent_orgs = get_specific_to_org_values(workflow_relationships)
    repaired_ids: list[str] = []
    already_covered_ids: list[str] = []
    for child_id in _ensure_visibility_child_ids(authoring_spec):
        child_doc = _find_raw_concept_by_exact_concept_id(child_id)
        if not isinstance(child_doc, Mapping):
            raise ValueError(f"workflow_child_not_found:{child_id}")
        if workflow_child_visibility_covers_parent(workflow_doc, child_doc):
            already_covered_ids.append(child_id)
            continue
        if apply:
            child_relationships = (
                dict(child_doc.get("relationships") or {})
                if isinstance(child_doc.get("relationships"), Mapping)
                else {}
            )
            updated_relationships = set_specific_to_user_values(
                child_relationships,
                parent_users,
            )
            updated_relationships = set_specific_to_org_values(
                updated_relationships,
                parent_orgs,
            )
            concept_service.update_concept(
                child_id,
                {"relationships": updated_relationships},
                defer_side_effects=True,
            )
            readback = _find_raw_concept_by_exact_concept_id(child_id)
            if not workflow_child_visibility_covers_parent(workflow_doc, readback):
                raise ValueError(
                    f"workflow_child_visibility_repair_readback_failed:{child_id}"
                )
        repaired_ids.append(child_id)
    return {
        "schema_version": "workflow_visibility_closure_repair.v1",
        "apply": apply,
        "parent_user_scope": parent_users,
        "parent_organisation_scope": parent_orgs,
        "repair_required_ids": repaired_ids,
        "already_covered_ids": already_covered_ids,
        "closure_verified": bool(
            apply or not repaired_ids
        ),
    }


def run_migration(
    *,
    apply: bool,
    actor_user_id: str,
    actor_organisation_id: str,
    verification_actor_user_id: str = DEFAULT_VERIFICATION_ACTOR_USER_ID,
) -> dict[str, Any]:
    with override_current_actor(
        user_concept_id=actor_user_id,
        organisation_concept_id=actor_organisation_id,
    ):
        before = load_workflow_definition_from_vontology(WORKFLOW_ID)
        if before is None:
            raise ValueError(f"workflow_not_found:{WORKFLOW_ID}")
        before_identity = _definition_identity(before)
        before_spec = serialise_workflow_definition_to_authoring_spec(before)
        candidate_spec, changed = (
            rewrite_metadata_workflow_for_canonical_arxiv_identity(before_spec)
        )
        visibility_repair = repair_ensure_arxiv_visibility_closure(
            candidate_spec,
            apply=False,
        )
        base_hash = _clean_text(before_identity.get("definition_hash"))
        preview = preview_workflow_authoring_spec(
            WORKFLOW_ID,
            authoring_spec=candidate_spec,
            base_definition_hash=base_hash,
        )
        validation = (
            (preview.get("preview") or {}).get("contract_validation") or {}
        )
        result: dict[str, Any] = {
            "workflow_id": WORKFLOW_ID,
            "actor_user_id": actor_user_id,
            "actor_organisation_id": actor_organisation_id,
            "mode": "apply" if apply else "preview",
            "changed": changed,
            "base_definition_hash": base_hash,
            "candidate_definition_hash": (
                (preview.get("preview") or {}).get("definition_identity") or {}
            ).get("definition_hash"),
            "contract_valid": bool(validation.get("valid")),
            "contract_errors": validation.get("errors") or [],
            "contract_warnings": validation.get("warnings") or [],
            "diff_summary": (preview.get("preview") or {}).get("diff_summary"),
            "visibility_repair": visibility_repair,
        }
        if not apply:
            return result
        if not bool(validation.get("valid")):
            raise ValueError("workflow_authoring_preview_invalid")
        result["visibility_repair"] = repair_ensure_arxiv_visibility_closure(
            candidate_spec,
            apply=True,
        )

        apply_result = apply_workflow_authoring_spec(
            WORKFLOW_ID,
            authoring_spec=candidate_spec,
            base_definition_hash=base_hash,
        )
        publication = apply_result.get("publication") or {}
        counts = publication.get("counts") or {}
        published_workflow_ids = publication.get("published_workflow_ids") or []
        if counts.get("errors") or WORKFLOW_ID not in published_workflow_ids:
            raise ValueError(
                "workflow_publication_failed:" + json.dumps(
                    publication,
                    sort_keys=True,
                    default=str,
                )
            )
        after = load_workflow_definition_from_vontology(WORKFLOW_ID)
        if after is None:
            raise ValueError("workflow_missing_after_publication")
        after_spec = serialise_workflow_definition_to_authoring_spec(after)
        _unchanged_spec, still_changed = (
            rewrite_metadata_workflow_for_canonical_arxiv_identity(after_spec)
        )
        if still_changed:
            raise ValueError("workflow_publication_readback_mismatch")
        result["publication"] = publication
        result["readback_definition_hash"] = _definition_identity(after).get(
            "definition_hash"
        )
        result["canonical_readback_verified"] = True
        with override_current_actor(
            user_concept_id=verification_actor_user_id,
            organisation_concept_id=actor_organisation_id,
        ):
            verification_actor_definition = (
                load_workflow_definition_from_vontology(WORKFLOW_ID)
            )
        if verification_actor_definition is None:
            raise ValueError("workflow_missing_for_verification_actor")
        if set(verification_actor_definition.states) != set(after.states):
            raise ValueError(
                "workflow_cross_actor_graph_closure_readback_mismatch"
            )
        result["cross_actor_graph_closure_verified"] = True
        result["verification_actor_user_id"] = verification_actor_user_id
        result["verified_state_count"] = len(after.states)
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Publish after preview validation. Without this flag, read-only.",
    )
    parser.add_argument("--actor-user-id", default=DEFAULT_ACTOR_USER_ID)
    parser.add_argument(
        "--actor-organisation-id",
        default=DEFAULT_ACTOR_ORGANISATION_ID,
    )
    parser.add_argument(
        "--verification-actor-user-id",
        default=DEFAULT_VERIFICATION_ACTOR_USER_ID,
    )
    args = parser.parse_args()
    print(
        json.dumps(
            run_migration(
                apply=bool(args.apply),
                actor_user_id=str(args.actor_user_id).strip(),
                actor_organisation_id=str(args.actor_organisation_id).strip(),
                verification_actor_user_id=str(
                    args.verification_actor_user_id
                ).strip(),
            ),
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
