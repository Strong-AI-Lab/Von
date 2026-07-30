"""Audit or repair weak represented descriptions for registered MCP tools.

The registered method catalogue is the exact executable interface contract.
Vontology may provide a richer capability description, but an empty legacy
relation or materialisation placeholder must not displace that contract in the
model-visible capability catalogue.

Preview is the default.  ``--apply`` repairs only represented tool concepts
whose current description is structurally unusable, then verifies canonical
text-relation read-back.  It does not create Vontology concepts for methods
that do not otherwise need represented metadata.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
load_dotenv(_PROJECT_ROOT / ".env", override=False)

from src.backend.integrations.internal_mcp.catalogue import (
    build_default_catalogue,
)
from src.backend.security.access_control import override_current_actor
from src.backend.services.text_value_service import (
    get_texts_for_concept,
    upsert_singleton_text_relation,
)
from src.backend.services.tool_metadata_service import (
    _load_from_vontology,
    invalidate_cache,
    is_usable_tool_capability_description,
)

ISSUE_KEY = "JVNAUTOSCI-2614"
DESCRIPTION_PREDICATE = "#V#hasDescription"
DEFAULT_ACTOR_USER_ID = "#V#michael_witbrock"
DEFAULT_ACTOR_ORGANISATION_ID = "#V#university_of_auckland_strong_ai_lab"


def _canonical_descriptions() -> dict[str, str]:
    catalogue = build_default_catalogue()
    return {
        name: str(catalogue.get(name).description or "").strip()
        for name in catalogue.list_methods()
    }


def _read_description(concept_id: str) -> str | None:
    rows = get_texts_for_concept(
        concept_id,
        predicate=DESCRIPTION_PREDICATE,
        lang="en-NZ",
        limit=5,
        recent_first=True,
    )
    texts = [
        str(row.get("text") or "").strip()
        for row in rows
        if str(row.get("text") or "").strip()
    ]
    return texts[0] if texts else None


def run_migration(
    *,
    apply: bool,
    actor_user_id: str,
    actor_organisation_id: str,
) -> dict[str, Any]:
    canonical = _canonical_descriptions()
    invalid_canonical = sorted(
        name
        for name, description in canonical.items()
        if not is_usable_tool_capability_description(
            description,
            tool_name=name,
        )
    )
    if invalid_canonical:
        raise ValueError(
            "registered_tool_descriptions_unusable:" + ",".join(invalid_canonical)
        )

    with override_current_actor(
        user_concept_id=actor_user_id,
        organisation_concept_id=actor_organisation_id,
    ):
        represented = _load_from_vontology()
        repairs: list[dict[str, str]] = []
        for name, metadata in sorted(represented.items()):
            concept_id = str(metadata.concept_id or "").strip()
            fallback = canonical.get(name)
            if not concept_id or not fallback:
                continue
            if is_usable_tool_capability_description(
                metadata.description,
                tool_name=name,
            ):
                continue
            repairs.append(
                {
                    "tool_name": name,
                    "concept_id": concept_id,
                    "replacement": fallback,
                }
            )

        report: dict[str, Any] = {
            "schema_version": "tool_capability_description_migration.v1",
            "issue_key": ISSUE_KEY,
            "mode": "apply" if apply else "preview",
            "registered_tool_count": len(canonical),
            "registered_description_issue_count": len(invalid_canonical),
            "represented_tool_count": len(represented),
            "repair_count": len(repairs),
            "repair_tool_names": [repair["tool_name"] for repair in repairs],
        }
        if not apply:
            return report

        write_receipts: list[dict[str, Any]] = []
        for repair in repairs:
            write_result = upsert_singleton_text_relation(
                subject_concept_id=repair["concept_id"],
                predicate=DESCRIPTION_PREDICATE,
                text=repair["replacement"],
                lang="en-NZ",
                policy="replace_others",
                provenance={
                    "actor": actor_user_id,
                    "issue_key": ISSUE_KEY,
                    "reason": "repair_model_visible_tool_capability_description",
                },
                context={
                    "tool_name": repair["tool_name"],
                    "source": ("migrate_jvnautosci_2614_tool_capability_descriptions"),
                },
                garbage_collect=True,
            )
            readback = _read_description(repair["concept_id"])
            if readback != repair["replacement"]:
                raise ValueError(
                    "tool_description_readback_mismatch:" + repair["tool_name"]
                )
            write_receipts.append(
                {
                    "tool_name": repair["tool_name"],
                    "concept_id": repair["concept_id"],
                    "kept_relation_id": write_result.get("kept_relation_id"),
                    "replaced_count": write_result.get("replaced_count"),
                    "canonical_readback_verified": True,
                }
            )

        invalidate_cache()
        report["write_receipts"] = write_receipts
        report["canonical_readback_verified_count"] = len(write_receipts)
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
