from __future__ import annotations

import hashlib
import mimetypes
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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


def _normalise_optional_concept_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("#v#"):
        return "#V#" + cleaned[3:]
    if cleaned.startswith("#V#"):
        return cleaned
    if cleaned.startswith("#"):
        return cleaned
    return f"#V#{cleaned.lstrip('#')}"


def _resolve_file_copy_actor_scope(
    *,
    user_concept_id: Any = None,
    organisation_concept_id: Any = None,
    namespace: Any = None,
) -> tuple[str | None, str | None, str | None]:
    from .namespace_service import (
        derive_actor_context_from_namespace,
        resolve_canonical_namespace,
    )

    clean_user = _normalise_optional_concept_id(user_concept_id)
    clean_org = _normalise_optional_concept_id(organisation_concept_id)
    canonical_namespace = resolve_canonical_namespace(namespace, clean_user, clean_org)
    namespace_user, namespace_org = derive_actor_context_from_namespace(
        canonical_namespace
    )
    return (
        namespace_user or clean_user,
        namespace_org or clean_org,
        canonical_namespace,
    )


def _resolve_namespace_source(
    *,
    namespace: Any,
    namespace_source: Any,
    canonical_namespace: str | None,
) -> str | None:
    clean_source = _normalise_optional_text(namespace_source)
    if clean_source:
        return clean_source
    if isinstance(namespace, str) and namespace.strip():
        return "request.namespace"
    if canonical_namespace:
        return "derived.user_org"
    return None


def _load_file_copy_concept_doc(
    *,
    file_copy_concept_id: str,
) -> Mapping[str, Any] | None:
    if not isinstance(file_copy_concept_id, str) or not file_copy_concept_id.strip():
        return None

    from ..db.repositories.concepts_repository import ConceptsRepository

    return ConceptsRepository.find_one(
        {"concept_id": file_copy_concept_id.strip()},
        {
            "concept_id": 1,
            "name": 1,
            "attributes": 1,
            "relationships": 1,
            "created_at": 1,
            "updated_at": 1,
        },
    )


def _file_copy_visibility_targets(
    concept_doc: Mapping[str, Any] | None,
) -> tuple[list[str], list[str]]:
    from ..security.visibility_predicates import (
        get_specific_to_org_values,
        get_specific_to_user_values,
    )

    attributes_raw = (
        concept_doc.get("attributes") if isinstance(concept_doc, Mapping) else None
    )
    attributes: Mapping[str, Any] = (
        attributes_raw if isinstance(attributes_raw, Mapping) else {}
    )
    relationships_raw = (
        concept_doc.get("relationships") if isinstance(concept_doc, Mapping) else None
    )
    relationships: Mapping[str, Any] = (
        relationships_raw if isinstance(relationships_raw, Mapping) else {}
    )

    specific_users = list(get_specific_to_user_values(dict(relationships)))
    specific_orgs = list(get_specific_to_org_values(dict(relationships)))
    if specific_users or specific_orgs:
        return specific_users, specific_orgs

    fallback_user = _normalise_optional_concept_id(attributes.get("user_concept_id"))
    fallback_org = _normalise_optional_concept_id(
        attributes.get("organisation_concept_id")
    )
    fallback_namespace = _normalise_optional_text(attributes.get("namespace"))
    namespace_user, namespace_org, _ignored_namespace = _resolve_file_copy_actor_scope(
        namespace=fallback_namespace
    )
    fallback_user_value = fallback_user or namespace_user
    fallback_org_value = fallback_org or namespace_org
    return (
        [fallback_user_value] if isinstance(fallback_user_value, str) else [],
        [fallback_org_value] if isinstance(fallback_org_value, str) else [],
    )


def _file_copy_visible_to_actor(
    *,
    concept_doc: Mapping[str, Any] | None,
    user_concept_id: Any = None,
    organisation_concept_id: Any = None,
    namespace: Any = None,
) -> bool:
    allowed_users, allowed_orgs = _file_copy_visibility_targets(concept_doc)
    if not allowed_users and not allowed_orgs:
        return True

    actor_user, actor_org, _canonical_namespace = _resolve_file_copy_actor_scope(
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    if actor_user and actor_user in allowed_users:
        return True
    if actor_org and actor_org in allowed_orgs:
        return True
    return False


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


def ensure_arxiv_markdown_file_type_exists(*, logger: Any | None = None) -> None:
    """Best-effort ensure the #V#arxiv_markdown_file type exists.

    This is a more specific subtype of #V#computer_file_copy.
    """

    type_concept_id = "#V#arxiv_markdown_file"

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
            name="arXiv Markdown file",
            concept_id=type_concept_id,
            parent_concept_ids=["#V#computer_file_copy"],
            create_as_instance=False,
            description=(
                "A computer file copy that is specifically an arXiv-sourced Markdown conversion."
            ),
            notes=(
                "Subtype created on-demand. Instances generally have blob store metadata (URI, key, content type, "
                "size, and hash) recorded as text relations, plus arXiv-specific metadata such as the arXiv ID."
            ),
            system_tags=["file", "blob_store", "arxiv", "markdown"],
        )
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[arxiv_markdown_file] Type ensure failed (continuing): %s",
                exc,
            )


def ensure_specific_computer_file_copy_type_exists(
    *,
    type_concept_id: str,
    name: str,
    description: str,
    parent_type_concept_id: str = "#V#computer_file_copy",
    notes: str | None = None,
    system_tags: list[str] | None = None,
    logger: Any | None = None,
) -> None:
    """Best-effort ensure a specific computer-file-copy subtype exists.

    Keep subtype creation centralised here so typing, upload handling, and
    interpretation flows share one authoritative pathway for file taxonomy.
    """

    clean_type_concept_id = (
        type_concept_id.strip() if isinstance(type_concept_id, str) else ""
    )
    clean_parent_type_concept_id = (
        parent_type_concept_id.strip()
        if isinstance(parent_type_concept_id, str)
        else "#V#computer_file_copy"
    )
    clean_name = name.strip() if isinstance(name, str) else ""
    clean_description = description.strip() if isinstance(description, str) else ""
    if (
        not clean_type_concept_id
        or not clean_parent_type_concept_id
        or not clean_name
        or not clean_description
    ):
        return

    ensure_computer_file_copy_type_exists(logger=logger)

    try:
        from . import concept_service

        def _concept_exists(concept_id: str) -> bool:
            try:
                return concept_service.get_concept_by_concept_id(concept_id) is not None
            except Exception:
                return False

        if _concept_exists(clean_type_concept_id):
            return

        if (
            clean_parent_type_concept_id != "#V#computer_file_copy"
            and not _concept_exists(clean_parent_type_concept_id)
        ):
            ensure_specific_computer_file_copy_type_exists(
                type_concept_id=clean_parent_type_concept_id,
                name=clean_parent_type_concept_id[3:].replace("_", " ").strip().title(),
                description=(
                    "A more specific computer file copy subtype created as part of "
                    "Von's reusable file-typing taxonomy."
                ),
                logger=logger,
            )

        concept_service.create_concept(
            name=clean_name,
            concept_id=clean_type_concept_id,
            parent_concept_ids=[clean_parent_type_concept_id],
            create_as_instance=False,
            description=clean_description,
            notes=notes,
            system_tags=list(system_tags or []),
        )
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[computer_file_copy] Specific subtype ensure failed for %s: %s",
                clean_type_concept_id,
                exc,
            )


def create_computer_file_copy_instance(
    *,
    type_concept_id: str = "#V#computer_file_copy",
    user_concept_id: str,
    organisation_concept_id: str | None = None,
    namespace: str | None = None,
    namespace_source: str | None = None,
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
    """Create a Computer File Copy instance with explicit visibility provenance.

    This matches the existing upload flow behaviour (text relations are treated
    as authoritative metadata for retrieval).
    """

    if type_concept_id == "#V#computer_file_copy":
        ensure_computer_file_copy_type_exists(logger=logger)
    elif type_concept_id == "#V#arxiv_pdf_file":
        ensure_arxiv_pdf_file_type_exists(logger=logger)
    elif type_concept_id == "#V#arxiv_markdown_file":
        ensure_arxiv_markdown_file_type_exists(logger=logger)
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
    (
        effective_user_concept_id,
        effective_organisation_concept_id,
        canonical_namespace,
    ) = _resolve_file_copy_actor_scope(
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    if not effective_user_concept_id:
        raise ValueError("user_concept_id is required")
    resolved_namespace_source = _resolve_namespace_source(
        namespace=namespace,
        namespace_source=namespace_source,
        canonical_namespace=canonical_namespace,
    )

    from . import concept_service
    from .text_value_service import upsert_text_for_concept

    attributes: dict[str, Any] = {
        "sha256": sha256,
        "size_bytes": size_bytes,
        "content_type": content_type,
        "blob_backend": blob_backend,
        "blob_key": blob_key,
        "blob_uri": blob_uri,
        "uploaded_at": uploaded_at,
    }
    if metadata:
        attributes.update(dict(metadata))
    attributes["user_concept_id"] = effective_user_concept_id
    if effective_organisation_concept_id:
        attributes["organisation_concept_id"] = effective_organisation_concept_id
    else:
        attributes.pop("organisation_concept_id", None)
    if canonical_namespace:
        attributes["namespace"] = canonical_namespace
    else:
        attributes.pop("namespace", None)
    if resolved_namespace_source:
        attributes["namespace_source"] = resolved_namespace_source
    else:
        attributes.pop("namespace_source", None)

    tags = ["file", "blob_store"]
    if type_concept_id == "#V#arxiv_pdf_file":
        tags.extend(["arxiv", "pdf"])
    elif type_concept_id == "#V#arxiv_markdown_file":
        tags.extend(["arxiv", "markdown"])

    concept_service.create_concept(
        name=name,
        concept_id=instance_concept_id,
        parent_concept_ids=[type_concept_id],
        create_as_instance=True,
        system_tags=tags,
        attributes=attributes,
        created_by_concept_id=effective_user_concept_id,
        organisation_concept_id=effective_organisation_concept_id,
        event_namespace=canonical_namespace,
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


def find_existing_computer_file_copy_instance(
    *,
    user_concept_id: str,
    blob_key: str,
    type_concept_id: str | None = None,
    sha256: str | None = None,
) -> ComputerFileCopyRecord | None:
    """Resolve an existing user-scoped file-copy concept by durable blob identity."""

    clean_user = user_concept_id.strip() if isinstance(user_concept_id, str) else ""
    clean_blob_key = blob_key.strip() if isinstance(blob_key, str) else ""
    clean_type = type_concept_id.strip() if isinstance(type_concept_id, str) else ""
    clean_sha256 = sha256.strip() if isinstance(sha256, str) else ""
    if not clean_user or not clean_blob_key:
        return None

    from ..db.repositories.concepts_repository import ConceptsRepository

    from ..security.visibility_predicates import SPECIFIC_TO_USER_PREDICATES

    query: dict[str, Any] = {
        "$or": [
            {f"relationships.{predicate}": clean_user}
            for predicate in SPECIFIC_TO_USER_PREDICATES
        ],
        "attributes.blob_key": clean_blob_key,
    }
    if clean_type:
        query["relationships.is_an_instance_of"] = clean_type
    if clean_sha256:
        query["attributes.sha256"] = clean_sha256

    cursor = ConceptsRepository.find(
        query,
        {
            "concept_id": 1,
            "attributes.uploaded_at": 1,
            "relationships.is_an_instance_of": 1,
        },
        sort=[("updated_at", -1), ("created_at", -1)],
        limit=1,
    )
    for doc in cursor:
        if not isinstance(doc, Mapping):
            continue
        concept_id = doc.get("concept_id")
        if not isinstance(concept_id, str) or not concept_id.strip():
            continue
        attributes_raw = doc.get("attributes")
        attributes: Mapping[str, Any] = (
            attributes_raw if isinstance(attributes_raw, Mapping) else {}
        )
        relationships_raw = doc.get("relationships")
        relationships: Mapping[str, Any] = (
            relationships_raw if isinstance(relationships_raw, Mapping) else {}
        )
        uploaded_at = _first_text_value(
            concept_id, "#V#has_upload_timestamp"
        ) or _normalise_optional_text(attributes.get("uploaded_at"))
        type_ids = _normalise_type_concept_ids(relationships)
        resolved_type = clean_type or (
            type_ids[0] if type_ids else "#V#computer_file_copy"
        )
        return ComputerFileCopyRecord(
            concept_id=concept_id,
            type_concept_id=resolved_type,
            uploaded_at=uploaded_at or _now_utc_iso(),
        )
    return None


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
    concept_doc: Mapping[str, Any] | None = None,
) -> FileCopyBlobInfo | None:
    if not isinstance(file_copy_concept_id, str) or not file_copy_concept_id.strip():
        return None

    concept_id = file_copy_concept_id.strip()
    if isinstance(concept_doc, Mapping):
        loaded_doc: Mapping[str, Any] | None = concept_doc
    else:
        loaded_doc = _load_file_copy_concept_doc(file_copy_concept_id=concept_id)
    if not isinstance(loaded_doc, Mapping):
        return None

    blob_key = _first_text_value(concept_id, "#V#has_blob_key")
    if not blob_key:
        return None

    blob_backend = _first_text_value(concept_id, "#V#has_blob_backend")
    blob_uri = _first_text_value(concept_id, "#V#has_blob_uri")
    content_type = _first_text_value(concept_id, "#V#has_mime_type")
    original_filename = _first_text_value(concept_id, "#V#has_original_filename")
    if not original_filename:
        name = loaded_doc.get("name")
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
    user_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
    namespace: str | None = None,
    max_bytes: int | None = None,
    allow_large: bool = False,
    logger: Any | None = None,
) -> dict[str, Any]:
    concept_doc = _load_file_copy_concept_doc(file_copy_concept_id=file_copy_concept_id)
    if not isinstance(concept_doc, Mapping):
        return {"success": False, "error": "not_found"}

    if not _file_copy_visible_to_actor(
        concept_doc=concept_doc,
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    ):
        if logger is not None:
            logger.info(
                "[file_copy] Access denied for concept=%s user=%s org=%s namespace=%s",
                file_copy_concept_id,
                user_concept_id,
                organisation_concept_id,
                namespace,
            )
        return {"success": False, "error": "not_found"}

    info = resolve_file_copy_blob_info(
        file_copy_concept_id=file_copy_concept_id,
        concept_doc=concept_doc,
    )
    if info is None:
        return {"success": False, "error": "not_found"}

    if info.blob_backend:
        try:
            from .blob_store import resolve_blob_store_backend_from_env

            env_backend = resolve_blob_store_backend_from_env()
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


def delete_file_copy_blob_and_concept(
    *,
    file_copy_concept_id: str,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Delete a blob-backed file-copy concept and its backing bytes.

    This helper intentionally reports partial outcomes explicitly. If blob
    deletion succeeds but concept deletion fails, callers receive
    ``partial=true`` and can surface a clear, non-ambiguous failure message.
    """

    info = resolve_file_copy_blob_info(file_copy_concept_id=file_copy_concept_id)
    if info is None:
        return {
            "success": False,
            "error": "not_found",
            "message": "File-copy concept not found or missing blob metadata.",
            "concept_deleted": False,
            "blob_deleted": False,
            "partial": False,
        }

    from .blob_store import get_blob_store_from_env

    try:
        store = get_blob_store_from_env()
        store.delete(info.blob_key)
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[file_copy] Blob delete failed for %s: %s", info.concept_id, exc
            )
        return {
            "success": False,
            "error": "blob_delete_failed",
            "message": f"Failed to delete backing file bytes: {exc}",
            "concept_id": info.concept_id,
            "blob_key": info.blob_key,
            "blob_uri": info.blob_uri,
            "concept_deleted": False,
            "blob_deleted": False,
            "partial": False,
        }

    try:
        from . import concept_service

        concept_deleted = bool(concept_service.delete_concept(info.concept_id))
    except Exception as exc:
        if logger is not None:
            logger.error(
                "[file_copy] Concept delete failed after blob delete for %s: %s",
                info.concept_id,
                exc,
            )
        return {
            "success": False,
            "error": "concept_delete_failed",
            "message": (
                "Backing file bytes were deleted, but concept deletion failed. "
                "The operation is partial and needs follow-up cleanup."
            ),
            "detail": str(exc),
            "concept_id": info.concept_id,
            "blob_key": info.blob_key,
            "blob_uri": info.blob_uri,
            "concept_deleted": False,
            "blob_deleted": True,
            "partial": True,
        }

    if not concept_deleted:
        return {
            "success": False,
            "error": "concept_delete_failed",
            "message": (
                "Backing file bytes were deleted, but concept deletion did not complete. "
                "The operation is partial and needs follow-up cleanup."
            ),
            "concept_id": info.concept_id,
            "blob_key": info.blob_key,
            "blob_uri": info.blob_uri,
            "concept_deleted": False,
            "blob_deleted": True,
            "partial": True,
        }

    return {
        "success": True,
        "message": "File copy and backing file bytes deleted successfully.",
        "concept_id": info.concept_id,
        "blob_key": info.blob_key,
        "blob_uri": info.blob_uri,
        "concept_deleted": True,
        "blob_deleted": True,
        "partial": False,
    }


def _normalise_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_type_concept_ids(relationships: Mapping[str, Any]) -> list[str]:
    raw = relationships.get("is_an_instance_of")
    if isinstance(raw, list):
        return [str(v) for v in raw if isinstance(v, str) and v.strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def build_file_copy_artifact_record(
    *,
    file_copy_concept_id: str | None = None,
    concept_doc: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build a canonical artefact record for a blob-backed file-copy concept.

    This record is the shared metadata shape used across retrieval and indexing
    paths so callers can pass one stable reference instead of ad-hoc field sets.
    """

    concept_id: str | None = None
    if isinstance(file_copy_concept_id, str) and file_copy_concept_id.strip():
        concept_id = file_copy_concept_id.strip()
    elif isinstance(concept_doc, Mapping):
        raw_concept_id = concept_doc.get("concept_id")
        if isinstance(raw_concept_id, str) and raw_concept_id.strip():
            concept_id = raw_concept_id.strip()
    if concept_id is None:
        return None

    doc: Mapping[str, Any] | None = concept_doc
    if not isinstance(doc, Mapping):
        try:
            resolved = _load_file_copy_concept_doc(file_copy_concept_id=concept_id)
        except Exception:
            resolved = None
        if not isinstance(resolved, Mapping):
            return None
        doc = resolved

    attributes_raw = doc.get("attributes")
    attributes: Mapping[str, Any] = (
        attributes_raw if isinstance(attributes_raw, Mapping) else {}
    )
    relationships_raw = doc.get("relationships")
    relationships: Mapping[str, Any] = (
        relationships_raw if isinstance(relationships_raw, Mapping) else {}
    )

    info = resolve_file_copy_blob_info(
        file_copy_concept_id=concept_id,
        concept_doc=doc,
    )
    if info is None:
        return None

    original_filename = _normalise_optional_text(
        info.original_filename
    ) or _normalise_optional_text(doc.get("name"))
    source_system = _normalise_optional_text(
        attributes.get("source_system")
    ) or _normalise_optional_text(attributes.get("source"))
    source_identifier = _normalise_optional_text(
        attributes.get("source_identifier")
    ) or _normalise_optional_text(attributes.get("original_identifier"))
    source_uri = _normalise_optional_text(
        attributes.get("source_uri")
    ) or _normalise_optional_text(attributes.get("source_url"))
    sha256 = _first_text_value(concept_id, "#V#has_sha256") or _normalise_optional_text(
        attributes.get("sha256")
    )
    uploaded_at = _first_text_value(
        concept_id, "#V#has_upload_timestamp"
    ) or _normalise_optional_text(attributes.get("uploaded_at"))
    ingested_at = _normalise_optional_text(attributes.get("ingested_at"))
    created_at = _normalise_optional_text(doc.get("created_at"))
    updated_at = _normalise_optional_text(doc.get("updated_at"))
    type_concept_ids = _normalise_type_concept_ids(relationships)
    namespace = _normalise_optional_text(attributes.get("namespace"))
    namespace_source = _normalise_optional_text(attributes.get("namespace_source"))
    user_concept_id = _normalise_optional_concept_id(attributes.get("user_concept_id"))
    organisation_concept_id = _normalise_optional_concept_id(
        attributes.get("organisation_concept_id")
    )
    if namespace and (user_concept_id is None or organisation_concept_id is None):
        namespace_user, namespace_org, _ignored_namespace = (
            _resolve_file_copy_actor_scope(namespace=namespace)
        )
        user_concept_id = user_concept_id or namespace_user
        organisation_concept_id = organisation_concept_id or namespace_org

    return {
        "artifact_id": concept_id,
        "concept_id": concept_id,
        "name": original_filename,
        "content_type": _normalise_optional_text(info.content_type),
        "size_bytes": info.size_bytes,
        "sha256": sha256,
        "blob": {
            "backend": _normalise_optional_text(info.blob_backend),
            "key": info.blob_key,
            "uri": _normalise_optional_text(info.blob_uri),
        },
        "type_concept_ids": type_concept_ids,
        "provenance": {
            "source": source_system,
            "source_identifier": source_identifier,
            "source_uri": source_uri,
            "namespace": namespace,
            "namespace_source": namespace_source,
            "user_concept_id": user_concept_id,
            "organisation_concept_id": organisation_concept_id,
            "uploaded_at": uploaded_at,
            "ingested_at": ingested_at,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    }


def _slugify_concept_id_for_blob_key(concept_id: str) -> str:
    cleaned = (concept_id or "").strip()
    if cleaned.startswith("#V#"):
        cleaned = cleaned[3:]
    cleaned = cleaned.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")
    return cleaned or "unknown"


def _safe_filename_for_blob_key(original_filename: str | None) -> str:
    safe_filename = re.sub(
        r"[^A-Za-z0-9._-]+", "_", str(original_filename or "").strip()
    ).strip("._")
    return safe_filename or "file.bin"


def import_bytes_file_copy(
    *,
    data: bytes,
    user_concept_id: str,
    organisation_concept_id: str | None = None,
    namespace: str | None = None,
    namespace_source: str | None = None,
    original_filename: str,
    content_type: str | None = None,
    type_concept_id: str = "#V#computer_file_copy",
    source_system: str = "bytes_import",
    source_identifier: str | None = None,
    source_uri: str | None = None,
    blob_key: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist bytes durably and register a blob-backed file-copy concept.

    Keep this as the shared ingestion path for any flow that already has the
    bytes in hand (filesystem imports, remote URL downloads, cached artefacts).
    """

    if not isinstance(data, (bytes, bytearray)) or not bytes(data):
        return {"success": False, "error": "empty_file"}
    if not isinstance(original_filename, str) or not original_filename.strip():
        return {"success": False, "error": "missing_original_filename"}

    data_bytes = bytes(data)
    original_filename = original_filename.strip()
    content_type = _normalise_optional_text(content_type)
    source_system = _normalise_optional_text(source_system) or "bytes_import"
    (
        effective_user_concept_id,
        effective_organisation_concept_id,
        canonical_namespace,
    ) = _resolve_file_copy_actor_scope(
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    if not effective_user_concept_id:
        return {"success": False, "error": "missing_user_concept_id"}
    resolved_namespace_source = _resolve_namespace_source(
        namespace=namespace,
        namespace_source=namespace_source,
        canonical_namespace=canonical_namespace,
    )

    sha256 = hashlib.sha256(data_bytes).hexdigest()
    size_bytes = len(data_bytes)
    uploaded_at = _now_utc_iso()
    user_slug = _slugify_concept_id_for_blob_key(effective_user_concept_id)
    safe_filename = _safe_filename_for_blob_key(original_filename)

    resolved_blob_key = (
        blob_key.strip()
        if isinstance(blob_key, str) and blob_key.strip()
        else f"imports/{user_slug}/{sha256}/{safe_filename}"
    )

    existing = find_existing_computer_file_copy_instance(
        user_concept_id=effective_user_concept_id,
        blob_key=resolved_blob_key,
        type_concept_id=type_concept_id,
        sha256=sha256,
    )
    if existing is not None:
        existing_info = resolve_file_copy_blob_info(
            file_copy_concept_id=existing.concept_id
        )
        artifact_record = build_file_copy_artifact_record(
            file_copy_concept_id=existing.concept_id
        )
        storage_payload = {
            "backend": getattr(existing_info, "blob_backend", None),
            "key": getattr(existing_info, "blob_key", resolved_blob_key),
            "uri": getattr(existing_info, "blob_uri", None),
            "content_type": getattr(existing_info, "content_type", content_type),
            "size_bytes": getattr(existing_info, "size_bytes", size_bytes),
            "metadata": None,
        }
        return {
            "success": True,
            "concept_id": existing.concept_id,
            "type_concept_id": existing.type_concept_id,
            "uploaded_at": existing.uploaded_at,
            "storage": storage_payload,
            "artifact_record": artifact_record,
            "typing": {
                "success": True,
                "typing_persisted": False,
                "status": "reused_existing",
            },
            "typing_result": None,
            "reused_existing": True,
        }

    persisted_metadata: dict[str, Any] = dict(metadata or {})
    persisted_metadata.update(
        {
            "original_filename": original_filename,
            "user_concept_id": effective_user_concept_id,
            "uploaded_at": uploaded_at,
            "source_system": source_system,
            "source_identifier": source_identifier,
            "source_uri": source_uri,
            "ingested_at": uploaded_at,
        }
    )
    if effective_organisation_concept_id:
        persisted_metadata["organisation_concept_id"] = (
            effective_organisation_concept_id
        )
    else:
        persisted_metadata.pop("organisation_concept_id", None)
    if canonical_namespace:
        persisted_metadata["namespace"] = canonical_namespace
    else:
        persisted_metadata.pop("namespace", None)
    if resolved_namespace_source:
        persisted_metadata["namespace_source"] = resolved_namespace_source
    else:
        persisted_metadata.pop("namespace_source", None)

    try:
        from .blob_uploads import BlobUploadError, put_bytes_durable

        stored = put_bytes_durable(
            key=resolved_blob_key,
            data=data_bytes,
            content_type=content_type,
            metadata=persisted_metadata,
            sha256=sha256,
            size_bytes=size_bytes,
        )
    except BlobUploadError as exc:
        return {
            "success": False,
            "error": "blob_store_upload_failed",
            "message": str(exc),
            "blob_key": resolved_blob_key,
        }

    try:
        created = create_computer_file_copy_instance(
            type_concept_id=type_concept_id,
            user_concept_id=effective_user_concept_id,
            organisation_concept_id=effective_organisation_concept_id,
            namespace=canonical_namespace,
            namespace_source=resolved_namespace_source,
            name=original_filename,
            sha256=sha256,
            size_bytes=size_bytes,
            content_type=content_type,
            blob_backend=stored.ref.backend,
            blob_key=stored.ref.key,
            blob_uri=stored.ref.uri,
            metadata=persisted_metadata,
        )
    except Exception as exc:
        return {
            "success": False,
            "error": "file_copy_register_failed",
            "message": str(exc),
            "blob_key": resolved_blob_key,
        }

    typing_result: dict[str, Any] | None = None
    typing_persist_result: dict[str, Any] | None = None
    try:
        from .file_copy_typing_service import (
            infer_file_copy_typing,
            persist_file_copy_typing,
        )

        typing_result = infer_file_copy_typing(
            content_type=content_type,
            original_filename=original_filename,
            size_bytes=size_bytes,
        )
        typing_persist_result = persist_file_copy_typing(
            file_copy_concept_id=created.concept_id,
            typing_result=typing_result,
        )
    except Exception as exc:
        typing_persist_result = {
            "success": False,
            "typing_persisted": False,
            "error": "typing_persist_failed",
            "message": str(exc),
        }

    artifact_record = build_file_copy_artifact_record(
        file_copy_concept_id=created.concept_id
    )

    return {
        "success": True,
        "concept_id": created.concept_id,
        "type_concept_id": created.type_concept_id,
        "uploaded_at": created.uploaded_at,
        "storage": {
            "backend": stored.ref.backend,
            "key": stored.ref.key,
            "uri": stored.ref.uri,
            "content_type": stored.ref.content_type,
            "size_bytes": stored.ref.size_bytes,
            "metadata": stored.ref.metadata,
        },
        "artifact_record": artifact_record,
        "typing": typing_persist_result,
        "typing_result": typing_result,
        "reused_existing": False,
    }


def import_local_file_copy(
    *,
    local_path: str,
    user_concept_id: str,
    organisation_concept_id: str | None = None,
    namespace: str | None = None,
    namespace_source: str | None = None,
    type_concept_id: str = "#V#computer_file_copy",
    allowed_root: str | Path | None = None,
    source_system: str = "filesystem_import",
    source_identifier: str | None = None,
    source_uri: str | None = None,
) -> dict[str, Any]:
    """Import a local file into blob storage and register a file-copy concept."""

    if not isinstance(local_path, str) or not local_path.strip():
        return {"success": False, "error": "missing_local_path"}
    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return {"success": False, "error": "missing_user_concept_id"}

    path = Path(local_path.strip()).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except Exception:
        return {
            "success": False,
            "error": "local_path_not_found",
            "local_path": local_path,
        }
    if not resolved.is_file():
        return {
            "success": False,
            "error": "local_path_not_file",
            "local_path": str(resolved),
        }

    if allowed_root is not None:
        try:
            root = Path(allowed_root).expanduser().resolve(strict=True)
        except Exception:
            return {"success": False, "error": "invalid_allowed_root"}
        if resolved != root and root not in resolved.parents:
            return {
                "success": False,
                "error": "path_outside_allowed_root",
                "local_path": str(resolved),
                "allowed_root": str(root),
            }

    try:
        data = resolved.read_bytes()
    except Exception as exc:
        return {
            "success": False,
            "error": "local_file_read_failed",
            "message": str(exc),
            "local_path": str(resolved),
        }
    if not data:
        return {"success": False, "error": "empty_file", "local_path": str(resolved)}

    original_filename = resolved.name
    content_type, _encoding = mimetypes.guess_type(original_filename)

    try:
        resolved_uri = resolved.as_uri()
    except Exception:
        resolved_uri = None

    result = import_bytes_file_copy(
        data=data,
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
        namespace_source=namespace_source,
        original_filename=original_filename,
        content_type=content_type,
        type_concept_id=type_concept_id,
        source_system=source_system,
        source_identifier=source_identifier or str(resolved),
        source_uri=source_uri or resolved_uri,
        metadata={"local_path": str(resolved)},
    )
    if isinstance(result, dict):
        result = dict(result)
        result["local_path"] = str(resolved)
    return result
