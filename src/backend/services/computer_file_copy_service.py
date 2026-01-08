from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


@dataclass(frozen=True)
class ComputerFileCopyRecord:
    concept_id: str
    type_concept_id: str
    uploaded_at: str


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_computer_file_copy_type_exists(*, logger: Any | None = None) -> None:
    type_concept_id = "#V#computer_file_copy"

    try:
        from . import concept_service
        from ..vontology.utils_vontology import (
            THING_PRIMARY_ID,
            create_vontology_concept,
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
            "#V#store_of_information"
            if _concept_exists("#V#store_of_information")
            else THING_PRIMARY_ID
        )
        if parent_id == THING_PRIMARY_ID:
            ensure_thing_exists_and_link_orphans()

        created = create_vontology_concept(
            parent_id=parent_id,
            new_concept_name="Computer File Copy",
            create_as_instance=False,
            description=(
                "A computer file copy is an information-bearing artefact representing a specific stored byte sequence "
                "(for example an uploaded file stored in Von's blob store)."
            ),
            notes=(
                "Created on-demand by Von's file ingestion flows. Instances typically have blob store metadata "
                "(URI, key, content type, size, and hash) recorded as text relations."
            ),
        )
        if not created.get("success"):
            if logger is not None:
                logger.warning(
                    "[computer_file_copy] Failed to create type: %s",
                    created.get("message"),
                )
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[computer_file_copy] Type ensure failed (continuing): %s",
                exc,
            )


def create_computer_file_copy_instance(
    *,
    user_concept_id: str,
    name: str,
    sha256: str,
    size_bytes: int,
    content_type: str | None,
    blob_backend: str,
    blob_key: str,
    blob_uri: str,
    metadata: Mapping[str, Any] | None = None,
    logger: Any | None = None,
) -> ComputerFileCopyRecord:
    """Create a Computer File Copy instance scoped to the current user.

    This matches the existing upload flow behaviour (text relations are treated
    as authoritative metadata for retrieval).
    """

    ensure_computer_file_copy_type_exists(logger=logger)

    type_concept_id = "#V#computer_file_copy"
    instance_concept_id = f"#V#computer_file_copy_{uuid.uuid4().hex}"
    uploaded_at = _now_utc_iso()

    from . import concept_service
    from .text_value_service import upsert_text_for_concept

    attributes: dict[str, Any] = {
        "sha256": sha256,
        "size_bytes": size_bytes,
        "content_type": content_type,
        "blob_backend": blob_backend,
        "blob_key": blob_key,
        "blob_uri": blob_uri,
    }
    if metadata:
        attributes.update(dict(metadata))

    concept_service.create_concept(
        name=name,
        concept_id=instance_concept_id,
        parent_concept_ids=[type_concept_id],
        create_as_instance=True,
        system_tags=["file", "blob_store"],
        attributes=attributes,
    )

    concept_service.update_concept(
        instance_concept_id,
        {"relationships.specific_to_user": [user_concept_id.strip()]},
    )

    upsert_text_for_concept(
        subject_concept_id=instance_concept_id,
        predicate="#V#has_original_filename",
        text=name,
        lang="en-NZ",
    )
    upsert_text_for_concept(
        subject_concept_id=instance_concept_id,
        predicate="#V#has_sha256",
        text=sha256,
        lang="en-NZ",
    )
    upsert_text_for_concept(
        subject_concept_id=instance_concept_id,
        predicate="#V#has_size_bytes",
        text=str(size_bytes),
        lang="en-NZ",
    )
    upsert_text_for_concept(
        subject_concept_id=instance_concept_id,
        predicate="#V#has_upload_timestamp",
        text=str(uploaded_at),
        lang="en-NZ",
    )
    if content_type:
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_mime_type",
            text=content_type,
            lang="en-NZ",
        )

    upsert_text_for_concept(
        subject_concept_id=instance_concept_id,
        predicate="#V#has_blob_backend",
        text=str(blob_backend),
        lang="en-NZ",
    )
    upsert_text_for_concept(
        subject_concept_id=instance_concept_id,
        predicate="#V#has_blob_key",
        text=str(blob_key),
        lang="en-NZ",
    )
    upsert_text_for_concept(
        subject_concept_id=instance_concept_id,
        predicate="#V#has_blob_uri",
        text=str(blob_uri),
        lang="en-NZ",
    )

    return ComputerFileCopyRecord(
        concept_id=instance_concept_id,
        type_concept_id=type_concept_id,
        uploaded_at=uploaded_at,
    )
