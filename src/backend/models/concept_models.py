from pydantic import BaseModel, Field, ConfigDict
from typing import List, Dict, Any, Optional
from datetime import datetime
from enum import Enum


class ConceptKind(str, Enum):
    TYPE = "type"
    INDIVIDUAL = "individual"


class IndexingStatus(str, Enum):
    PENDING = "pending"
    INDEXED = "indexed"
    FAILED = "failed"
    SKIPPED = "skipped"


class UserConceptTrackingModel(BaseModel):
    user_identifier: str
    concept_identifier: str  # For types: Vontology path (e.g., "Concept/Person"). For individuals: _id from concepts collection.
    concept_kind: ConceptKind
    vontology_path: List[str]  # e.g., ["Concept", "Person"]
    concept_name_for_display: str
    last_accessed_timestamp: datetime = Field(default_factory=datetime.utcnow)
    is_key_concept: bool = False
    access_count: int = 0

    model_config = ConfigDict(use_enum_values=True)


class ConceptModel(BaseModel):
    # _id will be handled by MongoDB or an ODM like Beanie
    vontology_path: List[str]  # e.g., ["Concept", "Person", "Researcher"]
    name: str
    notes: str = ""
    attributes: Dict[str, Any] = {}
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    # concept_kind is implicitly "individual" for this model when stored in the 'concepts' collection.
    # If types were stored here, an concept_kind field would be needed.

    model_config = ConfigDict()


class InteractionEntry(BaseModel):
    interaction_type: str  # E.g., "view", "edit_notes", "ask_question"
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    details: Dict[str, Any] = (
        {}
    )  # E.g., {"question": "What is X?", "answer_preview": "Y..."}


class ConceptInteraction(BaseModel):
    # _id will be handled by MongoDB or an ODM
    user_identifier: str
    session_id: str  # To group interactions within a single user session
    concept_identifier: (
        str  # _id from 'concepts' for individuals, Vontology path for types
    )
    concept_vontology_path: List[str]
    concept_kind: ConceptKind
    concept_name_for_display: (
        str  # Denormalized for easier querying of interaction history
    )
    interactions: List[InteractionEntry] = []
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Multi-tenant context (Phase 1: Composite Namespace)
    organisation_concept_id: Optional[str] = (
        None  # e.g., "sail", enables org-scoped content
    )
    role_in_org: Optional[str] = None  # e.g., "admin", "member" (stubbed in Phase 1)
    namespace: Optional[str] = (
        None  # Derived namespace: #V#{user_id}@{org_id} or #V#{user_id}
    )

    # RAG Indexing Status
    indexing_status: IndexingStatus = IndexingStatus.PENDING
    indexed_at: Optional[datetime] = None
    embedding: Optional[List[float]] = None

    model_config = ConfigDict(use_enum_values=True)


# For referencing the collection name in services
interaction_session_collection_name = (
    "interactions"  # Updated to match current db structure
)


# Additional classes for API compatibility
class ConceptData(BaseModel):
    """Data model for concept creation and updates"""

    name: str
    notes: str = ""
    vontology_path: List[str] = []
    attributes: Dict[str, Any] = {}


class ConceptType(BaseModel):
    """Type definition for concepts"""

    name: str
    vontology_path: List[str]
    display_name: str = ""


# REFACTORING_NOTE: The conceptual schemas previously defined as Python dicts (USER_concept_TRACKING_SCHEMA, concept_SCHEMA)
# have been converted into the Pydantic models above (UserconceptTrackingModel, conceptModel).
# The INTERACTION_LOG_SCHEMA_UPDATE_NOTES are reflected in the conceptInteraction and InteractionEntry models.
# These Pydantic models provide data validation, serialization, and clear schema definitions for application use.
