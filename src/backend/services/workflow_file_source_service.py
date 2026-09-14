"""Actor-scoped source text for workflow consumers, independent of RAG indexing."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from ..security.access_control import override_current_actor


def project_workflow_file_sources(
    inputs: Mapping[str, Any],
    *,
    user_concept_id: str,
    organisation_concept_id: str | None,
    namespace: str,
    max_chars: int = 20_000,
) -> list[dict[str, Any]]:
    """Reuse canonical extracted text, otherwise use the bounded byte reader.

    IDs are explicit launch inputs, never inferred from names or prompt text.
    This read performs no interpretation, indexing or represented mutations.
    Unavailable text is a scoped observation, not evidence of global absence.
    """
    from .computer_file_copy_service import (
        _load_file_copy_concept_doc,
        _file_copy_content_visible_to_actor,
        fetch_file_copy_bytes,
    )
    from .file_bytes_text_projection_service import extract_file_bytes_text_projection
    from .text_value_service import get_texts_for_concept

    ids = [inputs.get("file_copy_concept_id")]
    plural = inputs.get("file_copy_concept_ids")
    if isinstance(plural, list):
        ids.extend(plural)
    ids = list(
        dict.fromkeys(
            value.strip() for value in ids if isinstance(value, str) and value.strip()
        )
    )
    result = []
    remaining = max_chars
    with override_current_actor(user_concept_id, organisation_concept_id):
        for position, concept_id in enumerate(ids):
            item: dict[str, Any] = {"file_copy_concept_id": concept_id}
            if remaining <= 0 or position >= 4:
                result.append(
                    {**item, "status": "omitted", "reason": "source_text_budget"}
                )
                continue
            try:
                doc = _load_file_copy_concept_doc(file_copy_concept_id=concept_id)
                if not doc or not _file_copy_content_visible_to_actor(
                    concept_doc=doc,
                    user_concept_id=user_concept_id,
                    organisation_concept_id=organisation_concept_id,
                    namespace=namespace,
                ):
                    result.append(
                        {**item, "status": "unavailable", "reason": "not_found"}
                    )
                    continue
                rows = get_texts_for_concept(
                    concept_id,
                    predicate="hasContent",
                    limit=1,
                    recent_first=True,
                    context_view="actor_effective",
                )
                text = rows[0].get("text") if rows else None
                if isinstance(text, str) and text.strip():
                    item.update(
                        source="persisted_hasContent",
                        text=text[:remaining],
                        text_truncated=len(text) > remaining,
                    )
                else:
                    fetched = fetch_file_copy_bytes(
                        file_copy_concept_id=concept_id,
                        user_concept_id=user_concept_id,
                        organisation_concept_id=organisation_concept_id,
                        namespace=namespace,
                        max_bytes=5_000_000,
                    )
                    if not fetched.get("success"):
                        result.append(
                            {
                                **item,
                                "status": "unavailable",
                                "reason": fetched.get("error"),
                            }
                        )
                        continue
                    data = fetched["data"]
                    info = fetched["info"]
                    projection = extract_file_bytes_text_projection(
                        data=data,
                        content_type=info.content_type,
                        original_filename=info.original_filename,
                        max_text_chars=remaining,
                    )
                    item.update(
                        projection,
                        source="file_bytes",
                        sha256=hashlib.sha256(data).hexdigest(),
                    )
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    item.update(
                        status="available",
                        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    )
                    remaining -= len(text)
                else:
                    item.update(
                        status="unavailable",
                        reason=item.get("text_extraction_error")
                        or "no_extractable_text",
                    )
            except Exception as exc:
                item.update(status="unavailable", reason=type(exc).__name__)
            result.append(item)
    return result
