"""Preview or publish the UC-03 arXiv degraded-path output contract repair."""

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
from src.backend.workflows.workflow_definition_identity_service import (  # noqa: E402
    build_workflow_definition_identity,
)
from src.backend.workflows.workflow_studio_service import (  # noqa: E402
    apply_workflow_authoring_spec,
    preview_workflow_authoring_spec,
)


WORKFLOW_ID = "#V#arxiv_paper_representation_workflow"
STATE_SUFFIX = "_record_pdf_import_skipped"
DELEGATE_STATE_SUFFIX = "_delegate_to_general_paper_workflow"
DELEGATED_WORKFLOW_ID = "#V#scholarly_article_metadata_representation_workflow"
DIAGNOSTIC_KEYS = frozenset(
    {
        "arxiv_pdf_import_error",
        "arxiv_pdf_import_message",
        "arxiv_pdf_import_status_code",
        "arxiv_pdf_import_final_url",
    }
)
DEFAULT_ACTOR_USER_ID = "#V#zhan_von_witbrock"
DEFAULT_ACTOR_ORGANISATION_ID = "#V#university_of_auckland_strong_ai_lab"


def _clean_text(value: Any) -> str:
    return str(value or "").strip() if isinstance(value, str) else ""


def rewrite_degraded_pdf_contract(
    authoring_spec: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    spec = copy.deepcopy(dict(authoring_spec))
    raw_steps = spec.get("steps")
    if not isinstance(raw_steps, list):
        raise ValueError("workflow_authoring_spec_missing_steps")
    matches = [
        step
        for step in raw_steps
        if isinstance(step, dict)
        and _clean_text(step.get("state_id")).endswith(STATE_SUFFIX)
    ]
    if len(matches) != 1:
        raise ValueError(f"workflow_state_match_count:{STATE_SUFFIX}:{len(matches)}")
    state = matches[0]
    bindings = state.get("static_input_bindings")
    if not isinstance(bindings, list):
        raise ValueError("record_pdf_import_skipped_static_inputs_missing")
    assignment_bindings = [
        binding
        for binding in bindings
        if isinstance(binding, dict)
        and _clean_text(binding.get("tool_param") or binding.get("key"))
        == "assignments"
    ]
    if len(assignment_bindings) != 1:
        raise ValueError("record_pdf_import_skipped_assignments_ambiguous")
    assignments = assignment_bindings[0].get("value")
    if not isinstance(assignments, list):
        raise ValueError("record_pdf_import_skipped_assignments_invalid")

    seen: set[str] = set()
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        key = _clean_text(assignment.get("key"))
        if key not in DIAGNOSTIC_KEYS:
            continue
        seen.add(key)
        assignment.pop("skip_if_unresolved", None)
    missing = sorted(DIAGNOSTIC_KEYS - seen)
    if missing:
        raise ValueError(
            "record_pdf_import_skipped_diagnostics_missing:" + ",".join(missing)
        )

    delegate_matches = [
        step
        for step in raw_steps
        if isinstance(step, dict)
        and _clean_text(step.get("state_id")).endswith(DELEGATE_STATE_SUFFIX)
    ]
    if len(delegate_matches) != 1:
        raise ValueError(
            f"workflow_state_match_count:{DELEGATE_STATE_SUFFIX}:"
            f"{len(delegate_matches)}"
        )
    delegate_step = delegate_matches[0]
    existing_subworkflow_id = _clean_text(delegate_step.get("subworkflow_id"))
    if existing_subworkflow_id and existing_subworkflow_id != DELEGATED_WORKFLOW_ID:
        raise ValueError("delegate_subworkflow_id_conflict")
    delegate_step["subworkflow_id"] = DELEGATED_WORKFLOW_ID
    return spec, spec != dict(authoring_spec)


def _identity(definition: Any) -> dict[str, Any]:
    return build_workflow_definition_identity(
        workflow_id=WORKFLOW_ID,
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
        before = load_workflow_definition_from_vontology(WORKFLOW_ID)
        if before is None:
            raise ValueError(f"workflow_not_found:{WORKFLOW_ID}")
        before_identity = _identity(before)
        before_spec = serialise_workflow_definition_to_authoring_spec(before)
        candidate_spec, changed = rewrite_degraded_pdf_contract(before_spec)
        base_hash = _clean_text(before_identity.get("definition_hash"))
        preview = preview_workflow_authoring_spec(
            WORKFLOW_ID,
            authoring_spec=candidate_spec,
            base_definition_hash=base_hash,
        )
        preview_body = preview.get("preview") or {}
        validation = preview_body.get("contract_validation") or {}
        result: dict[str, Any] = {
            "workflow_id": WORKFLOW_ID,
            "actor_user_id": actor_user_id,
            "actor_organisation_id": actor_organisation_id,
            "mode": "apply" if apply else "preview",
            "changed": changed,
            "base_definition_hash": base_hash,
            "candidate_definition_hash": (
                preview_body.get("definition_identity") or {}
            ).get("definition_hash"),
            "contract_valid": bool(validation.get("valid")),
            "contract_errors": validation.get("errors") or [],
            "contract_warnings": validation.get("warnings") or [],
            "subworkflow_contract_issues": validation.get(
                "subworkflow_contract_issues"
            )
            or [],
            "diff_summary": preview_body.get("diff_summary"),
        }
        if not apply:
            return result
        applied = apply_workflow_authoring_spec(
            WORKFLOW_ID,
            authoring_spec=candidate_spec,
            base_definition_hash=base_hash,
        )
        publication = applied.get("publication") or {}
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
        _readback_spec, still_changed = rewrite_degraded_pdf_contract(after_spec)
        if still_changed:
            raise ValueError("workflow_publication_readback_mismatch")
        result["publication"] = publication
        result["readback_definition_hash"] = _identity(after).get("definition_hash")
        result["canonical_readback_verified"] = True
        return result


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
