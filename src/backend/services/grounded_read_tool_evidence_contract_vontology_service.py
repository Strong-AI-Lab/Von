"""Vontology KR materialisation for core grounded-read tool evidence contracts.

The concepts and relationships below are migration/bootstrap specifications. Runtime
projection remains generic and resolves the materialised graph from Vontology; this
module must not become a source-specific payload shaper.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import concept_service
from .relationship_write_service import add_relationship
from .tool_evidence_contract_vontology_service import (
    TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID,
    bootstrap_tool_evidence_contract_vocabulary,
    validate_tool_evidence_contract_vocabulary,
)
from .workflow_vontology_materialisation_helpers import (
    load_concept,
    normalise_relationship_targets,
    suspend_event_workflow_integration,
)

GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION = (
    "grounded_read_tool_evidence_contract.v1"
)
GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_SOURCE_TAG = "JVNAUTOSCI-2575"
GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_MANAGED_BY = (
    "grounded_read_tool_evidence_contract_vontology_service"
)

GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_ID = "#V#grounded_read_tool_evidence_contract_v1"

ARXIV_SEARCH_TOOL_ID = "#V#search_arxiv_tool"
ARXIV_PAPER_ENTITY_TYPE_ID = "#V#arxiv_paper_result_entity_type"
ARXIV_FINAL_ANSWER_VIEW_ID = "#V#arxiv_search_final_answer_evidence_view"

RAG_SEARCH_TOOL_ID = "#V#search_knowledge_base_tool"
RAG_RESULT_ENTITY_TYPE_ID = "#V#rag_search_result_entity_type"
RAG_FINAL_ANSWER_VIEW_ID = "#V#rag_search_final_answer_evidence_view"

CONCEPT_EXISTS_TOOL_ID = "#V#concept_exists_tool"
CONCEPT_EXISTENCE_ENTITY_TYPE_ID = "#V#concept_existence_result_entity_type"
CONCEPT_EXISTS_FINAL_ANSWER_VIEW_ID = "#V#concept_exists_final_answer_evidence_view"
FETCH_CONCEPT_TOOL_ID = "#V#fetch_concept_tool"
FETCH_CONCEPT_FINAL_ANSWER_VIEW_ID = "#V#concept_fetch_final_answer_evidence_view"

# Reusable result-status and provenance fields. These are shared because their wire
# semantics are common across internal MCP tools; source-specific fields remain below.
SUCCESS_FIELD_ID = "#V#tool_result_success_field"
ERROR_FIELD_ID = "#V#tool_result_error_field"
ERROR_CODE_FIELD_ID = "#V#tool_result_error_code_field"
ERROR_DETAILS_FIELD_ID = "#V#tool_result_error_details_field"
SUGGESTIONS_FIELD_ID = "#V#tool_result_suggestions_field"
RELATED_CONCEPT_IDS_FIELD_ID = "#V#tool_result_related_concept_ids_field"
QUERY_FIELD_ID = "#V#tool_query_field"
CONCEPT_ID_FIELD_ID = "#V#tool_result_concept_id_field"
CONCEPT_NAME_FIELD_ID = "#V#concept_name_field"
CONCEPT_DESCRIPTION_FIELD_ID = "#V#concept_description_field"
CONCEPT_RELATIONSHIPS_FIELD_ID = "#V#concept_relationships_field"
CONCEPT_RELATIONS_FIELD_ID = "#V#concept_relations_field"

ARXIV_PAPERS_COLLECTION_FIELD_ID = "#V#arxiv_papers_collection_field"
ARXIV_PAPER_ID_FIELD_ID = "#V#arxiv_paper_identifier_field"
ARXIV_TITLE_FIELD_ID = "#V#arxiv_paper_title_field"
ARXIV_URL_FIELD_ID = "#V#arxiv_paper_url_field"
ARXIV_RESOURCE_URI_FIELD_ID = "#V#arxiv_paper_resource_uri_field"
ARXIV_AUTHORS_FIELD_ID = "#V#arxiv_paper_authors_field"
ARXIV_CATEGORIES_FIELD_ID = "#V#arxiv_paper_categories_field"
ARXIV_ABSTRACT_FIELD_ID = "#V#arxiv_paper_abstract_field"
ARXIV_PUBLISHED_FIELD_ID = "#V#arxiv_paper_published_field"
ARXIV_TOTAL_RESULTS_FIELD_ID = "#V#arxiv_total_results_field"

RAG_RESULTS_COLLECTION_FIELD_ID = "#V#rag_results_collection_field"
RAG_RESULT_ID_FIELD_ID = "#V#rag_result_identifier_field"
RAG_SCORE_FIELD_ID = "#V#rag_result_score_field"
RAG_TEXT_FIELD_ID = "#V#rag_result_text_field"
RAG_TITLE_FIELD_ID = "#V#rag_result_title_field"
RAG_SOURCE_SYSTEM_FIELD_ID = "#V#rag_result_source_system_field"
RAG_ITEM_KIND_FIELD_ID = "#V#rag_result_item_kind_field"
RAG_TYPE_FIELD_ID = "#V#rag_result_type_field"
RAG_PREDICATE_FIELD_ID = "#V#rag_result_predicate_field"
RAG_SUBJECT_CONCEPT_ID_FIELD_ID = "#V#rag_result_subject_concept_id_field"
RAG_TARGET_CONCEPT_ID_FIELD_ID = "#V#rag_result_target_concept_id_field"
RAG_COUNT_FIELD_ID = "#V#rag_result_count_field"
RAG_RETRIEVAL_STATE_FIELD_ID = "#V#rag_retrieval_state_field"

CONCEPT_EXISTS_FIELD_ID = "#V#concept_exists_boolean_field"
CONCEPT_ACCESSIBLE_FIELD_ID = "#V#concept_accessible_boolean_field"
ACCESS_CONTROL_ENFORCED_FIELD_ID = "#V#concept_access_control_enforced_field"
ACCESS_RESTRICTION_FAMILIES_FIELD_ID = "#V#concept_access_restriction_families_field"
SPECIFIC_TO_USER_RESTRICTED_FIELD_ID = "#V#concept_specific_to_user_restricted_field"
SPECIFIC_TO_ORG_RESTRICTED_FIELD_ID = "#V#concept_specific_to_org_restricted_field"

SHARED_STATUS_FIELD_IDS: tuple[str, ...] = (
    SUCCESS_FIELD_ID,
    ERROR_FIELD_ID,
    ERROR_CODE_FIELD_ID,
    ERROR_DETAILS_FIELD_ID,
    SUGGESTIONS_FIELD_ID,
    RELATED_CONCEPT_IDS_FIELD_ID,
)

ARXIV_REQUIRED_FINAL_ANSWER_FIELD_IDS: tuple[str, ...] = (
    ARXIV_PAPERS_COLLECTION_FIELD_ID,
    ARXIV_PAPER_ID_FIELD_ID,
    ARXIV_TITLE_FIELD_ID,
    ARXIV_URL_FIELD_ID,
)
RAG_REQUIRED_FINAL_ANSWER_FIELD_IDS: tuple[str, ...] = (
    QUERY_FIELD_ID,
    RAG_RESULTS_COLLECTION_FIELD_ID,
    RAG_RESULT_ID_FIELD_ID,
    RAG_TEXT_FIELD_ID,
    RAG_SOURCE_SYSTEM_FIELD_ID,
    RAG_COUNT_FIELD_ID,
    RAG_RETRIEVAL_STATE_FIELD_ID,
)
CONCEPT_EXISTS_REQUIRED_FINAL_ANSWER_FIELD_IDS: tuple[str, ...] = (
    SUCCESS_FIELD_ID,
    CONCEPT_ID_FIELD_ID,
    CONCEPT_EXISTS_FIELD_ID,
    CONCEPT_ACCESSIBLE_FIELD_ID,
)
FETCH_CONCEPT_REQUIRED_FINAL_ANSWER_FIELD_IDS: tuple[str, ...] = (CONCEPT_ID_FIELD_ID,)


@dataclass(frozen=True)
class GroundedReadConceptSpec:
    concept_id: str
    name: str
    description: str
    parent_concept_ids: tuple[str, ...]
    category: str
    attributes: Mapping[str, Any] | None = None
    create_as_instance: bool = True


@dataclass(frozen=True)
class GroundedReadRelationshipSpec:
    source_id: str
    predicate: str
    target_id: str


def _base_attributes(category: str) -> dict[str, Any]:
    return {
        "repo_seed_source_tag": GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_SOURCE_TAG,
        "repo_seed_managed_by": GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_MANAGED_BY,
        "schema_version": GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION,
        "tool_evidence_contract_id": GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_ID,
        "tool_evidence_contract_vocabulary_id": TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID,
        "tool_contract_category": category,
    }


def _concept(
    *,
    concept_id: str,
    name: str,
    description: str,
    parent_concept_ids: tuple[str, ...],
    category: str,
    attributes: Mapping[str, Any] | None = None,
) -> GroundedReadConceptSpec:
    merged_attributes = _base_attributes(category)
    if attributes:
        merged_attributes.update(dict(attributes))
    return GroundedReadConceptSpec(
        concept_id=concept_id,
        name=name,
        description=description,
        parent_concept_ids=parent_concept_ids,
        category=category,
        attributes=merged_attributes,
    )


def _field(
    concept_id: str,
    name: str,
    description: str,
    *,
    field_key: str,
) -> GroundedReadConceptSpec:
    return _concept(
        concept_id=concept_id,
        name=name,
        description=description,
        parent_concept_ids=("#V#tool_result_field",),
        category="field",
        attributes={"field_key": field_key},
    )


def _safe_slug(value: str) -> str:
    cleaned = value.replace("[]", "_items_")
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", cleaned).strip("_").lower()
    return cleaned or "value"


def _wire_key_id(key: str) -> str:
    return f"#V#grounded_read_wire_key_{_safe_slug(key)}"


def _payload_path_id(path: str) -> str:
    return f"#V#grounded_read_payload_path_{_safe_slug(path)}"


_CORE_CONCEPT_SPECS: tuple[GroundedReadConceptSpec, ...] = (
    _concept(
        concept_id=GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_ID,
        name="Grounded read tool evidence contract v1",
        description=(
            "Contract binding core grounded-read tools to bounded final-answer "
            "evidence views, typed failure evidence, and retrieval provenance."
        ),
        parent_concept_ids=("#V#tool_interface_contract",),
        category="contract",
    ),
    _concept(
        concept_id=ARXIV_SEARCH_TOOL_ID,
        name="search_arxiv tool",
        description="Built-in internal MCP tool that searches arXiv papers.",
        parent_concept_ids=("#V#mcp_tool",),
        category="tool",
        attributes={
            "mcp_tool_name": "search_arxiv",
            "category": "arxiv",
            "dispatch_surface_family": "arxiv",
            "evidence_surface_family": "arxiv",
            "external_surface": True,
            "operation_category": "read",
            "evidence_role": "search",
        },
    ),
    _concept(
        concept_id=RAG_SEARCH_TOOL_ID,
        name="search_knowledge_base tool",
        description=(
            "Built-in internal MCP tool that searches namespace-scoped represented "
            "knowledge through the RAG retrieval surface."
        ),
        parent_concept_ids=("#V#mcp_tool",),
        category="tool",
        attributes={
            "mcp_tool_name": "search_knowledge_base",
            "category": "rag",
            "dispatch_surface_family": "knowledge_base",
            "evidence_surface_family": "knowledge_base",
            "external_surface": False,
            "operation_category": "read",
            "evidence_role": "search",
        },
    ),
    _concept(
        concept_id=CONCEPT_EXISTS_TOOL_ID,
        name="concept_exists tool",
        description=(
            "Built-in internal MCP tool that verifies exact Vontology concept "
            "existence and actor-scoped accessibility."
        ),
        parent_concept_ids=("#V#mcp_tool",),
        category="tool",
        attributes={
            "mcp_tool_name": "concept_exists",
            "category": "vontology",
            "dispatch_surface_family": "knowledge_base",
            "evidence_surface_family": "knowledge_base",
            "external_surface": False,
            "operation_category": "read",
            "evidence_role": "verification",
        },
    ),
    _concept(
        concept_id=FETCH_CONCEPT_TOOL_ID,
        name="fetch_concept tool",
        description=(
            "Built-in internal MCP tool that reads one exact Vontology concept "
            "with bounded names, description, and relation evidence."
        ),
        parent_concept_ids=("#V#mcp_tool",),
        category="tool",
        attributes={
            "mcp_tool_name": "fetch_concept",
            "category": "vontology",
            "dispatch_surface_family": "knowledge_base",
            "evidence_surface_family": "knowledge_base",
            "external_surface": False,
            "operation_category": "read",
            "evidence_role": "verification",
        },
    ),
    _concept(
        concept_id=ARXIV_PAPER_ENTITY_TYPE_ID,
        name="arXiv paper result entity type",
        description="Entity type for paper rows returned by arXiv search.",
        parent_concept_ids=("#V#tool_result_entity_type",),
        category="entity_type",
    ),
    _concept(
        concept_id=RAG_RESULT_ENTITY_TYPE_ID,
        name="RAG search result entity type",
        description="Entity type for bounded evidence rows returned by knowledge-base search.",
        parent_concept_ids=("#V#tool_result_entity_type",),
        category="entity_type",
    ),
    _concept(
        concept_id=CONCEPT_EXISTENCE_ENTITY_TYPE_ID,
        name="Concept existence result entity type",
        description="Entity type for exact concept existence and accessibility evidence.",
        parent_concept_ids=("#V#tool_result_entity_type",),
        category="entity_type",
    ),
    _concept(
        concept_id=ARXIV_FINAL_ANSWER_VIEW_ID,
        name="arXiv search final-answer evidence view",
        description=(
            "Evidence view preserving paper identity, canonical URL, bounded "
            "bibliographic context, and typed search failure evidence."
        ),
        parent_concept_ids=("#V#tool_evidence_view",),
        category="evidence_view",
    ),
    _concept(
        concept_id=RAG_FINAL_ANSWER_VIEW_ID,
        name="RAG search final-answer evidence view",
        description=(
            "Evidence view preserving namespace-scoped retrieved text, represented "
            "provenance, and typed retrieval state for answer synthesis."
        ),
        parent_concept_ids=("#V#tool_evidence_view",),
        category="evidence_view",
    ),
    _concept(
        concept_id=CONCEPT_EXISTS_FINAL_ANSWER_VIEW_ID,
        name="Concept existence final-answer evidence view",
        description=(
            "Evidence view preserving exact existence/accessibility results while "
            "excluding actor identifiers from the projected access diagnostics."
        ),
        parent_concept_ids=("#V#tool_evidence_view",),
        category="evidence_view",
    ),
    _concept(
        concept_id=FETCH_CONCEPT_FINAL_ANSWER_VIEW_ID,
        name="Concept fetch final-answer evidence view",
        description=(
            "Evidence view preserving bounded concept identity, names, description, "
            "structural and expanded relations, and typed not-found evidence."
        ),
        parent_concept_ids=("#V#tool_evidence_view",),
        category="evidence_view",
    ),
)


_FIELD_CONCEPT_SPECS: tuple[GroundedReadConceptSpec, ...] = (
    _field(
        SUCCESS_FIELD_ID,
        "Tool result success field",
        "Whether the tool call succeeded.",
        field_key="success",
    ),
    _field(
        ERROR_FIELD_ID,
        "Tool result error field",
        "Human-readable bounded tool failure evidence.",
        field_key="error",
    ),
    _field(
        ERROR_CODE_FIELD_ID,
        "Tool result error-code field",
        "Stable machine-readable tool failure code.",
        field_key="error_code",
    ),
    _field(
        ERROR_DETAILS_FIELD_ID,
        "Tool result error-details field",
        "Structured bounded diagnostics for a tool failure.",
        field_key="error_details",
    ),
    _field(
        SUGGESTIONS_FIELD_ID,
        "Tool result suggestions field",
        "Bounded recovery suggestions returned by a tool.",
        field_key="suggestions",
    ),
    _field(
        RELATED_CONCEPT_IDS_FIELD_ID,
        "Tool result related-concept IDs field",
        "Represented concept identifiers related to a tool outcome.",
        field_key="related_concept_ids",
    ),
    _field(
        QUERY_FIELD_ID,
        "Tool query field",
        "Query used for a search tool invocation.",
        field_key="query",
    ),
    _field(
        CONCEPT_ID_FIELD_ID,
        "Tool result concept-ID field",
        "Exact Vontology concept identifier carried by a result.",
        field_key="concept_id",
    ),
    _field(
        CONCEPT_NAME_FIELD_ID,
        "Concept name field",
        "Direct human-readable name carried by a concept read.",
        field_key="name",
    ),
    _field(
        CONCEPT_DESCRIPTION_FIELD_ID,
        "Concept description field",
        "Represented description carried by a concept read.",
        field_key="description",
    ),
    _field(
        CONCEPT_RELATIONSHIPS_FIELD_ID,
        "Concept relationships field",
        "Structural predicate-to-target relationships carried by a concept read.",
        field_key="relationships",
    ),
    _field(
        CONCEPT_RELATIONS_FIELD_ID,
        "Concept expanded relations field",
        "Optional bounded relation readback requested for a concept.",
        field_key="relations",
    ),
    _field(
        ARXIV_PAPERS_COLLECTION_FIELD_ID,
        "arXiv papers collection field",
        "Collection of paper rows returned by arXiv search.",
        field_key="papers",
    ),
    _field(
        ARXIV_PAPER_ID_FIELD_ID,
        "arXiv paper identifier field",
        "Stable arXiv identifier for one returned paper.",
        field_key="id",
    ),
    _field(
        ARXIV_TITLE_FIELD_ID,
        "arXiv paper title field",
        "Exact title of one returned paper.",
        field_key="title",
    ),
    _field(
        ARXIV_URL_FIELD_ID,
        "arXiv paper URL field",
        "Canonical arXiv abstract URL for one returned paper.",
        field_key="url",
    ),
    _field(
        ARXIV_RESOURCE_URI_FIELD_ID,
        "arXiv paper resource-URI field",
        "Provider resource URI for one returned paper.",
        field_key="resource_uri",
    ),
    _field(
        ARXIV_AUTHORS_FIELD_ID,
        "arXiv paper authors field",
        "Authors of one returned paper.",
        field_key="authors",
    ),
    _field(
        ARXIV_CATEGORIES_FIELD_ID,
        "arXiv paper categories field",
        "arXiv categories for one returned paper.",
        field_key="categories",
    ),
    _field(
        ARXIV_ABSTRACT_FIELD_ID,
        "arXiv paper abstract field",
        "Abstract or summary returned for one paper.",
        field_key="abstract",
    ),
    _field(
        ARXIV_PUBLISHED_FIELD_ID,
        "arXiv paper published field",
        "Publication timestamp returned for one paper.",
        field_key="published",
    ),
    _field(
        ARXIV_TOTAL_RESULTS_FIELD_ID,
        "arXiv total-results field",
        "Total paper-result count reported by arXiv search.",
        field_key="total_results",
    ),
    _field(
        RAG_RESULTS_COLLECTION_FIELD_ID,
        "RAG results collection field",
        "Collection of evidence rows returned by knowledge-base search.",
        field_key="results",
    ),
    _field(
        RAG_RESULT_ID_FIELD_ID,
        "RAG result identifier field",
        "Stable chunk or evidence-row identifier.",
        field_key="id",
    ),
    _field(
        RAG_SCORE_FIELD_ID,
        "RAG result score field",
        "Backend relevance score for a retrieved row.",
        field_key="score",
    ),
    _field(
        RAG_TEXT_FIELD_ID,
        "RAG result text field",
        "Retrieved text evidence used for answer synthesis.",
        field_key="text",
    ),
    _field(
        RAG_TITLE_FIELD_ID,
        "RAG result title field",
        "Human-readable title resolved from a result or its metadata.",
        field_key="title",
    ),
    _field(
        RAG_SOURCE_SYSTEM_FIELD_ID,
        "RAG result source-system field",
        "Source-system provenance for a retrieved row.",
        field_key="source_system",
    ),
    _field(
        RAG_ITEM_KIND_FIELD_ID,
        "RAG result item-kind field",
        "Represented item-kind provenance for a retrieved row.",
        field_key="item_kind",
    ),
    _field(
        RAG_TYPE_FIELD_ID,
        "RAG result type field",
        "Represented source type for a retrieved row.",
        field_key="type",
    ),
    _field(
        RAG_PREDICATE_FIELD_ID,
        "RAG result predicate field",
        "Predicate provenance for a retrieved text relation.",
        field_key="predicate",
    ),
    _field(
        RAG_SUBJECT_CONCEPT_ID_FIELD_ID,
        "RAG result subject-concept-ID field",
        "Subject concept identifier carried in row provenance.",
        field_key="subject_concept_id",
    ),
    _field(
        RAG_TARGET_CONCEPT_ID_FIELD_ID,
        "RAG result target-concept-ID field",
        "Target concept identifier carried in row provenance.",
        field_key="target_concept_id",
    ),
    _field(
        RAG_COUNT_FIELD_ID,
        "RAG result count field",
        "Count of knowledge-base result rows.",
        field_key="count",
    ),
    _field(
        RAG_RETRIEVAL_STATE_FIELD_ID,
        "RAG retrieval-state field",
        "Typed rag_retrieval_state.v1 facts for the exact retrieval attempt.",
        field_key="retrieval_state",
    ),
    _field(
        CONCEPT_EXISTS_FIELD_ID,
        "Concept exists boolean field",
        "Whether the exact concept exists.",
        field_key="exists",
    ),
    _field(
        CONCEPT_ACCESSIBLE_FIELD_ID,
        "Concept accessible boolean field",
        "Whether the exact concept is accessible to the current actor.",
        field_key="accessible",
    ),
    _field(
        ACCESS_CONTROL_ENFORCED_FIELD_ID,
        "Concept access-control-enforced field",
        "Whether actor-scoped access control was enforced for the lookup.",
        field_key="access_control_enforced",
    ),
    _field(
        ACCESS_RESTRICTION_FAMILIES_FIELD_ID,
        "Concept access restriction-families field",
        "Visibility restriction families present on the concept, without actor identifiers.",
        field_key="restriction_families_present",
    ),
    _field(
        SPECIFIC_TO_USER_RESTRICTED_FIELD_ID,
        "Concept user-restricted field",
        "Whether user-specific visibility restricts the concept.",
        field_key="specific_to_user_restricted",
    ),
    _field(
        SPECIFIC_TO_ORG_RESTRICTED_FIELD_ID,
        "Concept organisation-restricted field",
        "Whether organisation-specific visibility restricts the concept.",
        field_key="specific_to_org_restricted",
    ),
)


_FIELD_ALIASES: Mapping[str, tuple[str, ...]] = {
    SUCCESS_FIELD_ID: ("success",),
    ERROR_FIELD_ID: ("error",),
    ERROR_CODE_FIELD_ID: ("error_code",),
    ERROR_DETAILS_FIELD_ID: ("error_details",),
    SUGGESTIONS_FIELD_ID: ("suggestions",),
    RELATED_CONCEPT_IDS_FIELD_ID: ("related_concept_ids",),
    QUERY_FIELD_ID: ("query",),
    CONCEPT_ID_FIELD_ID: ("concept_id",),
    CONCEPT_NAME_FIELD_ID: ("name", "direct_concept_name"),
    CONCEPT_DESCRIPTION_FIELD_ID: ("description",),
    CONCEPT_RELATIONSHIPS_FIELD_ID: ("relationships",),
    CONCEPT_RELATIONS_FIELD_ID: ("relations",),
    ARXIV_PAPERS_COLLECTION_FIELD_ID: ("papers", "results"),
    ARXIV_PAPER_ID_FIELD_ID: ("id", "arxiv_id"),
    ARXIV_TITLE_FIELD_ID: ("title",),
    ARXIV_URL_FIELD_ID: ("url", "canonical_url", "entry_url", "abs_url"),
    ARXIV_RESOURCE_URI_FIELD_ID: ("resource_uri",),
    ARXIV_AUTHORS_FIELD_ID: ("authors",),
    ARXIV_CATEGORIES_FIELD_ID: ("categories",),
    ARXIV_ABSTRACT_FIELD_ID: ("abstract", "summary"),
    ARXIV_PUBLISHED_FIELD_ID: ("published",),
    ARXIV_TOTAL_RESULTS_FIELD_ID: ("total_results", "total"),
    RAG_RESULTS_COLLECTION_FIELD_ID: ("results", "items"),
    RAG_RESULT_ID_FIELD_ID: ("id",),
    RAG_SCORE_FIELD_ID: ("score",),
    RAG_TEXT_FIELD_ID: ("text",),
    RAG_TITLE_FIELD_ID: ("title",),
    RAG_SOURCE_SYSTEM_FIELD_ID: ("source_system",),
    RAG_ITEM_KIND_FIELD_ID: ("item_kind",),
    RAG_TYPE_FIELD_ID: ("type",),
    RAG_PREDICATE_FIELD_ID: ("predicate",),
    RAG_SUBJECT_CONCEPT_ID_FIELD_ID: ("subject_concept_id",),
    RAG_TARGET_CONCEPT_ID_FIELD_ID: ("target_concept_id",),
    RAG_COUNT_FIELD_ID: ("count",),
    RAG_RETRIEVAL_STATE_FIELD_ID: ("retrieval_state",),
    CONCEPT_EXISTS_FIELD_ID: ("exists",),
    CONCEPT_ACCESSIBLE_FIELD_ID: ("accessible",),
    ACCESS_CONTROL_ENFORCED_FIELD_ID: ("access_control_enforced",),
    ACCESS_RESTRICTION_FAMILIES_FIELD_ID: ("restriction_families_present",),
    SPECIFIC_TO_USER_RESTRICTED_FIELD_ID: ("specific_to_user_restricted",),
    SPECIFIC_TO_ORG_RESTRICTED_FIELD_ID: ("specific_to_org_restricted",),
}


def _collection_paths(
    collection_names: Sequence[str], *row_paths: str
) -> tuple[str, ...]:
    return tuple(
        f"{collection_name}[].{row_path}"
        for collection_name in collection_names
        for row_path in row_paths
    )


_FIELD_PAYLOAD_PATHS: Mapping[str, tuple[str, ...]] = {
    CONCEPT_NAME_FIELD_ID: ("direct_concept_name", "name", "names[].name"),
    CONCEPT_DESCRIPTION_FIELD_ID: (
        "description",
        "concept_data.preserved_fields.description",
        "concept_data.description",
    ),
    ARXIV_PAPERS_COLLECTION_FIELD_ID: ("papers", "results"),
    ARXIV_PAPER_ID_FIELD_ID: _collection_paths(("papers", "results"), "id", "arxiv_id"),
    ARXIV_TITLE_FIELD_ID: _collection_paths(("papers", "results"), "title"),
    ARXIV_URL_FIELD_ID: _collection_paths(
        ("papers", "results"), "url", "canonical_url", "entry_url", "abs_url"
    ),
    ARXIV_RESOURCE_URI_FIELD_ID: _collection_paths(
        ("papers", "results"), "resource_uri"
    ),
    ARXIV_AUTHORS_FIELD_ID: _collection_paths(("papers", "results"), "authors"),
    ARXIV_CATEGORIES_FIELD_ID: _collection_paths(("papers", "results"), "categories"),
    ARXIV_ABSTRACT_FIELD_ID: _collection_paths(
        ("papers", "results"), "abstract", "summary"
    ),
    ARXIV_PUBLISHED_FIELD_ID: _collection_paths(("papers", "results"), "published"),
    RAG_RESULTS_COLLECTION_FIELD_ID: ("results", "items"),
    RAG_RESULT_ID_FIELD_ID: _collection_paths(("results", "items"), "id"),
    RAG_SCORE_FIELD_ID: _collection_paths(("results", "items"), "score"),
    RAG_TEXT_FIELD_ID: _collection_paths(("results", "items"), "text"),
    RAG_TITLE_FIELD_ID: _collection_paths(
        ("results", "items"),
        "title",
        "metadata.title",
        "metadata.paper_title",
        "metadata.document_title",
        "metadata.source_title",
        "metadata.name",
    ),
    RAG_SOURCE_SYSTEM_FIELD_ID: _collection_paths(
        ("results", "items"), "metadata.source_system"
    ),
    RAG_ITEM_KIND_FIELD_ID: _collection_paths(
        ("results", "items"), "metadata.item_kind"
    ),
    RAG_TYPE_FIELD_ID: _collection_paths(("results", "items"), "metadata.type"),
    RAG_PREDICATE_FIELD_ID: _collection_paths(
        ("results", "items"), "metadata.predicate"
    ),
    CONCEPT_ID_FIELD_ID: _collection_paths(("results", "items"), "metadata.concept_id"),
    RAG_SUBJECT_CONCEPT_ID_FIELD_ID: _collection_paths(
        ("results", "items"), "metadata.subject_concept_id"
    ),
    RAG_TARGET_CONCEPT_ID_FIELD_ID: _collection_paths(
        ("results", "items"), "metadata.target_concept_id"
    ),
    ACCESS_CONTROL_ENFORCED_FIELD_ID: ("access.access_control_enforced",),
    ACCESS_RESTRICTION_FAMILIES_FIELD_ID: ("access.restriction_families_present",),
    SPECIFIC_TO_USER_RESTRICTED_FIELD_ID: ("access.specific_to_user_restricted",),
    SPECIFIC_TO_ORG_RESTRICTED_FIELD_ID: ("access.specific_to_org_restricted",),
}


def _wire_key_spec(key: str) -> GroundedReadConceptSpec:
    return _concept(
        concept_id=_wire_key_id(key),
        name=f"Grounded-read wire key {key}",
        description=f"Wire key named {key} in a grounded-read MCP payload.",
        parent_concept_ids=("#V#tool_wire_key",),
        category="wire_key",
        attributes={"wire_key": key},
    )


def _payload_path_spec(path: str) -> GroundedReadConceptSpec:
    return _concept(
        concept_id=_payload_path_id(path),
        name=f"Grounded-read payload path {path}",
        description=f"Payload path {path} in a grounded-read MCP result.",
        parent_concept_ids=("#V#tool_payload_path",),
        category="payload_path",
        attributes={"payload_path": path},
    )


_WIRE_KEY_CONCEPT_SPECS: tuple[GroundedReadConceptSpec, ...] = tuple(
    _wire_key_spec(key)
    for key in dict.fromkeys(
        alias for aliases in _FIELD_ALIASES.values() for alias in aliases
    )
)
_PAYLOAD_PATH_CONCEPT_SPECS: tuple[GroundedReadConceptSpec, ...] = tuple(
    _payload_path_spec(path)
    for path in dict.fromkeys(
        path for paths in _FIELD_PAYLOAD_PATHS.values() for path in paths
    )
)

GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS: tuple[
    GroundedReadConceptSpec, ...
] = (
    *_CORE_CONCEPT_SPECS,
    *_FIELD_CONCEPT_SPECS,
    *_WIRE_KEY_CONCEPT_SPECS,
    *_PAYLOAD_PATH_CONCEPT_SPECS,
)


ARXIV_OUTPUT_FIELD_IDS: tuple[str, ...] = (
    QUERY_FIELD_ID,
    ARXIV_PAPERS_COLLECTION_FIELD_ID,
    ARXIV_PAPER_ID_FIELD_ID,
    ARXIV_TITLE_FIELD_ID,
    ARXIV_URL_FIELD_ID,
    ARXIV_RESOURCE_URI_FIELD_ID,
    ARXIV_AUTHORS_FIELD_ID,
    ARXIV_CATEGORIES_FIELD_ID,
    ARXIV_ABSTRACT_FIELD_ID,
    ARXIV_PUBLISHED_FIELD_ID,
    ARXIV_TOTAL_RESULTS_FIELD_ID,
    *SHARED_STATUS_FIELD_IDS,
)
RAG_OUTPUT_FIELD_IDS: tuple[str, ...] = (
    QUERY_FIELD_ID,
    RAG_RESULTS_COLLECTION_FIELD_ID,
    RAG_RESULT_ID_FIELD_ID,
    RAG_SCORE_FIELD_ID,
    RAG_TEXT_FIELD_ID,
    RAG_TITLE_FIELD_ID,
    RAG_SOURCE_SYSTEM_FIELD_ID,
    RAG_ITEM_KIND_FIELD_ID,
    RAG_TYPE_FIELD_ID,
    RAG_PREDICATE_FIELD_ID,
    CONCEPT_ID_FIELD_ID,
    RAG_SUBJECT_CONCEPT_ID_FIELD_ID,
    RAG_TARGET_CONCEPT_ID_FIELD_ID,
    RAG_COUNT_FIELD_ID,
    RAG_RETRIEVAL_STATE_FIELD_ID,
    *SHARED_STATUS_FIELD_IDS,
)
CONCEPT_EXISTS_OUTPUT_FIELD_IDS: tuple[str, ...] = (
    SUCCESS_FIELD_ID,
    CONCEPT_ID_FIELD_ID,
    CONCEPT_EXISTS_FIELD_ID,
    CONCEPT_ACCESSIBLE_FIELD_ID,
    ACCESS_CONTROL_ENFORCED_FIELD_ID,
    ACCESS_RESTRICTION_FAMILIES_FIELD_ID,
    SPECIFIC_TO_USER_RESTRICTED_FIELD_ID,
    SPECIFIC_TO_ORG_RESTRICTED_FIELD_ID,
    ERROR_FIELD_ID,
    ERROR_CODE_FIELD_ID,
    ERROR_DETAILS_FIELD_ID,
    SUGGESTIONS_FIELD_ID,
    RELATED_CONCEPT_IDS_FIELD_ID,
)
FETCH_CONCEPT_OUTPUT_FIELD_IDS: tuple[str, ...] = (
    CONCEPT_ID_FIELD_ID,
    CONCEPT_NAME_FIELD_ID,
    CONCEPT_DESCRIPTION_FIELD_ID,
    CONCEPT_RELATIONSHIPS_FIELD_ID,
    CONCEPT_RELATIONS_FIELD_ID,
    *SHARED_STATUS_FIELD_IDS,
)


def _relationship(
    source_id: str, predicate: str, target_id: str
) -> GroundedReadRelationshipSpec:
    return GroundedReadRelationshipSpec(source_id, predicate, target_id)


def _contract_relationships() -> tuple[GroundedReadRelationshipSpec, ...]:
    relationships = [
        *(
            _relationship(
                GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_ID,
                "#V#tool_contract_applies_to_tool",
                tool_id,
            )
            for tool_id in (
                ARXIV_SEARCH_TOOL_ID,
                RAG_SEARCH_TOOL_ID,
                CONCEPT_EXISTS_TOOL_ID,
                FETCH_CONCEPT_TOOL_ID,
            )
        ),
        *(
            _relationship(
                GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_ID,
                "#V#tool_contract_has_evidence_view",
                view_id,
            )
            for view_id in (
                ARXIV_FINAL_ANSWER_VIEW_ID,
                RAG_FINAL_ANSWER_VIEW_ID,
                CONCEPT_EXISTS_FINAL_ANSWER_VIEW_ID,
                FETCH_CONCEPT_FINAL_ANSWER_VIEW_ID,
            )
        ),
    ]
    relationships.extend(
        _relationship(
            GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_ID,
            "#V#vocabulary_includes_concept",
            concept_id,
        )
        for concept_id in canonical_grounded_read_tool_evidence_contract_concept_ids()
        if concept_id != GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_ID
    )
    return tuple(relationships)


def _tool_field_relationships() -> tuple[GroundedReadRelationshipSpec, ...]:
    fields_by_tool = {
        ARXIV_SEARCH_TOOL_ID: ARXIV_OUTPUT_FIELD_IDS,
        RAG_SEARCH_TOOL_ID: RAG_OUTPUT_FIELD_IDS,
        CONCEPT_EXISTS_TOOL_ID: CONCEPT_EXISTS_OUTPUT_FIELD_IDS,
        FETCH_CONCEPT_TOOL_ID: FETCH_CONCEPT_OUTPUT_FIELD_IDS,
    }
    relationships = [
        _relationship(tool_id, "#V#tool_has_output_field", field_id)
        for tool_id, field_ids in fields_by_tool.items()
        for field_id in field_ids
    ]
    relationships.extend(
        (
            _relationship(
                ARXIV_SEARCH_TOOL_ID,
                "#V#tool_emits_collection_entity_type",
                ARXIV_PAPER_ENTITY_TYPE_ID,
            ),
            _relationship(
                RAG_SEARCH_TOOL_ID,
                "#V#tool_emits_collection_entity_type",
                RAG_RESULT_ENTITY_TYPE_ID,
            ),
            _relationship(
                CONCEPT_EXISTS_TOOL_ID,
                "#V#tool_emits_entity_type",
                CONCEPT_EXISTENCE_ENTITY_TYPE_ID,
            ),
            _relationship(
                FETCH_CONCEPT_TOOL_ID,
                "#V#tool_emits_entity_type",
                CONCEPT_EXISTENCE_ENTITY_TYPE_ID,
            ),
        )
    )
    return tuple(relationships)


def _entity_relationships() -> tuple[GroundedReadRelationshipSpec, ...]:
    fields_by_entity = {
        ARXIV_PAPER_ENTITY_TYPE_ID: tuple(
            field_id
            for field_id in ARXIV_OUTPUT_FIELD_IDS
            if field_id not in SHARED_STATUS_FIELD_IDS
            and field_id
            not in {
                QUERY_FIELD_ID,
                ARXIV_PAPERS_COLLECTION_FIELD_ID,
                ARXIV_TOTAL_RESULTS_FIELD_ID,
            }
        ),
        RAG_RESULT_ENTITY_TYPE_ID: tuple(
            field_id
            for field_id in RAG_OUTPUT_FIELD_IDS
            if field_id not in SHARED_STATUS_FIELD_IDS
            and field_id
            not in {
                QUERY_FIELD_ID,
                RAG_RESULTS_COLLECTION_FIELD_ID,
                RAG_COUNT_FIELD_ID,
                RAG_RETRIEVAL_STATE_FIELD_ID,
            }
        ),
        CONCEPT_EXISTENCE_ENTITY_TYPE_ID: tuple(
            dict.fromkeys(
                field_id
                for field_id in (
                    *CONCEPT_EXISTS_OUTPUT_FIELD_IDS,
                    *FETCH_CONCEPT_OUTPUT_FIELD_IDS,
                )
                if field_id not in SHARED_STATUS_FIELD_IDS
            )
        ),
    }
    return tuple(
        _relationship(entity_id, "#V#entity_type_has_tool_field", field_id)
        for entity_id, field_ids in fields_by_entity.items()
        for field_id in field_ids
    )


def _field_alias_relationships() -> tuple[GroundedReadRelationshipSpec, ...]:
    return tuple(
        _relationship(field_id, "#V#field_has_wire_alias", _wire_key_id(alias))
        for field_id, aliases in _FIELD_ALIASES.items()
        for alias in aliases
    )


def _field_payload_path_relationships() -> tuple[GroundedReadRelationshipSpec, ...]:
    return tuple(
        _relationship(
            field_id,
            "#V#field_extracts_from_payload_path",
            _payload_path_id(path),
        )
        for field_id, paths in _FIELD_PAYLOAD_PATHS.items()
        for path in paths
    )


def _field_role_relationships() -> tuple[GroundedReadRelationshipSpec, ...]:
    relationships = [
        *(
            _relationship(
                field_id,
                "#V#field_has_role",
                "#V#tool_field_role_collection_membership",
            )
            for field_id in (
                ARXIV_PAPERS_COLLECTION_FIELD_ID,
                RAG_RESULTS_COLLECTION_FIELD_ID,
            )
        ),
        *(
            _relationship(
                field_id,
                "#V#field_has_role",
                "#V#tool_field_role_entity_identifier",
            )
            for field_id in (
                ARXIV_PAPER_ID_FIELD_ID,
                RAG_RESULT_ID_FIELD_ID,
                CONCEPT_ID_FIELD_ID,
            )
        ),
        *(
            _relationship(
                field_id,
                "#V#field_has_role",
                "#V#tool_field_role_display_label",
            )
            for field_id in (ARXIV_TITLE_FIELD_ID, RAG_TITLE_FIELD_ID)
        ),
    ]
    answer_fields = tuple(
        dict.fromkeys(
            (
                *ARXIV_OUTPUT_FIELD_IDS,
                *RAG_OUTPUT_FIELD_IDS,
                *CONCEPT_EXISTS_OUTPUT_FIELD_IDS,
                *FETCH_CONCEPT_OUTPUT_FIELD_IDS,
            )
        )
    )
    relationships.extend(
        _relationship(
            field_id, "#V#field_has_role", "#V#tool_field_role_answer_evidence"
        )
        for field_id in answer_fields
    )
    return tuple(relationships)


def _field_policy_relationships() -> tuple[GroundedReadRelationshipSpec, ...]:
    required_fields = tuple(
        dict.fromkeys(
            (
                *ARXIV_REQUIRED_FINAL_ANSWER_FIELD_IDS,
                *RAG_REQUIRED_FINAL_ANSWER_FIELD_IDS,
                *CONCEPT_EXISTS_REQUIRED_FINAL_ANSWER_FIELD_IDS,
                *FETCH_CONCEPT_REQUIRED_FINAL_ANSWER_FIELD_IDS,
            )
        )
    )
    return tuple(
        _relationship(
            field_id,
            "#V#field_has_completion_policy",
            "#V#tool_field_completion_required_before_answer",
        )
        for field_id in required_fields
    )


def _evidence_view_relationships() -> tuple[GroundedReadRelationshipSpec, ...]:
    view_specs = (
        (
            ARXIV_FINAL_ANSWER_VIEW_ID,
            ARXIV_SEARCH_TOOL_ID,
            ARXIV_PAPER_ENTITY_TYPE_ID,
            ARXIV_REQUIRED_FINAL_ANSWER_FIELD_IDS,
            ARXIV_OUTPUT_FIELD_IDS,
        ),
        (
            RAG_FINAL_ANSWER_VIEW_ID,
            RAG_SEARCH_TOOL_ID,
            RAG_RESULT_ENTITY_TYPE_ID,
            RAG_REQUIRED_FINAL_ANSWER_FIELD_IDS,
            RAG_OUTPUT_FIELD_IDS,
        ),
        (
            CONCEPT_EXISTS_FINAL_ANSWER_VIEW_ID,
            CONCEPT_EXISTS_TOOL_ID,
            CONCEPT_EXISTENCE_ENTITY_TYPE_ID,
            CONCEPT_EXISTS_REQUIRED_FINAL_ANSWER_FIELD_IDS,
            CONCEPT_EXISTS_OUTPUT_FIELD_IDS,
        ),
        (
            FETCH_CONCEPT_FINAL_ANSWER_VIEW_ID,
            FETCH_CONCEPT_TOOL_ID,
            CONCEPT_EXISTENCE_ENTITY_TYPE_ID,
            FETCH_CONCEPT_REQUIRED_FINAL_ANSWER_FIELD_IDS,
            FETCH_CONCEPT_OUTPUT_FIELD_IDS,
        ),
    )
    relationships: list[GroundedReadRelationshipSpec] = []
    for view_id, tool_id, entity_id, required_fields, included_fields in view_specs:
        relationships.extend(
            (
                _relationship(view_id, "#V#evidence_view_applies_to_tool", tool_id),
                _relationship(
                    view_id, "#V#evidence_view_applies_to_entity_type", entity_id
                ),
                _relationship(
                    view_id,
                    "#V#evidence_view_has_purpose",
                    "#V#tool_evidence_view_purpose_final_answer",
                ),
            )
        )
        relationships.extend(
            _relationship(view_id, "#V#evidence_view_requires_field", field_id)
            for field_id in required_fields
        )
        relationships.extend(
            _relationship(view_id, "#V#evidence_view_includes_field", field_id)
            for field_id in included_fields
        )
    return tuple(relationships)


def _grounded_read_relationship_specs() -> tuple[GroundedReadRelationshipSpec, ...]:
    return (
        *_contract_relationships(),
        *_entity_relationships(),
        *_tool_field_relationships(),
        *_field_alias_relationships(),
        *_field_payload_path_relationships(),
        *_field_role_relationships(),
        *_field_policy_relationships(),
        *_evidence_view_relationships(),
    )


def canonical_grounded_read_tool_evidence_contract_concept_ids() -> tuple[str, ...]:
    return tuple(
        spec.concept_id for spec in GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS
    )


def canonical_grounded_read_tool_evidence_contract_relationships() -> tuple[
    GroundedReadRelationshipSpec, ...
]:
    return _grounded_read_relationship_specs()


def _normalise_targets(value: Any) -> list[str]:
    return normalise_relationship_targets(value)


def _ensure_structural_targets(
    *, concept_id: str, relationship_key: str, target_ids: Sequence[str]
) -> bool:
    concept_doc = load_concept(concept_id)
    if not isinstance(concept_doc, Mapping):
        return False
    relationships = dict(concept_doc.get("relationships") or {})
    existing_targets = _normalise_targets(relationships.get(relationship_key))
    updated_targets = list(existing_targets)
    for target_id in target_ids:
        cleaned = str(target_id or "").strip()
        if cleaned and cleaned not in updated_targets:
            updated_targets.append(cleaned)
    if updated_targets == existing_targets:
        return False
    concept_service.update_concept(
        concept_id, {f"relationships.{relationship_key}": updated_targets}
    )
    return True


def _ensure_concept_attributes(spec: GroundedReadConceptSpec) -> bool:
    expected_attributes = dict(spec.attributes or {})
    if not expected_attributes:
        return False
    concept_doc = load_concept(spec.concept_id)
    if not isinstance(concept_doc, Mapping):
        return False
    raw_attributes = concept_doc.get("attributes")
    existing_attributes: Mapping[str, Any] = (
        raw_attributes if isinstance(raw_attributes, Mapping) else {}
    )
    updates = {
        f"attributes.{key}": expected_value
        for key, expected_value in expected_attributes.items()
        if existing_attributes.get(key) != expected_value
    }
    if not updates:
        return False
    concept_service.update_concept(spec.concept_id, updates)
    return True


def _ensure_concept(spec: GroundedReadConceptSpec) -> str:
    concept_doc = load_concept(spec.concept_id)
    if not isinstance(concept_doc, Mapping):
        concept_service.create_concept(
            name=spec.name,
            concept_id=spec.concept_id,
            description=spec.description,
            parent_concept_ids=list(spec.parent_concept_ids),
            create_as_instance=spec.create_as_instance,
            attributes=dict(spec.attributes or {}),
            system_tags=[
                "grounded_read_tool_evidence_contract",
                GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_SOURCE_TAG,
            ],
            visibility_scope_mode="global_general",
        )
        return "created"
    relationship_key = (
        "is_an_instance_of" if spec.create_as_instance else "is_a_type_of"
    )
    repaired = _ensure_structural_targets(
        concept_id=spec.concept_id,
        relationship_key=relationship_key,
        target_ids=spec.parent_concept_ids,
    )
    repaired = _ensure_concept_attributes(spec) or repaired
    return "repaired" if repaired else "existing"


def _ensure_relationship(spec: GroundedReadRelationshipSpec) -> dict[str, Any]:
    result = add_relationship(
        source_id=spec.source_id, predicate=spec.predicate, target=spec.target_id
    )
    return dict(result) if isinstance(result, Mapping) else {"success": False}


def _relationship_identity(spec: GroundedReadRelationshipSpec) -> str:
    return f"{spec.source_id}:{spec.predicate}:{spec.target_id}"


def bootstrap_grounded_read_tool_evidence_contract() -> dict[str, Any]:
    vocabulary_report = bootstrap_tool_evidence_contract_vocabulary()
    concept_status_by_id: dict[str, str] = {}
    relationship_ids: list[str] = []
    errors: list[dict[str, Any]] = []
    if not vocabulary_report.get("success"):
        errors.append(
            {
                "section": "generic_vocabulary",
                "reason_code": "tool_evidence_contract_vocabulary_invalid",
                "details": vocabulary_report,
            }
        )

    with suspend_event_workflow_integration():
        for spec in GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS:
            try:
                concept_status_by_id[spec.concept_id] = _ensure_concept(spec)
            except Exception as exc:
                errors.append(
                    {
                        "section": "concepts",
                        "concept_id": spec.concept_id,
                        "reason_code": str(exc),
                    }
                )
        for relationship_spec in _grounded_read_relationship_specs():
            try:
                result = _ensure_relationship(relationship_spec)
            except Exception as exc:
                errors.append(
                    {
                        "section": "relationships",
                        "relationship": _relationship_identity(relationship_spec),
                        "reason_code": str(exc),
                    }
                )
                continue
            if result.get("success") is True:
                relationship_ids.append(_relationship_identity(relationship_spec))
            else:
                errors.append(
                    {
                        "section": "relationships",
                        "relationship": _relationship_identity(relationship_spec),
                        "reason_code": str(
                            result.get("error") or "relationship_failed"
                        ),
                        "details": result,
                    }
                )

    validation = validate_grounded_read_tool_evidence_contract()
    validation_errors = validation.get("errors") or []
    if isinstance(validation_errors, list):
        errors.extend(
            dict(error) for error in validation_errors if isinstance(error, Mapping)
        )

    created_concept_ids = [
        concept_id
        for concept_id, status in concept_status_by_id.items()
        if status == "created"
    ]
    repaired_concept_ids = [
        concept_id
        for concept_id, status in concept_status_by_id.items()
        if status == "repaired"
    ]
    existing_concept_ids = [
        concept_id
        for concept_id, status in concept_status_by_id.items()
        if status == "existing"
    ]
    return {
        "success": not errors and bool(validation.get("success")),
        "schema_version": GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION,
        "source_tag": GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_SOURCE_TAG,
        "managed_by": GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_MANAGED_BY,
        "contract_concept_id": GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_ID,
        "vocabulary_report": vocabulary_report,
        "created_concept_ids": created_concept_ids,
        "repaired_concept_ids": repaired_concept_ids,
        "existing_concept_ids": existing_concept_ids,
        "relationship_ids": relationship_ids,
        "validation": validation,
        "errors": errors,
        "counts": {
            "concept_specs": len(GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS),
            "relationship_specs": len(_grounded_read_relationship_specs()),
            "created_concepts": len(created_concept_ids),
            "existing_concepts": len(existing_concept_ids),
            "repaired_concepts": len(repaired_concept_ids),
            "relationships_written": len(relationship_ids),
            "errors": len(errors),
        },
    }


def validate_grounded_read_tool_evidence_contract() -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    missing_concept_ids: list[str] = []
    missing_relationships: list[str] = []
    vocabulary_validation = validate_tool_evidence_contract_vocabulary()
    if not vocabulary_validation.get("success"):
        errors.append(
            {
                "section": "generic_vocabulary",
                "reason_code": "tool_evidence_contract_vocabulary_invalid",
                "details": vocabulary_validation,
            }
        )
    for spec in GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS:
        if not isinstance(load_concept(spec.concept_id), Mapping):
            missing_concept_ids.append(spec.concept_id)
            errors.append(
                {
                    "section": "concepts",
                    "concept_id": spec.concept_id,
                    "reason_code": "missing_concept",
                }
            )
    for relationship_spec in _grounded_read_relationship_specs():
        source_doc = load_concept(relationship_spec.source_id)
        if not isinstance(source_doc, Mapping):
            continue
        targets = set(
            _normalise_targets(
                (source_doc.get("relationships") or {}).get(relationship_spec.predicate)
            )
        )
        if relationship_spec.target_id not in targets:
            identity = _relationship_identity(relationship_spec)
            missing_relationships.append(identity)
            errors.append(
                {
                    "section": "relationships",
                    "relationship": identity,
                    "reason_code": "missing_relationship",
                }
            )
    return {
        "success": not errors,
        "schema_version": GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION,
        "contract_concept_id": GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_ID,
        "view_concept_ids": (
            ARXIV_FINAL_ANSWER_VIEW_ID,
            RAG_FINAL_ANSWER_VIEW_ID,
            CONCEPT_EXISTS_FINAL_ANSWER_VIEW_ID,
            FETCH_CONCEPT_FINAL_ANSWER_VIEW_ID,
        ),
        "required_final_answer_field_ids_by_view": {
            ARXIV_FINAL_ANSWER_VIEW_ID: ARXIV_REQUIRED_FINAL_ANSWER_FIELD_IDS,
            RAG_FINAL_ANSWER_VIEW_ID: RAG_REQUIRED_FINAL_ANSWER_FIELD_IDS,
            CONCEPT_EXISTS_FINAL_ANSWER_VIEW_ID: (
                CONCEPT_EXISTS_REQUIRED_FINAL_ANSWER_FIELD_IDS
            ),
            FETCH_CONCEPT_FINAL_ANSWER_VIEW_ID: (
                FETCH_CONCEPT_REQUIRED_FINAL_ANSWER_FIELD_IDS
            ),
        },
        "missing_concept_ids": missing_concept_ids,
        "missing_relationships": missing_relationships,
        "generic_vocabulary_validation": vocabulary_validation,
        "errors": errors,
        "counts": {
            "expected_concepts": len(
                GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS
            ),
            "expected_relationships": len(_grounded_read_relationship_specs()),
            "missing_concepts": len(missing_concept_ids),
            "missing_relationships": len(missing_relationships),
            "errors": len(errors),
        },
    }


__all__ = [
    "ACCESS_CONTROL_ENFORCED_FIELD_ID",
    "ACCESS_RESTRICTION_FAMILIES_FIELD_ID",
    "ARXIV_FINAL_ANSWER_VIEW_ID",
    "ARXIV_PAPERS_COLLECTION_FIELD_ID",
    "ARXIV_PAPER_ID_FIELD_ID",
    "ARXIV_REQUIRED_FINAL_ANSWER_FIELD_IDS",
    "ARXIV_SEARCH_TOOL_ID",
    "ARXIV_TITLE_FIELD_ID",
    "ARXIV_URL_FIELD_ID",
    "CONCEPT_ACCESSIBLE_FIELD_ID",
    "CONCEPT_EXISTS_FIELD_ID",
    "CONCEPT_EXISTS_FINAL_ANSWER_VIEW_ID",
    "CONCEPT_EXISTS_REQUIRED_FINAL_ANSWER_FIELD_IDS",
    "CONCEPT_EXISTS_TOOL_ID",
    "CONCEPT_ID_FIELD_ID",
    "ERROR_CODE_FIELD_ID",
    "ERROR_DETAILS_FIELD_ID",
    "ERROR_FIELD_ID",
    "FETCH_CONCEPT_FINAL_ANSWER_VIEW_ID",
    "FETCH_CONCEPT_REQUIRED_FINAL_ANSWER_FIELD_IDS",
    "FETCH_CONCEPT_TOOL_ID",
    "GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS",
    "GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_ID",
    "GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION",
    "QUERY_FIELD_ID",
    "RAG_COUNT_FIELD_ID",
    "RAG_FINAL_ANSWER_VIEW_ID",
    "RAG_REQUIRED_FINAL_ANSWER_FIELD_IDS",
    "RAG_RESULTS_COLLECTION_FIELD_ID",
    "RAG_RETRIEVAL_STATE_FIELD_ID",
    "RAG_SEARCH_TOOL_ID",
    "RAG_SOURCE_SYSTEM_FIELD_ID",
    "RAG_TEXT_FIELD_ID",
    "SUCCESS_FIELD_ID",
    "SUGGESTIONS_FIELD_ID",
    "bootstrap_grounded_read_tool_evidence_contract",
    "canonical_grounded_read_tool_evidence_contract_concept_ids",
    "canonical_grounded_read_tool_evidence_contract_relationships",
    "validate_grounded_read_tool_evidence_contract",
]
