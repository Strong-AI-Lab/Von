import os
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

LEGACY_FIELDS = [
    "name",
    "notes",
    "description",
    "metadata.description",
    "entities",
]
STRICT_ENV_VAR = "CONCEPT_IMPORT_STRICT"
DISABLE_PRESERVED_ENV = "VON_DISABLE_PRESERVED_FIELDS"
STRICT_GUARD_ENV = "VON_CONCEPT_DATA_GUARD_STRICT"


class LegacyFieldUsage(Exception):
    """Raised when strict mode is enabled and legacy fields are detected."""


def _flatten_legacy_presence(concept: Dict[str, Any]) -> List[str]:
    present = []
    if "name" in concept:
        present.append("name")
    if "notes" in concept:
        present.append("notes")
    if "description" in concept:
        present.append("description")
    metadata = concept.get("metadata") or {}
    if isinstance(metadata, dict):
        if "description" in metadata:
            present.append("metadata.description")
    if "entities" in concept:
        present.append("entities")
    return present


def normalize_concept_payload(
    raw: Dict[str, Any], *, strict: bool | None = None, record_warnings: bool = True
) -> Dict[str, Any]:
    """Normalize a single concept-like payload to unified schema.

    Transformations (non-destructive where possible):
      - Top-level legacy `name` -> append to names[] if not already present.
    - (Phase 1) Skip mapping legacy `description` / `metadata.description` / `notes` into preserved_fields (deprecated).
      - `entities` (legacy array wrapper) handled externally (import layer) — flagged here only.

    Args:
        raw: Incoming dict (modified in-place defensively copied first).
        strict: Override environment strict mode.
        record_warnings: Whether to emit logger warnings when applying legacy fallbacks.
    Returns:
        Normalized copy of dict.
    Raises:
        LegacyFieldUsage when strict mode active and any legacy field encountered.
    """
    if raw is None:
        return {}
    concept = dict(raw)  # shallow copy

    if strict is None:
        strict = os.getenv(STRICT_ENV_VAR, "0") in ("1", "true", "True")

    legacy_present = _flatten_legacy_presence(concept)
    if strict and legacy_present:
        raise LegacyFieldUsage(
            f"Legacy fields disallowed in strict mode: {legacy_present}"
        )

    # names[] handling
    if "name" in concept:
        nl_value = concept["name"]
        if nl_value and isinstance(nl_value, str):
            names = concept.get("names") or []
            if not any(
                isinstance(n, dict) and n.get("name") == nl_value for n in names
            ):
                names.append({"name": nl_value, "kind": "NL"})
                concept["names"] = names
        if record_warnings:
            logger.warning("Normalizing legacy field 'name' -> names[] entry")
        # Do not delete original yet (phased removal) — could remove in later phase.

    # Phase 1: preserved_fields deprecated — skip mapping description/notes into preserved_fields.
    # Retain existing preserved_fields if present (read-only legacy support) but do not create/populate.
    # Future phases may migrate these into text_relations explicitly during import.
    # (Intentional no-op for description/notes migration.)

    return concept


def summarize_legacy_usage(concepts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return counts of legacy field presence for a batch (supports metrics/log decisions)."""
    counts: Dict[str, int] = {k: 0 for k in LEGACY_FIELDS}
    for c in concepts:
        if "name" in c:
            counts["name"] += 1
        if "notes" in c:
            counts["notes"] += 1
        if "description" in c:
            counts["description"] += 1
        md = c.get("metadata") or {}
        if isinstance(md, dict):
            if "description" in md:
                counts["metadata.description"] += 1
        if "entities" in c:
            counts["entities"] += 1
    total = len(concepts) or 1
    return {
        "counts": counts,
        "percentages": {k: (v / total) * 100 for k, v in counts.items()},
        "total": total,
    }
