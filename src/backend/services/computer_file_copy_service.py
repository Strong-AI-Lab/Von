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


@dataclass(frozen=True)
class FileCopyBlobInfo:
    concept_id: str
    blob_key: str
    blob_backend: str | None
    blob_uri: str | None
    content_type: str | None
    original_filename: str | None
    size_bytes: int | None


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


def ensure_arxiv_pdf_file_type_exists(*, logger: Any | None = None) -> None:
    """Best-effort ensure the #V#arxiv_pdf_file type exists.

    This is a more specific subtype of #V#computer_file_copy.
    """

    type_concept_id = "#V#arxiv_pdf_file"

    ensure_computer_file_copy_type_exists(logger=logger)

    try:
        from . import concept_service

        def _concept_exists(concept_id: str) -> bool:
            try:
                return concept_service.get_concept_by_concept_id(concept_id) is not None
            except Exception:
                return False

        if _concept_exists(type_concept_id):
            return

        concept_service.create_concept(
            name="arXiv PDF file",
            concept_id=type_concept_id,
            parent_concept_ids=["#V#computer_file_copy"],
            create_as_instance=False,
            description=(
                "A computer file copy that is specifically an arXiv-sourced PDF. "
                "Instances are typically created by Von's arXiv import tools."
            ),
            notes=(
                "Subtype created on-demand. Instances generally have blob store metadata (URI, key, content type, "
                "size, and hash) recorded as text relations, plus arXiv-specific metadata such as the arXiv ID."
            ),
            system_tags=["file", "blob_store", "arxiv", "pdf"],
        )
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[arxiv_pdf_file] Type ensure failed (continuing): %s",
                exc,
            )


def create_computer_file_copy_instance(
    *,
    type_concept_id: str = "#V#computer_file_copy",
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

    if type_concept_id == "#V#computer_file_copy":
        ensure_computer_file_copy_type_exists(logger=logger)
    elif type_concept_id == "#V#arxiv_pdf_file":
        ensure_arxiv_pdf_file_type_exists(logger=logger)
    else:
        # Best-effort: ensure the base file-copy type exists; assume caller-provided
        # type already exists or will be created elsewhere.
        ensure_computer_file_copy_type_exists(logger=logger)

    type_slug = (
        type_concept_id[3:]
        if isinstance(type_concept_id, str) and type_concept_id.startswith("#V#")
        else "computer_file_copy"
    )
    instance_concept_id = f"#V#{type_slug}_{uuid.uuid4().hex}"
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

    tags = ["file", "blob_store"]
    if type_concept_id == "#V#arxiv_pdf_file":
        tags.extend(["arxiv", "pdf"])

    concept_service.create_concept(
        name=name,
        concept_id=instance_concept_id,
        parent_concept_ids=[type_concept_id],
        create_as_instance=True,
        system_tags=tags,
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


def _first_text_value(concept_id: str, predicate: str) -> str | None:
    try:
        from .text_value_service import get_texts_for_concept

        rows = get_texts_for_concept(concept_id, predicate=predicate, limit=1)
        if rows and isinstance(rows[0], dict):
            text = rows[0].get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    except Exception:
        return None
    return None


def resolve_file_copy_blob_info(
    *,
    file_copy_concept_id: str,
) -> FileCopyBlobInfo | None:
    if not isinstance(file_copy_concept_id, str) or not file_copy_concept_id.strip():
        return None

    from ..db.repositories.concepts_repository import ConceptsRepository

    concept_id = file_copy_concept_id.strip()
    concept_doc = ConceptsRepository.find_one(
        {"concept_id": concept_id}, {"concept_id": 1, "name": 1}
    )
    if not isinstance(concept_doc, dict):
        return None

    blob_key = _first_text_value(concept_id, "#V#has_blob_key")
    if not blob_key:
        return None

    blob_backend = _first_text_value(concept_id, "#V#has_blob_backend")
    blob_uri = _first_text_value(concept_id, "#V#has_blob_uri")
    content_type = _first_text_value(concept_id, "#V#has_mime_type")
    original_filename = _first_text_value(concept_id, "#V#has_original_filename")
    if not original_filename:
        name = concept_doc.get("name")
        if isinstance(name, str) and name.strip():
            original_filename = name.strip()

    size_bytes = None
    size_text = _first_text_value(concept_id, "#V#has_size_bytes")
    if isinstance(size_text, str) and size_text.strip():
        try:
            size_bytes = int(size_text.strip())
        except ValueError:
            size_bytes = None

    return FileCopyBlobInfo(
        concept_id=concept_id,
        blob_key=blob_key,
        blob_backend=blob_backend,
        blob_uri=blob_uri,
        content_type=content_type,
        original_filename=original_filename,
        size_bytes=size_bytes,
    )


def fetch_file_copy_bytes(
    *,
    file_copy_concept_id: str,
    max_bytes: int | None = None,
    allow_large: bool = False,
    logger: Any | None = None,
) -> dict[str, Any]:
    info = resolve_file_copy_blob_info(file_copy_concept_id=file_copy_concept_id)
    if info is None:
        return {"success": False, "error": "not_found"}

    if info.blob_backend:
        try:
            import os

            env_backend = (
                os.environ.get("VON_BLOB_STORE_BACKEND") or "local"
            ).strip().lower()
            if info.blob_backend.strip().lower() != env_backend and logger is not None:
                logger.warning(
                    "[file_copy] Blob backend mismatch for %s: concept=%s env=%s",
                    info.concept_id,
                    info.blob_backend,
                    env_backend,
                )
        except Exception:
            pass

    if (
        max_bytes is not None
        and isinstance(info.size_bytes, int)
        and info.size_bytes > max_bytes
        and not allow_large
    ):
        return {
            "success": False,
            "error": "file_too_large",
            "size_bytes": info.size_bytes,
            "max_bytes": max_bytes,
        }

    from .blob_store import get_blob_store_from_env

    store = get_blob_store_from_env()
    try:
        data_bytes = store.get_bytes(info.blob_key)
    except Exception as exc:
        if logger is not None:
            logger.warning("[file_copy] Blob fetch failed: %s", exc)
        return {"success": False, "error": "blob_fetch_failed"}

    if (
        max_bytes is not None
        and isinstance(data_bytes, (bytes, bytearray))
        and len(data_bytes) > max_bytes
        and not allow_large
    ):
        return {
            "success": False,
            "error": "file_too_large",
            "size_bytes": len(data_bytes),
            "max_bytes": max_bytes,
        }

    return {
        "success": True,
        "info": info,
        "data": bytes(data_bytes),
    }
