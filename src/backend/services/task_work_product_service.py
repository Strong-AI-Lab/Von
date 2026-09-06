"""Actor-visible current work products, explicitly linked from canonical tasks.

The relation chooses a product identity, not a revision. A current body is only
reported when its actor-effective hasContent projection has exactly one row.
No narrative guesses, newest-row selection, copied product store or authority
are introduced by this projection.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import can_access_concept
from .text_value_service import get_texts_for_concept

from .task_ontology_service import PREDICATE_HAS_CURRENT_WORK_PRODUCT

logger = logging.getLogger(__name__)


def resolve_task_work_product(
    task_doc: Mapping[str, Any], *, include_content: bool = False
) -> dict[str, Any]:
    relationships = task_doc.get("relationships") or {}
    raw = relationships.get(PREDICATE_HAS_CURRENT_WORK_PRODUCT) or []
    targets = list(dict.fromkeys([raw] if isinstance(raw, str) else raw))
    if not targets:
        return {"status": "missing"}
    if len(targets) != 1:
        return {"status": "ambiguous_link"}
    product_id = targets[0]
    try:
        if not can_access_concept(product_id):
            return {"status": "unavailable"}
        doc = ConceptsRepository.find_one({"concept_id": product_id})
        if not doc:
            return {"status": "unavailable"}
        reference = {"concept_id": product_id}
        rows = get_texts_for_concept(
            product_id,
            predicate="#V#hasContent",
            limit=2,
            context_view="actor_effective",
        )
        if len(rows) > 1:
            return {**reference, "status": "ambiguous_content"}
        if not rows or not str(rows[0].get("text") or "").strip():
            return {**reference, "status": "missing_content"}
        row = rows[0]
        content = row["text"]
        result = {
            **reference,
            "status": "ready",
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "predicate": "#V#hasContent",
            "language": row.get("lang"),
            "context_view": "actor_effective",
            "row_kind": row.get("row_kind", "base_text_relation"),
        }
        if include_content:
            result["content"] = content
        return result
    except Exception as exc:
        logger.warning("Task work-product read unavailable: %s", type(exc).__name__)
        return {"status": "unavailable"}
