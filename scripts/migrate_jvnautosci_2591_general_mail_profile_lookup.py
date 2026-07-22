"""Preview or apply the UC-04 canonical mail-profile lookup repair.

The represented workflow only needs binary profile-authority relations.  Using
``relation_kind=any`` also scans actor-scoped text relations, which can exceed
the workflow MCP deadline before the requested predicate filter is applied.
This migration publishes the narrow represented-input correction through
Workflow Studio and verifies canonical read-back.
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
from src.backend.workflows.definitions import (  # noqa: E402
    GENERAL_MAIL_REVIEW_WORKFLOW_ID,
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


WORKFLOW_ID = GENERAL_MAIL_REVIEW_WORKFLOW_ID
LOOKUP_STATE_SUFFIXES = (
    "lookup_default_mail_profile",
    "lookup_represented_mail_profiles",
)
DEFAULT_ACTOR_USER_ID = "#V#zhan_von_witbrock"
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


def _static_binding(step: dict[str, Any], tool_param: str) -> dict[str, Any]:
    bindings = step.get("static_input_bindings")
    if not isinstance(bindings, list):
        raise ValueError(
            f"workflow_static_bindings_missing:{step.get('state_id')}"
        )
    matches = [
        binding
        for binding in bindings
        if isinstance(binding, dict)
        and _clean_text(binding.get("tool_param")) == tool_param
    ]
    if len(matches) != 1:
        raise ValueError(
            "workflow_static_binding_match_count:"
            f"{step.get('state_id')}:{tool_param}:{len(matches)}"
        )
    return matches[0]


def rewrite_general_mail_profile_lookups(
    authoring_spec: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Restrict the two authoritative profile lookups to binary relations."""

    spec = copy.deepcopy(dict(authoring_spec))
    raw_steps = spec.get("steps")
    if not isinstance(raw_steps, list):
        raise ValueError("workflow_authoring_spec_missing_steps")
    steps = [step for step in raw_steps if isinstance(step, dict)]
    if len(steps) != len(raw_steps):
        raise ValueError("workflow_authoring_spec_contains_invalid_step")

    for suffix in LOOKUP_STATE_SUFFIXES:
        step = _step_with_suffix(steps, suffix)
        if _clean_text(step.get("action_id")) != "workflow_mcp.invoke_tool":
            raise ValueError(f"workflow_lookup_action_mismatch:{suffix}")
        tool_name = _static_binding(step, "tool_name").get("value")
        if tool_name != "find_relations_with_argument":
            raise ValueError(f"workflow_lookup_tool_mismatch:{suffix}")
        arguments_binding = _static_binding(step, "tool_arguments")
        arguments = arguments_binding.get("value")
        if not isinstance(arguments, dict):
            raise ValueError(f"workflow_lookup_arguments_invalid:{suffix}")
        arguments["relation_kind"] = "binary"

    # The resolver currently needs no tools, but keep its optional recovery
    # default on the same bounded relation surface.
    resolver = _step_with_suffix(steps, "resolve_mail_profile")
    llm_policy = resolver.get("llm_policy")
    if isinstance(llm_policy, dict):
        defaults = llm_policy.get("tool_argument_defaults")
        if isinstance(defaults, dict):
            relation_defaults = defaults.get("find_relations_with_argument")
            if isinstance(relation_defaults, dict):
                relation_defaults["relation_kind"] = "binary"

    spec["steps"] = steps
    return spec, spec != dict(authoring_spec)


def _definition_identity(definition: Any) -> dict[str, Any]:
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
        before_identity = _definition_identity(before)
        before_spec = serialise_workflow_definition_to_authoring_spec(before)
        candidate_spec, changed = rewrite_general_mail_profile_lookups(before_spec)
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
        }
        if not apply:
            return result

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
                "workflow_publication_failed:"
                + json.dumps(publication, sort_keys=True, default=str)
            )
        after = load_workflow_definition_from_vontology(WORKFLOW_ID)
        if after is None:
            raise ValueError("workflow_missing_after_publication")
        after_spec = serialise_workflow_definition_to_authoring_spec(after)
        _unchanged_spec, still_changed = rewrite_general_mail_profile_lookups(
            after_spec
        )
        if still_changed:
            raise ValueError("workflow_publication_readback_mismatch")
        result["publication"] = publication
        result["readback_definition_hash"] = _definition_identity(after).get(
            "definition_hash"
        )
        result["canonical_readback_verified"] = True
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
