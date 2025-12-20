"""Embedding-driven concept similarity search service.

This module provides an embedding implementation backed by a FAISS index that
supports the initial concept similarity workflow. Each concept is represented
as a hashed bag-of-words vector that is assembled from:

* Canonical names (display name, language-specific names, concept_id)
* Descriptive material (descriptions, notes, preserved content fields)
* Linked text relations (`hasDescription`, `hasNote`, etc.)
* Direct relationships (type/instance links and any other predicates)

While the embeddings are intentionally simple, they provide a deterministic
baseline that honours current access-control rules and avoids network calls.
The FAISS index delivers efficient similarity searches and future iterations
can swap the hashing scheme for a model-backed embedding while keeping the same
outward-facing API.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

try:  # pragma: no cover - exercised via integration paths
    import faiss  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - explicit failure path
    faiss = None  # type: ignore[assignment]
    _FAISS_IMPORT_ERROR = exc
else:
    _FAISS_IMPORT_ERROR = None

from ..db.repositories.concepts_repository import ConceptsRepository
from ..services.text_value_service import get_texts_for_concept
from ..vontology.utils_vontology import (
    get_concept_description,
    get_concept_display_name_with_names_fallback,
    get_concept_notes,
)

logger = logging.getLogger(__name__)


class ConceptSimilarityServiceError(Exception):
    """Raised when similarity computation fails."""


@dataclass
class ConceptSimilarityResult:
    """Container for similarity search outcomes."""

    concept_id: str
    name: str
    score: float


_EMBEDDING_DIM = 512
_TOKEN_PATTERN = re.compile(r"[\w#]+", re.UNICODE)
_RELATED_CONCEPT_CACHE: Dict[str, Tuple[str, Optional[str]]] = {}


def _stable_hash(token: str) -> int:
    digest = hashlib.sha1(token.encode("utf-8")).hexdigest()
    return int(digest, 16)


def _normalise_tokens(text: str) -> List[str]:
    if not text:
        return []
    return [match.lower() for match in _TOKEN_PATTERN.findall(text)]


def _tokens_from_value(value: Any) -> List[str]:
    if isinstance(value, str) and value:
        return _normalise_tokens(value)
    return []


def _tokens_to_embedding(tokens: Sequence[str]) -> np.ndarray:
    vector = np.zeros(_EMBEDDING_DIM, dtype=np.float32)
    for token in tokens:
        idx = _stable_hash(token) % _EMBEDDING_DIM
        vector[idx] += 1.0
    norm = float(np.linalg.norm(vector))
    if norm > 0:
        vector /= norm
    return np.ascontiguousarray(vector, dtype=np.float32)


def _extract_preserved_content(concept: Dict[str, Any]) -> List[str]:
    preserved = concept.get("concept_data", {}).get("preserved_fields", {})
    if not isinstance(preserved, dict):
        return []
    collected: List[str] = []
    for key, value in preserved.items():
        if not isinstance(value, str):
            continue
        if key.lower() in {"description", "notes", "content", "definition"}:
            collected.extend(_normalise_tokens(value))
    return collected


def _resolve_related_concepts(
    concept_ids: Iterable[str],
) -> Dict[str, Tuple[str, Optional[str]]]:
    resolved: Dict[str, Tuple[str, Optional[str]]] = {}
    missing: List[str] = []
    for cid in concept_ids:
        if not isinstance(cid, str):
            continue
        cached = _RELATED_CONCEPT_CACHE.get(cid)
        if cached is not None:
            resolved[cid] = cached
        else:
            missing.append(cid)

    if missing:
        cursor = ConceptsRepository.find(
            {"concept_id": {"$in": missing}},
            projection={"concept_id": 1, "names": 1, "concept_data": 1},
        )
        for doc in cursor:
            cid = doc.get("concept_id")
            if not isinstance(cid, str):
                continue
            name = get_concept_display_name_with_names_fallback(doc)
            description = get_concept_description(doc)
            resolved[cid] = (name, description)
            _RELATED_CONCEPT_CACHE[cid] = (name, description)

    return resolved


def _extract_relationship_tokens(concept: Dict[str, Any]) -> List[str]:
    relationships = concept.get("relationships")
    if not isinstance(relationships, dict):
        return []

    tokens: List[str] = []
    related_ids: List[str] = []

    def _walk(value: Any):
        if isinstance(value, str):
            related_ids.append(value)
            tokens.extend(_normalise_tokens(value))
        elif isinstance(value, list):
            for item in value:
                _walk(item)
        elif isinstance(value, dict):
            for nested in value.values():
                _walk(nested)

    for predicate, value in relationships.items():
        if isinstance(predicate, str):
            tokens.extend(_normalise_tokens(predicate))
        _walk(value)

    if related_ids:
        resolved = _resolve_related_concepts(related_ids)
        for cid, (name, description) in resolved.items():
            if isinstance(name, str) and name:
                tokens.extend(_normalise_tokens(name))
            if isinstance(description, str) and description:
                tokens.extend(_normalise_tokens(description))
            tokens.extend(_normalise_tokens(cid))

    return tokens


def _extract_text_relations(concept_id: Optional[str]) -> List[str]:
    if not concept_id:
        return []
    texts = get_texts_for_concept(concept_id)
    gathered: List[str] = []
    for item in texts:
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            gathered.append(text)
        predicate = item.get("predicate")
        if isinstance(predicate, str):
            gathered.extend(_normalise_tokens(predicate))
    return gathered


def _collect_concept_tokens(concept: Dict[str, Any]) -> List[str]:
    tokens: List[str] = []

    name = get_concept_display_name_with_names_fallback(concept)
    if name:
        tokens.extend(_normalise_tokens(name))

    concept_id = concept.get("concept_id")
    if isinstance(concept_id, str):
        tokens.extend(_normalise_tokens(concept_id))

    names = concept.get("names")
    if isinstance(names, list):
        for entry in names:
            if isinstance(entry, dict):
                entry_name = entry.get("name")
                if isinstance(entry_name, str):
                    tokens.extend(_normalise_tokens(entry_name))

    description = get_concept_description(concept)
    if description:
        tokens.extend(_normalise_tokens(description))

    notes = get_concept_notes(concept)
    if notes:
        tokens.extend(_normalise_tokens(notes))

    tokens.extend(_tokens_from_value(concept.get("content")))
    tokens.extend(_tokens_from_value(concept.get("summary")))
    tokens.extend(_tokens_from_value(concept.get("details")))

    tokens.extend(_tokens_from_value(concept.get("description")))
    tokens.extend(_tokens_from_value(concept.get("notes")))

    concept_data = concept.get("concept_data", {})
    if isinstance(concept_data, dict):
        tokens.extend(_tokens_from_value(concept_data.get("notes")))
        tokens.extend(_tokens_from_value(concept_data.get("description")))
        tokens.extend(_tokens_from_value(concept_data.get("content")))

    attributes = concept.get("attributes", {})
    if isinstance(attributes, dict):
        tokens.extend(_tokens_from_value(attributes.get("description")))
        tokens.extend(_tokens_from_value(attributes.get("notes")))
        for attr_value in attributes.values():
            if isinstance(attr_value, str):
                tokens.extend(_normalise_tokens(attr_value))

    system_tags = concept.get("system_tags")
    if isinstance(system_tags, list):
        for tag in system_tags:
            if isinstance(tag, str):
                tokens.extend(_normalise_tokens(tag))
    elif isinstance(system_tags, str):
        tokens.extend(_normalise_tokens(system_tags))

    user_tags = concept.get("user_tags")
    if isinstance(user_tags, list):
        for tag in user_tags:
            if isinstance(tag, str):
                tokens.extend(_normalise_tokens(tag))
    elif isinstance(user_tags, str):
        tokens.extend(_normalise_tokens(user_tags))

    tokens.extend(_extract_preserved_content(concept))
    tokens.extend(_extract_relationship_tokens(concept))
    tokens.extend(_extract_text_relations(concept_id))

    return tokens


def build_concept_embedding(concept: Dict[str, Any]) -> np.ndarray:
    tokens = _collect_concept_tokens(concept)
    if not tokens:
        return np.zeros(_EMBEDDING_DIM, dtype=np.float32)
    return _tokens_to_embedding(tokens)


def search_similar_concepts(
    query: str, limit: int = 10
) -> List[ConceptSimilarityResult]:
    if not query or not isinstance(query, str):
        return []
    if limit <= 0:
        raise ConceptSimilarityServiceError("limit must be positive")

    concepts_coll = ConceptsRepository.collection()
    if concepts_coll is None:
        raise ConceptSimilarityServiceError("Concepts collection not available")

    if faiss is None:
        raise ConceptSimilarityServiceError(
            "FAISS library is required for similarity search"
        ) from _FAISS_IMPORT_ERROR

    query_tokens = _normalise_tokens(query)
    if not query_tokens:
        return []
    query_vec = _tokens_to_embedding(query_tokens)

    embeddings: List[np.ndarray] = []
    metadata: List[Tuple[str, str]] = []

    projection = {
        "concept_id": 1,
        "names": 1,
        "concept_data": 1,
        "relationships": 1,
        "attributes": 1,
    }
    cursor = ConceptsRepository.find({}, projection=projection)
    for concept in cursor:
        concept_id = concept.get("concept_id")
        if not isinstance(concept_id, str):
            continue
        embedding = build_concept_embedding(concept)
        if not np.any(embedding):
            continue
        embeddings.append(embedding)
        name = get_concept_display_name_with_names_fallback(concept) or concept_id
        metadata.append((concept_id, name))

    if not embeddings:
        return []

    matrix = np.ascontiguousarray(np.vstack(embeddings).astype(np.float32))
    index = faiss.IndexFlatIP(_EMBEDDING_DIM)
    index.add(matrix)

    search_limit = min(limit, len(metadata))
    query_matrix = np.ascontiguousarray(query_vec.reshape(1, -1).astype(np.float32))
    scores, indices = index.search(query_matrix, search_limit)

    results: List[ConceptSimilarityResult] = []
    seen: set[str] = set()
    for idx, score in zip(indices[0], scores[0]):
        if idx < 0:
            continue
        concept_id, name = metadata[idx]
        if concept_id in seen:
            continue
        seen.add(concept_id)
        results.append(
            ConceptSimilarityResult(
                concept_id=concept_id,
                name=name,
                score=float(score),
            )
        )
        if len(results) >= limit:
            break

    return results


__all__ = [
    "ConceptSimilarityResult",
    "ConceptSimilarityServiceError",
    "build_concept_embedding",
    "search_similar_concepts",
]
