"""Preview or apply the UC-05 failure-case workflow routing repair.

The failure-case prompt-improvement workflow is appropriate only when a turn
explicitly refers to a prior failure.  This migration adds represented query
cues and routing notes so an ordinary Jira/mail/repository lookup cannot enter
that workflow merely because it is semantically similar to an old failure.
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
from src.backend.services.text_value_service import (  # noqa: E402
    get_texts_for_concept,
    upsert_singleton_text_relation,
)


WORKFLOW_ID = "#V#failure_case_prompt_improvement_workflow"
DISCOVERY_PREDICATE = "#V#hasWorkflowDiscoveryExemplarsJson"
DEFAULT_ACTOR_USER_ID = "#V#zhan_von_witbrock"
DEFAULT_ACTOR_ORGANISATION_ID = "#V#university_of_auckland_strong_ai_lab"
REQUIRED_QUERY_CUES = (
    "failure case",
    "failed turn",
    "failed answer",
    "prompt improvement",
    "model failure",
    "learn from the failure",
)
ROUTING_NOTES = (
    "Choose this workflow only when the user explicitly asks to diagnose, "
    "learn from, replay, or improve a prior failed assistant turn or model result.",
    "Do not choose this workflow for an ordinary Jira, mail, repository, "
    "knowledge-base, or concept lookup merely because a prior execution record exists.",
)


def _append_unique_text(values: Any, additions: tuple[str, ...]) -> list[str]:
    result = [
        str(item).strip()
        for item in values
        if isinstance(item, str) and str(item).strip()
    ] if isinstance(values, list) else []
    seen = set(result)
    for item in additions:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def rewrite_failure_case_discovery_exemplars(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    rewritten = copy.deepcopy(dict(payload))
    rewritten.setdefault("schema_version", "workflow_discovery_exemplars.v1")
    rewritten["required_query_cues"] = _append_unique_text(
        rewritten.get("required_query_cues"), REQUIRED_QUERY_CUES
    )
    rewritten["routing_notes"] = _append_unique_text(
        rewritten.get("routing_notes"), ROUTING_NOTES
    )
    return rewritten, rewritten != dict(payload)


def _read_discovery_exemplars() -> dict[str, Any]:
    rows = get_texts_for_concept(
        WORKFLOW_ID,
        predicate=DISCOVERY_PREDICATE,
        lang="en-NZ",
        limit=5,
        recent_first=True,
    )
    if len(rows) != 1:
        raise ValueError(f"workflow_discovery_relation_count:{len(rows)}")
    raw_text = rows[0].get("text")
    try:
        payload = json.loads(str(raw_text or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("workflow_discovery_json_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("workflow_discovery_json_not_object")
    return payload


def run_migration(
    *, apply: bool, actor_user_id: str, actor_organisation_id: str
) -> dict[str, Any]:
    with override_current_actor(
        user_concept_id=actor_user_id,
        organisation_concept_id=actor_organisation_id,
    ):
        before = _read_discovery_exemplars()
        candidate, changed = rewrite_failure_case_discovery_exemplars(before)
        report: dict[str, Any] = {
            "workflow_id": WORKFLOW_ID,
            "predicate": DISCOVERY_PREDICATE,
            "mode": "apply" if apply else "preview",
            "changed": changed,
            "required_query_cues": candidate["required_query_cues"],
            "routing_notes": candidate["routing_notes"],
        }
        if not apply:
            return report

        write_result = upsert_singleton_text_relation(
            subject_concept_id=WORKFLOW_ID,
            predicate=DISCOVERY_PREDICATE,
            text=json.dumps(candidate, sort_keys=True),
            lang="en-NZ",
            policy="replace_others",
            provenance={
                "actor": actor_user_id,
                "issue_key": "JVNAUTOSCI-2591",
                "reason": "uc05_failure_case_routing_repair",
            },
            context={
                "workflow_id": WORKFLOW_ID,
                "source": "migrate_jvnautosci_2591_failure_case_routing",
            },
            garbage_collect=True,
        )
        after = _read_discovery_exemplars()
        _unchanged, still_changed = rewrite_failure_case_discovery_exemplars(after)
        if still_changed:
            raise ValueError("workflow_discovery_readback_mismatch")
        report["write_result"] = write_result
        report["canonical_readback_verified"] = True
        return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
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
