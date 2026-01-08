from __future__ import annotations

import hashlib
import re
from typing import Any


def _stable_paper_instance_concept_id(arxiv_id: str) -> str:
    raw = (arxiv_id or "").strip().lower()
    if raw.startswith("arxiv:"):
        raw = raw.split(":", 1)[1].strip()

    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]

    # Keep IDs reasonably short for storage and UI.
    if len(slug) > 64:
        slug = slug[:64].rstrip("_")

    return (
        f"#V#paper_on_arxiv_{slug}_{digest}" if slug else f"#V#paper_on_arxiv_{digest}"
    )


def ensure_paper_on_arxiv_type_exists(*, logger: Any | None = None) -> None:
    """Best-effort ensure the #V#paper_on_arxiv type exists."""

    type_concept_id = "#V#paper_on_arxiv"

    try:
        from . import concept_service
        from ..vontology.utils_vontology import (
            THING_PRIMARY_ID,
            ensure_thing_exists_and_link_orphans,
        )

        def _concept_exists(concept_id: str) -> bool:
            try:
                return concept_service.get_concept_by_concept_id(concept_id) is not None
            except Exception:
                return False

        if _concept_exists(type_concept_id):
            return

        parent_id = (
            "#V#scholarly_work"
            if _concept_exists("#V#scholarly_work")
            else THING_PRIMARY_ID
        )
        if parent_id == THING_PRIMARY_ID:
            ensure_thing_exists_and_link_orphans()

        concept_service.create_concept(
            name="Paper on arXiv",
            concept_id=type_concept_id,
            parent_concept_ids=[parent_id],
            create_as_instance=False,
            description="A scholarly work available on arXiv.",
            notes="Created on-demand by Von's arXiv import flows.",
            system_tags=["arxiv", "paper"],
        )
    except Exception as exc:
        if logger is not None:
            logger.warning("[paper_on_arxiv] Type ensure failed (continuing): %s", exc)


def ensure_arxiv_paper_instance(
    *,
    user_concept_id: str,
    arxiv_id: str,
    logger: Any | None = None,
) -> str:
    """Ensure a stable per-arXiv-ID paper instance exists; returns its concept_id."""

    ensure_paper_on_arxiv_type_exists(logger=logger)

    from . import concept_service
    from .text_value_service import upsert_text_for_concept

    type_concept_id = "#V#paper_on_arxiv"
    instance_concept_id = _stable_paper_instance_concept_id(arxiv_id)

    existing = None
    try:
        existing = concept_service.get_concept_by_concept_id(instance_concept_id)
    except Exception:
        existing = None

    if not existing:
        concept_service.create_concept(
            name=f"arXiv paper {arxiv_id}",
            concept_id=instance_concept_id,
            parent_concept_ids=[type_concept_id],
            create_as_instance=True,
            system_tags=["arxiv", "paper"],
            attributes={
                "source": "arxiv",
                "arxiv_id": arxiv_id,
            },
        )
        concept_service.update_concept(
            instance_concept_id,
            {"relationships.specific_to_user": [user_concept_id.strip()]},
        )

        # Make the arXiv identifier directly searchable without introducing a new predicate.
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="hasName",
            text=str(arxiv_id).strip(),
            lang="en-NZ",
            context={"name_type": "CODE"},
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="hasName",
            text=f"https://arxiv.org/abs/{str(arxiv_id).strip()}",
            lang="en-NZ",
            context={"name_type": "CODE"},
        )

    return instance_concept_id


def link_file_copy_to_arxiv_paper(
    *,
    user_concept_id: str,
    arxiv_id: str,
    file_copy_concept_id: str,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Link a #V#computer_file_copy to the corresponding #V#paper_on_arxiv instance."""

    from ..db.repositories.concepts_repository import ConceptsRepository

    paper_concept_id = ensure_arxiv_paper_instance(
        user_concept_id=user_concept_id,
        arxiv_id=arxiv_id,
        logger=logger,
    )

    changed = ConceptsRepository.mutate_relationship_edge(
        file_copy_concept_id,
        "related_to",
        paper_concept_id,
        action="add",
        maintain_inverse=True,
    )

    # Prefer semantically meaningful predicates for file<->propositional content.
    # These predicates were introduced specifically for the arXiv import path.
    changed = (
        ConceptsRepository.mutate_relationship_edge(
            file_copy_concept_id,
            "#V#computer_file_for_propositional_information_thing",
            paper_concept_id,
            action="add",
            maintain_inverse=False,
        )
        or changed
    )
    changed = (
        ConceptsRepository.mutate_relationship_edge(
            paper_concept_id,
            "#V#propositional_information_thing_has_computer_file",
            file_copy_concept_id,
            action="add",
            maintain_inverse=False,
        )
        or changed
    )

    return {
        "paper_concept_id": paper_concept_id,
        "linked": True,
        "changed": bool(changed),
    }
