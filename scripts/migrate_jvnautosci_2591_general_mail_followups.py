"""Publish the represented UC-04 profile-status and explicit-body paths."""

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
from src.backend.workflows.vontology_loader import (  # noqa: E402
    load_workflow_definition_from_vontology,
)
from src.backend.workflows.workflow_authoring_service import (  # noqa: E402
    serialise_workflow_definition_to_authoring_spec,
)
from src.backend.workflows.workflow_concept_authority_service import (  # noqa: E402
    build_seed_canonical_workflow_definitions,
)
from src.backend.workflows.workflow_definition_identity_service import (  # noqa: E402
    build_workflow_definition_identity,
)
from src.backend.workflows.workflow_studio_service import (  # noqa: E402
    apply_workflow_authoring_spec,
    preview_workflow_authoring_spec,
)


GENERAL_MAIL_WORKFLOW_ID = "#V#general_mail_review_workflow"
DETAIL_WORKFLOW_ID = "#V#gmail_message_detail_fetch_workflow"
WORKFLOW_IDS = (GENERAL_MAIL_WORKFLOW_ID, DETAIL_WORKFLOW_ID)
DEFAULT_ACTOR_USER_ID = "#V#zhan_von_witbrock"
DEFAULT_ACTOR_ORGANISATION_ID = "#V#university_of_auckland_strong_ai_lab"


def _step_with_suffix(steps: list[dict[str, Any]], suffix: str) -> dict[str, Any]:
    matches = [
        step for step in steps if str(step.get("state_id") or "").endswith(suffix)
    ]
    if len(matches) != 1:
        raise ValueError(f"workflow_state_match_count:{suffix}:{len(matches)}")
    return matches[0]


def _state_id_by_suffix(
    steps: list[dict[str, Any]], suffix: str
) -> str | None:
    matches = [
        str(step.get("state_id") or "").strip()
        for step in steps
        if str(step.get("state_id") or "").endswith(suffix)
    ]
    return matches[0] if len(matches) == 1 else None


def _remap_step_targets(
    step: dict[str, Any],
    *,
    candidate_steps: list[dict[str, Any]],
) -> None:
    for transition in step.get("conditional_transitions") or []:
        if not isinstance(transition, dict):
            continue
        target = str(transition.get("to_state") or "").strip()
        replacement = _state_id_by_suffix(candidate_steps, target)
        if replacement:
            transition["to_state"] = replacement
    for key in (
        "on_failure_state_key",
        "on_approval_required_state_key",
        "on_break_state_key",
        "on_continue_state_key",
        "on_false_state_key",
        "on_true_state_key",
        "on_unknown_state_key",
    ):
        target = str(step.get(key) or "").strip()
        replacement = _state_id_by_suffix(candidate_steps, target)
        if replacement:
            step[key] = replacement


def _copy_fields(
    target: dict[str, Any],
    source: Mapping[str, Any],
    fields: tuple[str, ...],
) -> None:
    for field in fields:
        if field in source:
            target[field] = copy.deepcopy(source[field])
        else:
            target.pop(field, None)


def _copy_llm_policy_preserving_runtime_defaults(
    target: dict[str, Any], source: Mapping[str, Any]
) -> None:
    current_policy = target.get("llm_policy")
    current_selection_policy = (
        current_policy.get("selection_policy")
        if isinstance(current_policy, Mapping)
        else None
    )
    _copy_fields(target, source, ("llm_policy",))
    candidate_policy = target.get("llm_policy")
    if (
        current_selection_policy is not None
        and isinstance(candidate_policy, dict)
        and "selection_policy" not in candidate_policy
    ):
        candidate_policy["selection_policy"] = current_selection_policy


def rewrite_general_mail_followups(
    current_spec: Mapping[str, Any],
    desired_spec: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    candidate = copy.deepcopy(dict(current_spec))
    raw_steps = candidate.get("steps")
    desired_raw_steps = desired_spec.get("steps")
    if not isinstance(raw_steps, list) or not isinstance(desired_raw_steps, list):
        raise ValueError("workflow_authoring_spec_missing_steps")
    steps = [step for step in raw_steps if isinstance(step, dict)]
    desired_steps = [step for step in desired_raw_steps if isinstance(step, dict)]

    for suffix, fields in {
        "prepare_mail_review_tool_prompt": ("static_input_bindings",),
        "record_mail_list_invocation": ("static_input_bindings",),
        "render_mail_review_response": (),
    }.items():
        _copy_fields(
            _step_with_suffix(steps, suffix),
            _step_with_suffix(desired_steps, suffix),
            fields,
        )
    _copy_llm_policy_preserving_runtime_defaults(
        _step_with_suffix(steps, "render_mail_review_response"),
        _step_with_suffix(desired_steps, "render_mail_review_response"),
    )

    extractor = _step_with_suffix(steps, "extract_mail_review_request_parameters")
    desired_extractor = _step_with_suffix(
        desired_steps, "extract_mail_review_request_parameters"
    )
    _copy_llm_policy_preserving_runtime_defaults(extractor, desired_extractor)
    _copy_fields(
        extractor,
        desired_extractor,
        ("validation_policy", "tool_output_context_mappings", "writes_context_keys"),
    )

    for suffix in ("fetch_mail_profile_status", "render_mail_profile_status"):
        if _state_id_by_suffix(steps, suffix) is None:
            steps.append(copy.deepcopy(_step_with_suffix(desired_steps, suffix)))

    _copy_fields(
        _step_with_suffix(steps, "fetch_mail_profile_status"),
        _step_with_suffix(desired_steps, "fetch_mail_profile_status"),
        ("tool_output_context_mappings", "writes_context_keys"),
    )

    _copy_fields(
        extractor,
        desired_extractor,
        ("conditional_transitions",),
    )
    for suffix in (
        "extract_mail_review_request_parameters",
        "fetch_mail_profile_status",
        "render_mail_profile_status",
    ):
        _remap_step_targets(_step_with_suffix(steps, suffix), candidate_steps=steps)

    candidate["steps"] = steps
    return candidate, candidate != dict(current_spec)


def rewrite_detail_body_path(
    current_spec: Mapping[str, Any],
    desired_spec: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    candidate = copy.deepcopy(dict(current_spec))
    raw_steps = candidate.get("steps")
    desired_raw_steps = desired_spec.get("steps")
    if not isinstance(raw_steps, list) or not isinstance(desired_raw_steps, list):
        raise ValueError("workflow_authoring_spec_missing_steps")
    steps = [step for step in raw_steps if isinstance(step, dict)]
    desired_steps = [step for step in desired_raw_steps if isinstance(step, dict)]

    fetch = _step_with_suffix(steps, "fetch_message_detail")
    desired_fetch = _step_with_suffix(desired_steps, "fetch_message_detail")
    current_mappings = {
        (mapping.get("context_key"), mapping.get("tool_param")): mapping
        for mapping in fetch.get("context_input_mappings") or []
        if isinstance(mapping, dict)
    }
    merged_mappings: list[dict[str, Any]] = []
    for mapping in desired_fetch.get("context_input_mappings") or []:
        if not isinstance(mapping, Mapping):
            continue
        key = (mapping.get("context_key"), mapping.get("tool_param"))
        existing = current_mappings.get(key)
        if existing is not None:
            merged_mappings.append(copy.deepcopy(existing))
            continue
        created = copy.deepcopy(dict(mapping))
        context_key = str(created.get("context_key") or "").strip()
        tool_param = str(created.get("tool_param") or "").strip()
        created["mapping_concept_id"] = (
            "#V#workflow_mapping_gmail_message_detail_fetch_workflow_"
            f"fetch_message_detail_{context_key}_to_{tool_param}_parameter"
        )
        created["required"] = True
        merged_mappings.append(created)
    fetch["context_input_mappings"] = merged_mappings
    _copy_fields(
        fetch,
        desired_fetch,
        ("static_input_bindings", "tool_output_context_mappings", "writes_context_keys"),
    )
    project = _step_with_suffix(steps, "project_message_detail_output")
    _copy_fields(
        project,
        _step_with_suffix(desired_steps, "project_message_detail_output"),
        ("static_input_bindings",),
    )
    candidate["steps"] = steps
    return candidate, candidate != dict(current_spec)


def _identity(workflow_id: str, definition: Any) -> dict[str, Any]:
    return build_workflow_definition_identity(
        workflow_id=workflow_id,
        source="vontology",
        definition=definition,
        authoritative_definition=definition,
    )


def run_migration(
    *, apply: bool, actor_user_id: str, actor_organisation_id: str
) -> dict[str, Any]:
    with override_current_actor(
        user_concept_id=actor_user_id,
        organisation_concept_id=actor_organisation_id,
    ):
        desired_definitions = build_seed_canonical_workflow_definitions(
            target_workflow_ids=WORKFLOW_IDS
        )
        reports: dict[str, Any] = {}
        for workflow_id, rewrite in (
            (GENERAL_MAIL_WORKFLOW_ID, rewrite_general_mail_followups),
            (DETAIL_WORKFLOW_ID, rewrite_detail_body_path),
        ):
            current_definition = load_workflow_definition_from_vontology(workflow_id)
            if current_definition is None:
                raise ValueError(f"workflow_not_found:{workflow_id}")
            current_spec = serialise_workflow_definition_to_authoring_spec(
                current_definition
            )
            desired_spec = serialise_workflow_definition_to_authoring_spec(
                desired_definitions[workflow_id]
            )
            candidate_spec, changed = rewrite(current_spec, desired_spec)
            base_hash = str(
                _identity(workflow_id, current_definition).get("definition_hash") or ""
            )
            preview = preview_workflow_authoring_spec(
                workflow_id,
                authoring_spec=candidate_spec,
                base_definition_hash=base_hash,
            )
            preview_body = preview.get("preview") or {}
            validation = preview_body.get("contract_validation") or {}
            workflow_report: dict[str, Any] = {
                "changed": changed,
                "base_definition_hash": base_hash,
                "contract_valid": bool(validation.get("valid")),
                "contract_errors": validation.get("errors") or [],
                "diff_summary": preview_body.get("diff_summary"),
            }
            if apply and changed:
                if not workflow_report["contract_valid"]:
                    raise ValueError(f"workflow_preview_invalid:{workflow_id}")
                apply_result = apply_workflow_authoring_spec(
                    workflow_id,
                    authoring_spec=candidate_spec,
                    base_definition_hash=base_hash,
                )
                publication = apply_result.get("publication") or {}
                counts = publication.get("counts") or {}
                if counts.get("errors") or workflow_id not in (
                    publication.get("published_workflow_ids") or []
                ):
                    raise ValueError(
                        "workflow_publication_failed:"
                        + json.dumps(publication, sort_keys=True, default=str)
                    )
                after = load_workflow_definition_from_vontology(workflow_id)
                if after is None:
                    raise ValueError(f"workflow_missing_after_publication:{workflow_id}")
                after_spec = serialise_workflow_definition_to_authoring_spec(after)
                _candidate, still_changed = rewrite(after_spec, desired_spec)
                if still_changed:
                    raise ValueError(f"workflow_publication_readback_mismatch:{workflow_id}")
                workflow_report["canonical_readback_verified"] = True
                workflow_report["readback_definition_hash"] = _identity(
                    workflow_id, after
                ).get("definition_hash")
            reports[workflow_id] = workflow_report
        return {
            "mode": "apply" if apply else "preview",
            "actor_user_id": actor_user_id,
            "actor_organisation_id": actor_organisation_id,
            "workflows": reports,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor-user-id", default=DEFAULT_ACTOR_USER_ID)
    parser.add_argument(
        "--actor-organisation-id", default=DEFAULT_ACTOR_ORGANISATION_ID
    )
    args = parser.parse_args()
    print(
        json.dumps(
            run_migration(
                apply=bool(args.apply),
                actor_user_id=str(args.actor_user_id).strip(),
                actor_organisation_id=str(args.actor_organisation_id).strip(),
            ),
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
