from __future__ import annotations

from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator
from datetime import datetime, timezone
import re

# Module-level BCP-47 regex to avoid Pydantic treating it as a model field
BCP47_RE = re.compile(
    r"^(?:"
    r"(?:[a-z]{2,3}(?:-[a-z]{3}){0,3}|[a-z]{4}|[a-z]{5,8})"  # language
    r"(?:-[a-z]{4})?"  # script
    r"(?:-(?:[a-z]{2}|\d{3}))?"  # region
    r"(?:-(?:[0-9a-z]{5,8}|\d[0-9a-z]{3}))*"  # variants
    r"(?:-x(?:-[0-9a-z]{1,8})+)?"  # private use
    r"|x(?:-[0-9a-z]{1,8})+"  # private use only
    r"|und|mul"  # undefined, multiple
    r")$",
    re.IGNORECASE,
)


class TextValueModel(BaseModel):
    """Represents a language-tagged text value.

    This model stores the raw text and its BCP47 language tag (e.g., 'en', 'en-US').
    The semantic role (name, note, description, interaction, etc.) is captured by
    a separate relation document linking a concept to this text value via a predicate.
    """

    text: str
    lang: str = Field(default="en", description="BCP47 language code")

    # Optional provenance fields for auditability
    provenance: Dict[str, Any] = Field(default_factory=dict)

    # Optional normalized fingerprint for deduplication (e.g., lowercased text + lang)
    fingerprint: Optional[str] = None

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # -- Validators --
    @field_validator("text")
    @classmethod
    def _text_not_empty(cls, v: str) -> str:
        if v is None:
            raise ValueError("text is required")
        v2 = v.strip()
        if not v2:
            raise ValueError("text must not be empty or whitespace")
        return v2

    @field_validator("lang")
    @classmethod
    def _lang_bcp47(cls, v: str) -> str:
        v2 = (v or "").strip()
        if not v2:
            return "en"
        if not BCP47_RE.match(v2):
            raise ValueError(f"lang must be a valid BCP-47 tag, got: {v}")
        return v2


class RelationPredicate:
    """Predicate constants for concept→text relations."""

    HAS_NAME = "hasName"
    HAS_NOTE = "hasNote"
    HAS_DESCRIPTION = "hasDescription"
    HAS_INTERACTION = "hasInteraction"
    HAS_CONTENT = "hasContent"


# Use Literal of the allowed predicate string values


class TextRelationModel(BaseModel):
    """Represents a link between a concept (subject) and a text value (object)."""

    subject_concept_id: str = Field(
        description="The primary concept identifier (e.g., '#V#Person' or a unified concept document's concept_id')."
    )
    predicate: str = Field(
        description=f"The relationship predicate. Common values: {RelationPredicate.HAS_NAME}, {RelationPredicate.HAS_NOTE}, {RelationPredicate.HAS_DESCRIPTION}, {RelationPredicate.HAS_INTERACTION}, {RelationPredicate.HAS_CONTENT}. Custom predicates like '#V#has_email' are also supported."
    )
    object_text_id: str = Field(
        description="The ObjectId of the text value document, as a hex string."
    )

    # Optional extra context
    context: Dict[str, Any] = Field(default_factory=dict)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("predicate")
    @classmethod
    def _predicate_allowed(cls, v: str) -> str:
        v2 = (v or "").strip()
        if not v2:
            raise ValueError("predicate required")
        # Allow registered constants
        allowed = {
            RelationPredicate.HAS_NAME,
            RelationPredicate.HAS_NOTE,
            RelationPredicate.HAS_DESCRIPTION,
            RelationPredicate.HAS_INTERACTION,
            RelationPredicate.HAS_CONTENT,
        }
        if v2 in allowed:
            return v2
        # Permit custom project-scoped predicates beginning with '#V#'
        if v2.startswith("#V#"):
            return v2
        raise ValueError(f"Unsupported predicate: {v2}")


__all__ = [
    "TextValueModel",
    "TextRelationModel",
    "RelationPredicate",
]
