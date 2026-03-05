"""Bootstrap canonical representation contract profiles in Vontology.

This script ensures profile concepts exist, then persists canonical JSON
profiles using singleton text relations so turn-execution contract logic can
load semantics from Vontology at runtime.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _bootstrap_sys_path() -> None:
    repo_root = Path(__file__).parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    src_root = repo_root / "src"
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))


_bootstrap_sys_path()

from src.backend.services import concept_service  # noqa: E402
from src.backend.services.representation_contract_vontology_service import (  # noqa: E402
    bootstrap_canonical_representation_contract_profiles,
    canonical_representation_profile_concept_ids,
)

PROFILE_TYPE_CONCEPT_ID = "#V#representation_contract_profile"
PROFILE_TYPE_NAME = "Representation Contract Profile"

PROFILE_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    (
        "#V#representation_contract_profile_paper",
        "Paper Representation Contract Profile",
        "Profile semantics for scholarly paper representation intents.",
    ),
    (
        "#V#representation_contract_profile_person",
        "Person Representation Contract Profile",
        "Profile semantics for person representation intents from CVs or business cards.",
    ),
    (
        "#V#representation_contract_profile_company",
        "Company Representation Contract Profile",
        "Profile semantics for company representation intents from web pages or documents.",
    ),
    (
        "#V#representation_contract_profile_meeting",
        "Meeting Representation Contract Profile",
        "Profile semantics for meeting representation intents from transcripts or calendar artefacts.",
    ),
)

PROFILE_TEXT_PREDICATE_CONCEPT_ID = "#V#has_representation_contract_profile_json"
PROFILE_TEXT_PREDICATE_NAME = "has_representation_contract_profile_json"


def _get_concept(concept_id: str):
    try:
        return concept_service.get_concept_by_concept_id(concept_id)
    except Exception:
        return None


def _ensure_type_concept(concept_id: str, name: str, description: str) -> None:
    if _get_concept(concept_id):
        print(f"[bootstrap] exists: {concept_id}")
        return
    print(f"[bootstrap] creating type concept: {concept_id}")
    concept_service.create_concept(
        concept_id=concept_id,
        name=name,
        description=description,
        instance_of_type="#V#type",
    )


def _ensure_profile_concept(
    concept_id: str,
    name: str,
    description: str,
) -> None:
    if _get_concept(concept_id):
        print(f"[bootstrap] exists: {concept_id}")
        return
    print(f"[bootstrap] creating profile concept: {concept_id}")
    concept_service.create_concept(
        concept_id=concept_id,
        name=name,
        description=description,
        instance_of_type=PROFILE_TYPE_CONCEPT_ID,
    )


def _ensure_profile_text_predicate_concept() -> None:
    if _get_concept(PROFILE_TEXT_PREDICATE_CONCEPT_ID):
        print(f"[bootstrap] exists: {PROFILE_TEXT_PREDICATE_CONCEPT_ID}")
        return
    print(
        "[bootstrap] creating predicate concept: "
        f"{PROFILE_TEXT_PREDICATE_CONCEPT_ID}"
    )
    try:
        concept_service.create_concept(
            concept_id=PROFILE_TEXT_PREDICATE_CONCEPT_ID,
            name=PROFILE_TEXT_PREDICATE_NAME,
            description=(
                "Singleton JSON payload that stores representation contract "
                "profile semantics for turn-execution required-effects contracts."
            ),
            instance_of_type="#V#predicate",
        )
    except Exception as exc:
        print(
            "[bootstrap] warning: could not create predicate concept as "
            f"#V#predicate instance ({exc}); creating as generic type instead."
        )
        concept_service.create_concept(
            concept_id=PROFILE_TEXT_PREDICATE_CONCEPT_ID,
            name=PROFILE_TEXT_PREDICATE_NAME,
            description=(
                "Singleton JSON payload that stores representation contract "
                "profile semantics for turn-execution required-effects contracts."
            ),
            instance_of_type="#V#type",
        )


def main() -> int:
    try:
        print("[bootstrap] ensuring representation profile ontology concepts...")
        _ensure_type_concept(
            PROFILE_TYPE_CONCEPT_ID,
            PROFILE_TYPE_NAME,
            "Type for representation contract profile concepts.",
        )
        _ensure_profile_text_predicate_concept()
        for concept_id, name, description in PROFILE_DEFINITIONS:
            _ensure_profile_concept(concept_id, name, description)

        print("[bootstrap] writing canonical profile JSON text relations...")
        report = bootstrap_canonical_representation_contract_profiles(
            concept_ids=canonical_representation_profile_concept_ids(),
            predicate=PROFILE_TEXT_PREDICATE_CONCEPT_ID,
            language="en-NZ",
            policy="replace_others",
            provenance={
                "source": "scripts/bootstrap_representation_contract_profiles.py",
                "owner": "JVNAUTOSCI-1377",
            },
            context={
                "purpose": "representation_required_effects_contract_profiles",
                "task": "JVNAUTOSCI-1377",
            },
            garbage_collect=True,
        )
        print("[bootstrap] report:")
        print(report)
        return 0 if bool(report.get("success")) else 1
    except Exception as exc:
        print(f"[bootstrap] failed: {exc}")
        print(
            "[bootstrap] hint: ensure Vontology Mongo connectivity is available "
            "(for Atlas, verify IP allow-list and credentials)."
        )
        return 2


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
