#!/usr/bin/env python3
"""
MCP stdio server wrapper for Vontology operations.
This provides a Model Context Protocol interface over stdin/stdout
while reusing the existing Flask endpoint logic.
"""

import sys
import os
import asyncio
import json
from typing import Any

# Adjust path to import from the project root
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError:
    print("Error: MCP package not installed. Run: pdm add mcp", file=sys.stderr)
    sys.exit(1)

# Absolute imports (works when run as a script)
from datetime import datetime, timezone
from src.backend.vontology.utils_vontology import (
    create_vontology_concept,
    get_all_vontology_nodes_with_details,
    get_vontology_tree,
    simulate_or_delete_concept,
)
from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.services.concept_service import (
    get_concept_display_name_with_names_fallback,
    get_concept_by_concept_id,
    enrich_concept_with_text_relations,
    update_concept,
)
from src.backend.services.concept_relation_service import build_concept_relations_payload
from src.backend.services.concept_search_service import search_concepts
from src.backend.services.text_value_service import upsert_text_for_concept
from src.backend.services.annotation_extraction_service import extract_annotations
from src.backend.services.concept_merge_service import merge_concepts
from src.backend.services.settings_service import (
    get_active_llm_setting,
    get_preferred_language,
    get_setting,
)
from src.backend.integrations.internal_mcp.catalogue import _add_relationship
from src.backend.integrations.internal_mcp.catalogue import _remove_relationship
from src.backend.integrations.internal_mcp.arxiv_proxy import (
    get_arxiv_proxy,
    ArxivProxyError,
)
from src.backend.integrations.internal_mcp.search_proxy_mcp import (
    get_search_proxy,
    SearchProxyError,
)
from src.backend.services.rag_service import get_rag_service, RAGBackendUnavailable

# Create MCP server instance
app = Server("vontology-mcp")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="get_context",
            description="Get current server-side context: active LLM model, language preference, and runtime settings. NOTE: User and organisation information is managed client-side (localStorage) per JVNAUTOSCI-628 and is not available through this endpoint.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="create_concepts",
            description="Creates one or more concepts (instances, types, or predicates). Each concept needs a name and kind. Use for bulk creation. Supports singleton arrays. After creation, use add_names for alternative names/translations. Unknown top-level fields are ignored to accommodate orchestrator-added context.",
            inputSchema={
                "type": "object",
                "properties": {
                    "parent_id": {
                        "type": "string",
                        "description": "The concept ID of the parent concept"
                    },
                    "concepts": {
                        "type": "array",
                        "description": "Array of concepts to create",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Primary name (required)"},
                                "kind": {"type": "string", "enum": ["instance", "type", "predicate"], "default": "type", "description": "'instance' for individuals, 'type' for subtypes, 'predicate' for relationships"},
                                "description": {"type": "string", "description": "Optional description"},
                                "notes": {"type": "string", "description": "Optional notes"}
                            },
                            "required": ["name"]
                        },
                        "minItems": 1
                    }
                },
                "required": ["parent_id", "concepts"]
            }
        ),
        Tool(
            name="find_subconcepts",
            description="Finds all direct subconcepts (children) of a given concept in the Vontology hierarchy",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept ID to find children of"
                    }
                },
                "required": ["concept_id"]
            }
        ),
        Tool(
            name="find_concepts_by_name",
            description="Searches for concepts in the Vontology by name substring",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The name substring to search for"
                    }
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="add_names",
            description="Adds one or more names (aliases/synonyms) to an existing concept using text relations. Supports multilingual names and different types: NL (Natural Language - default, for standard names/translations), ABBR (Abbreviation - for short forms like 'EU', 'NATO'), CODE (technical identifiers/URIs). Language codes use ISO 639-1/BCP 47 format (e.g., 'en-NZ', 'fr', 'de', 'mi', 'zh'). Default: en-NZ, NL.",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept ID to add names to (format: #V#concept_name)"
                    },
                    "names": {
                        "type": "array",
                        "description": "Array of names. Each can be: (1) a string like 'EU member' (defaults: en-NZ, NL), or (2) an object {name, language?, name_type?} for control. Examples: ['EU member'], [{name:'État membre', language:'fr'}], [{name:'EU MS', name_type:'ABBR'}]",
                        "items": {
                            "oneOf": [
                                {"type": "string", "description": "Simple name (defaults: en-NZ, NL)"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string", "description": "The name text"},
                                        "language": {"type": "string", "default": "en-NZ", "description": "Language code: en-NZ (default), en-US, fr, de, es, it, mi, zh, ja, etc."},
                                        "name_type": {"type": "string", "default": "NL", "description": "Type: NL (Natural Language), ABBR (Abbreviation), CODE (technical ID)", "enum": ["NL", "ABBR", "CODE"]}
                                    },
                                    "required": ["name"]
                                }
                            ]
                        },
                        "minItems": 1
                    }
                },
                "required": ["concept_id", "names"]
            }
        ),
        Tool(
            name="get_tree",
            description="Get complete ontology tree structure starting from the root 'thing' concept. Returns nested hierarchy showing all concepts and their relationships.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="fetch_concept",
            description="Fetch full details of ONE specific concept by its ID. Returns complete information including names (from text relations), description, metadata, relationships, salient predicates, and all other concept properties.",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept ID to fetch (format: #V#concept_name, e.g., '#V#person')"
                    },
                    "include_relations_arg1": {
                        "type": "boolean",
                        "description": "When true, include structural relations where the concept is the subject (argument 1)."
                    },
                    "include_relations_any_arg": {
                        "type": "boolean",
                        "description": "When true, include structural relations where the concept appears in any argument position (incoming references)."
                    },
                    "include_text_relations_arg1": {
                        "oneOf": [
                            {"type": "boolean"},
                            {"type": "string", "enum": ["snippets"], "description": "Use 'snippets' to request short text snippets in addition to raw text."}
                        ],
                        "description": "Include text relations (e.g., hasName, hasDescription) where the concept is the subject."
                    },
                    "predicate_filter": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional whitelist of predicate identifiers to include."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of relation entries to return (defaults to 200, capped at 500)."
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Offset applied before collecting relation entries."
                    },
                    "include_concept_preview": {
                        "type": "boolean",
                        "description": "Toggle inclusion of light-weight previews for related concepts.",
                        "default": True
                    }
                },
                "required": ["concept_id"]
            }
        ),
        Tool(
            name="search_concepts",
            description="Advanced concept search with multiple filter options. Supports filtering by kind (individual/type/predicate), instance_of, hierarchy paths, and match types.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query text"},
                    "instance_of": {"type": "string", "description": "Filter to instances of a specific type (e.g., '#V#researcher')"},
                    "filter_kind": {"type": "array", "items": {"type": "string", "enum": ["individual", "type", "predicate"]}, "description": "Filter by concept kind"},
                    "include_hierarchy_path": {"type": "boolean", "description": "Include full hierarchy path"},
                    "match_type": {"type": "string", "enum": ["exact", "substring", "similarity"], "description": "Type of matching"},
                    "namespace": {"type": ["string", "null"], "description": "Optional namespace for future isolation; currently accepted but not required"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="vontology_concept_search",
            description="Namespaced alias for concept search used by the MCP orchestrator. Same behaviour as search_concepts (query required; use empty string when combining with instance_of filters).",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query text"},
                    "instance_of": {"type": "string", "description": "Filter to instances of a specific type"},
                    "filter_kind": {"type": "array", "items": {"type": "string", "enum": ["individual", "type", "predicate"]}, "description": "Filter by concept kind"},
                    "include_hierarchy_path": {"type": "boolean", "description": "Include full hierarchy path"},
                    "match_type": {"type": "string", "enum": ["exact", "substring", "similarity"], "description": "Type of matching"},
                    "namespace": {"type": ["string", "null"], "description": "Optional namespace for future isolation; currently accepted but not required"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="extract_annotations",
            description="Extract structured annotations from text using LLM analysis. Identifies concept mentions and suggests ontology links. Returns array of annotation objects with concept IDs, matched text, and confidence scores.",
            inputSchema={
                "type": "object",
                "properties": {
                    "input_text": {"type": "string", "description": "The text content to analyze"},
                    "context_concept_id": {"type": "string", "description": "Optional context concept ID"}
                },
                "required": ["input_text"]
            }
        ),
        Tool(
            name="search_arxiv",
            description="Search arXiv.org for scholarly articles. Returns list of papers with id, title, authors, summary, publication date. Supports boolean operators in query.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (supports AND, OR, NOT operators)"},
                    "max_results": {"type": "integer", "default": 10, "description": "Maximum results to return"},
                    "sort_by": {"type": "string", "enum": ["relevance", "lastUpdatedDate", "submittedDate"], "default": "relevance"},
                    "sort_order": {"type": "string", "enum": ["ascending", "descending"], "default": "descending"}
                },
                "required": []
            }
        ),
        Tool(
            name="get_paper_metadata",
            description="Get detailed metadata for a specific arXiv paper by its ID. Returns title, authors, abstract, publication dates, categories, DOI, PDF URL, comments.",
            inputSchema={
                "type": "object",
                "properties": {
                    "arxiv_id": {"type": "string", "description": "arXiv identifier (e.g., '2506.16596' or 'arXiv:2506.16596')"}
                },
                "required": ["arxiv_id"]
            }
        ),
        Tool(
            name="download_paper",
            description="Download PDF of an arXiv paper to local storage (data/arxiv_papers/). Returns file path where PDF was saved.",
            inputSchema={
                "type": "object",
                "properties": {
                    "arxiv_id": {"type": "string", "description": "arXiv identifier"},
                    "filename": {"type": "string", "description": "Optional custom filename (defaults to arxiv_id.pdf)"}
                },
                "required": ["arxiv_id"]
            }
        ),
        Tool(
            name="search_web",
            description="Search the web for current information using Tavily MCP proxy. Supports advanced search depth, domain filters, direct answers, raw content, and images.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "default": 10, "description": "Maximum number of results"},
                    "search_depth": {"type": "string", "enum": ["basic", "advanced"], "default": "basic", "description": "Search depth"},
                    "include_domains": {"type": "array", "items": {"type": "string"}, "description": "Optional whitelist of domains"},
                    "exclude_domains": {"type": "array", "items": {"type": "string"}, "description": "Optional blacklist of domains"},
                    "include_answer": {"type": "boolean", "default": False, "description": "Include AI-generated direct answer"},
                    "include_raw_content": {"type": "boolean", "default": False, "description": "Include raw page content"},
                    "include_images": {"type": "boolean", "default": False, "description": "Include image URLs"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="context_search",
            description="Context-aware web search that uses provided background information to refine Tavily results.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "context": {"type": "string", "description": "Context to focus the search"},
                    "max_results": {"type": "integer", "default": 10, "description": "Maximum number of results"},
                    "search_depth": {"type": "string", "enum": ["basic", "advanced"], "default": "basic", "description": "Search depth"},
                    "include_answer": {"type": "boolean", "default": False, "description": "Include AI-generated direct answer"}
                },
                "required": ["query", "context"]
            }
        ),
        Tool(
            name="qna_search",
            description="Question-answering optimised search that returns concise answers with supporting sources.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Question to answer"},
                    "max_results": {"type": "integer", "default": 5, "description": "Maximum number of results"},
                    "search_depth": {"type": "string", "enum": ["basic", "advanced"], "default": "advanced", "description": "Search depth"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="extract_url",
            description="Extract the main content and title from a specific URL using Tavily.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to extract content from"}
                },
                "required": ["url"]
            }
        ),
        Tool(
            name="add_relationship",
            description="Add a relationship between two concepts or from a concept to a text value. Use to add instance_of/typeOf relationships (e.g., add '#V#professor' as instance_of for a person), custom predicates (e.g., '#V#hasAffiliation' → 'Auckland University'), or any binary relationship. Supports both concept-to-concept relations (target is concept ID) and text predicates (target is text value). Common predicates: 'instance_of'/'instanceOf' (maps to is_an_instance_of), 'typeOf' (maps to is_a_type_of), or custom predicates like '#V#hasAffiliation', '#V#founderOf', '#V#hasResearchInterest'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {"type": "string", "description": "Source concept ID (e.g., '#V#nikola_k._kasabov')"},
                    "predicate": {"type": "string", "description": "Relationship type: 'instance_of', 'typeOf', or custom predicate like '#V#hasAffiliation'"},
                    "target": {"type": "string", "description": "Target concept ID (e.g., '#V#professor') or text value for text predicates"}
                },
                "required": ["source_id", "predicate", "target"]
            }
        ),
        Tool(
            name="remove_relationship",
            description="Remove a relationship between two concepts (concept-to-concept only). Use to clean incorrect type/instance links or other structural predicates.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {"type": "string", "description": "Source concept ID (e.g., '#V#mjw_work_diary_2025-11-24')"},
                    "predicate": {"type": "string", "description": "Relationship alias or stored field name (e.g., 'instance_of', 'typeOf')"},
                    "target": {"type": "string", "description": "Target concept ID (e.g., '#V#diary_entry_about_michael_witbrocks_work')"}
                },
                "required": ["source_id", "predicate", "target"]
            }
        ),
        Tool(
            name="delete_concept",
            description="Deletes a concept and handles its relationships. Can simulate the deletion first to see impact.",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept ID to delete"
                    },
                    "simulate": {
                        "type": "boolean",
                        "description": "If true (default), only simulates the deletion and returns a report. If false, executes the deletion.",
                        "default": True
                    }
                },
                "required": ["concept_id"]
            }
        ),
        Tool(
            name="merge_concepts",
            description="Merges a source concept into a target concept. Moves relationships, names, and text values, then deletes the source. Can simulate first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {
                        "type": "string",
                        "description": "The concept ID to merge FROM (will be deleted)"
                    },
                    "target_id": {
                        "type": "string",
                        "description": "The concept ID to merge TO (will receive data)"
                    },
                    "simulate": {
                        "type": "boolean",
                        "description": "If true (default), only simulates the merge and returns a report. If false, executes the merge.",
                        "default": True
                    }
                },
                "required": ["source_id", "target_id"]
            }
        ),
        Tool(
            name="update_concept",
            description="Update specific fields of a concept. Use when you need to modify properties or relationships directly (e.g. fixing ontology errors, changing 'kind' by updating relationships). Supports dot notation in update_data keys for partial updates of nested objects.",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept ID to update"
                    },
                    "update_data": {
                        "type": "object",
                        "description": "Dictionary of fields to update. Use dot notation for nested fields (e.g. {'relationships.is_an_instance_of': [...]})."
                    }
                },
                "required": ["concept_id", "update_data"]
            }
        ),
        Tool(
            name="search_knowledge_base",
            description="Search the internal knowledge base (RAG) for documents and indexed content. Use when user asks about internal documents, policies, or specific indexed knowledge that is not in the ontology or on the public web. Returns semantically relevant text chunks.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query text"},
                    "top_k": {"type": "integer", "default": 5, "description": "Number of results to return"},
                    "namespace": {"type": "string", "description": "Optional namespace filter"}
                },
                "required": ["query"]
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Handle tool calls by delegating to existing functions."""

    try:
        if name == "get_context":
            # Get active model setting (returns dict with model name and provider)
            model_setting = get_active_llm_setting()

            context = {
                "llm_model": model_setting.get("model") if isinstance(model_setting, dict) else model_setting,
                "llm_provider": model_setting.get("provider") if isinstance(model_setting, dict) else None,
                "language_preference": get_preferred_language(),
                "fetch_counts_on_load": get_setting("fetch_counts_on_load"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "note": "User and organisation context managed client-side (localStorage) per JVNAUTOSCI-628"
            }

            return [TextContent(
                type="text",
                text=json.dumps(context, indent=2)
            )]

        elif name == "create_concepts":
            parent_id = arguments.get("parent_id")
            concepts = arguments.get("concepts", [])

            if not parent_id or not concepts:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing required parameters: parent_id and concepts array"})
                )]

            results = []
            for concept_data in concepts:
                name_val = concept_data.get("name")
                kind = concept_data.get("kind", "type")  # Default to type if not specified
                description = concept_data.get("description")
                notes = concept_data.get("notes")

                if not name_val:
                    results.append({"error": "Concept missing required 'name' field", "data": concept_data})
                    continue

                # Map kind to create_as_instance parameter
                create_as_instance = (kind == "instance")

                result = create_vontology_concept(
                    parent_id=parent_id,
                    new_concept_name=name_val,
                    create_as_instance=create_as_instance,
                    description=description,
                    notes=notes
                )
                results.append(result)

            return [TextContent(
                type="text",
                text=json.dumps({"results": results, "total": len(concepts), "successful": sum(1 for r in results if r.get("success"))}, indent=2)
            )]

        elif name == "find_subconcepts":
            concept_id = arguments.get("concept_id")

            if not concept_id:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing concept_id parameter"})
                )]

            # Reuse the logic from Flask endpoint
            cursor = ConceptsRepository.find(
                {"relationships.is_a_type_of": concept_id},
                {"concept_id": 1, "name": 1, "names": 1, "computed_kind": 1}
            )

            subconcepts = []
            for doc in cursor:
                cid = doc.get('concept_id')
                if not cid:
                    continue

                try:
                    name = get_concept_display_name_with_names_fallback(doc)
                except Exception:
                    name = doc.get('name') or cid

                kind = doc.get('computed_kind') or 'individual'

                subconcepts.append({
                    "id": cid,
                    "name": name,
                    "kind": kind
                })

            return [TextContent(
                type="text",
                text=json.dumps(subconcepts, indent=2)
            )]

        elif name == "find_concepts_by_name":
            name_substring = arguments.get("name")

            if not name_substring:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing name parameter"})
                )]

            # Reuse the search service
            search_result = search_concepts(
                query=name_substring,
                match_type="substring",
                include_description=False,
                limit=100
            )

            matching_concepts = []
            for result in search_result.get('results', []):
                matching_concepts.append({
                    "id": result.get('concept_id'),
                    "name": result.get('name'),
                    "kind": result.get('kind', 'individual')
                })

            return [TextContent(
                type="text",
                text=json.dumps(matching_concepts, indent=2)
            )]

        elif name == "add_names":
            concept_id = arguments.get("concept_id")
            names = arguments.get("names")

            if not concept_id:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing concept_id parameter"})
                )]
            if not names or not isinstance(names, list) or len(names) == 0:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing or invalid names array"})
                )]

            # Verify concept exists
            concept = ConceptsRepository.find_one({"concept_id": concept_id})
            if not concept:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": f"Concept '{concept_id}' not found"})
                )]

            results = []
            errors = []

            # Process each name
            for idx, name_obj in enumerate(names):
                # Handle both string and dict formats
                if isinstance(name_obj, str):
                    name_text = name_obj
                    language = 'en-NZ'
                    name_type = 'NL'
                elif isinstance(name_obj, dict):
                    name_text = name_obj.get('name')
                    language = name_obj.get('language', 'en-NZ')
                    name_type = name_obj.get('name_type', 'NL')
                else:
                    errors.append({"index": idx, "error": "Invalid name format"})
                    continue

                if not name_text or not isinstance(name_text, str) or not name_text.strip():
                    errors.append({"index": idx, "error": "Missing or invalid name text"})
                    continue

                try:
                    result = upsert_text_for_concept(
                        subject_concept_id=concept_id,
                        predicate="hasName",
                        text=name_text.strip(),
                        lang=language,
                        context={"name_type": name_type}
                    )
                    if result:
                        results.append({
                            "index": idx,
                            "name": name_text.strip(),
                            "language": language,
                            "name_type": name_type,
                            "text_value_id": str(result.get('text_value_id')),
                            "relation_id": str(result.get('relation_id'))
                        })
                    else:
                        errors.append({"index": idx, "name": name_text, "error": "Failed to add"})
                except Exception as e:
                    errors.append({"index": idx, "name": name_text, "error": str(e)})

            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": len(errors) == 0,
                    "concept_id": concept_id,
                    "added_count": len(results),
                    "error_count": len(errors),
                    "results": results,
                    "errors": errors if errors else []
                }, indent=2))
            ]

        elif name == "get_tree":
            # Reuse existing tree function
            tree_result = get_vontology_tree()
            return [TextContent(
                type="text",
                text=json.dumps(tree_result, indent=2)
            )]

        elif name == "fetch_concept":
            concept_id = arguments.get("concept_id")

            if not concept_id:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing concept_id parameter"})
                )]

            try:
                concept = get_concept_by_concept_id(concept_id)
                if concept:
                    # Enrich with names from text relations
                    concept = enrich_concept_with_text_relations(concept)
                    include_relations_arg1 = bool(arguments.get("include_relations_arg1"))
                    include_relations_any_arg = bool(arguments.get("include_relations_any_arg"))
                    include_text_relations_arg1 = arguments.get("include_text_relations_arg1", False)
                    predicate_filter = arguments.get("predicate_filter")
                    limit = arguments.get("limit")
                    offset = arguments.get("offset")
                    include_concept_preview = arguments.get("include_concept_preview", True)

                    if predicate_filter is not None and not isinstance(predicate_filter, list):
                        if isinstance(predicate_filter, (tuple, set)):
                            predicate_filter = list(predicate_filter)
                        else:
                            predicate_filter = [predicate_filter]

                    if any([include_relations_arg1, include_relations_any_arg, include_text_relations_arg1]):
                        relations_payload = build_concept_relations_payload(
                            concept,
                            include_relations_arg1=include_relations_arg1,
                            include_relations_any_arg=include_relations_any_arg,
                            include_text_relations_arg1=include_text_relations_arg1,
                            predicate_filter=predicate_filter,
                            limit=limit,
                            offset=offset,
                            include_concept_preview=include_concept_preview,
                        )
                        concept["relations"] = relations_payload
                    return [TextContent(
                        type="text",
                        text=json.dumps(concept, indent=2, default=str)
                    )]
                else:
                    return [TextContent(
                        type="text",
                        text=json.dumps({"error": f"Concept '{concept_id}' not found"})
                    )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": str(e)})
                )]

        elif name in {"search_concepts", "vontology_concept_search"}:
            # Use existing search_concepts service with provided arguments
            search_result = search_concepts(**arguments)
            return [TextContent(
                type="text",
                text=json.dumps(search_result, indent=2, default=str)
            )]

        elif name == "extract_annotations":
            input_text = arguments.get("input_text")
            context_concept_id = arguments.get("context_concept_id")

            if not input_text:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing input_text parameter"})
                )]

            try:
                annotations_result = extract_annotations(text=input_text)
                if context_concept_id:
                    annotations_result = {
                        "context_concept_id": context_concept_id,
                        "annotations": annotations_result,
                    }
                return [TextContent(
                    type="text",
                    text=json.dumps(annotations_result, indent=2, default=str)
                )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": str(e)})
                )]

        elif name == "search_arxiv":
            try:
                proxy = get_arxiv_proxy()
                result = proxy.search_arxiv(
                    query=arguments.get("query", ""),
                    max_results=arguments.get("max_results", 10),
                    sort_by=arguments.get("sort_by", "relevance"),
                    sort_order=arguments.get("sort_order", "descending")
                )
                return [TextContent(
                    type="text",
                    text=json.dumps(result, indent=2)
                )]
            except ArxivProxyError as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": str(e), "success": False})
                )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": f"Unexpected error: {str(e)}", "success": False})
                )]

        elif name == "get_paper_metadata":
            arxiv_id = arguments.get("arxiv_id")

            if not arxiv_id:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing required parameter: arxiv_id"})
                )]

            try:
                proxy = get_arxiv_proxy()
                result = proxy.get_paper_metadata(arxiv_id=arxiv_id)
                return [TextContent(
                    type="text",
                    text=json.dumps(result, indent=2)
                )]
            except ArxivProxyError as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": str(e), "success": False})
                )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": f"Unexpected error: {str(e)}", "success": False})
                )]

        elif name == "download_paper":
            arxiv_id = arguments.get("arxiv_id")

            if not arxiv_id:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing required parameter: arxiv_id"})
                )]

            try:
                proxy = get_arxiv_proxy()
                result = proxy.download_paper(
                    arxiv_id=arxiv_id,
                    filename=arguments.get("filename")
                )
                return [TextContent(
                    type="text",
                    text=json.dumps(result, indent=2)
                )]
            except ArxivProxyError as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": str(e), "success": False})
                )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": f"Unexpected error: {str(e)}", "success": False})
                )]

        elif name == "search_web":
            query = arguments.get("query")
            if not query:
                return [TextContent(type="text", text=json.dumps({"error": "Missing query parameter"}))]

            try:
                proxy = await get_search_proxy()
                result = await proxy.search(
                    query=query,
                    max_results=arguments.get("max_results", 10),
                    search_depth=arguments.get("search_depth", "basic"),
                    include_domains=arguments.get("include_domains"),
                    exclude_domains=arguments.get("exclude_domains"),
                    include_answer=arguments.get("include_answer", False),
                    include_raw_content=arguments.get("include_raw_content", False),
                    include_images=arguments.get("include_images", False),
                )
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
            except SearchProxyError as e:
                return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {e}"}))]

        elif name == "context_search":
            query = arguments.get("query")
            context_value = arguments.get("context")
            if not query or not context_value:
                return [TextContent(type="text", text=json.dumps({"error": "Missing required parameters: query and context"}))]

            try:
                proxy = await get_search_proxy()
                result = await proxy.context_search(
                    query=query,
                    context=context_value,
                    max_results=arguments.get("max_results", 10),
                    search_depth=arguments.get("search_depth", "basic"),
                    include_answer=arguments.get("include_answer", False),
                )
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
            except SearchProxyError as e:
                return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {e}"}))]

        elif name == "qna_search":
            query = arguments.get("query")
            if not query:
                return [TextContent(type="text", text=json.dumps({"error": "Missing query parameter"}))]

            try:
                proxy = await get_search_proxy()
                result = await proxy.qna_search(
                    query=query,
                    max_results=arguments.get("max_results", 5),
                    search_depth=arguments.get("search_depth", "advanced"),
                )
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
            except SearchProxyError as e:
                return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {e}"}))]

        elif name == "extract_url":
            url = arguments.get("url")
            if not url:
                return [TextContent(type="text", text=json.dumps({"error": "Missing url parameter"}))]

            try:
                proxy = await get_search_proxy()
                result = await proxy.extract(url=url)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
            except SearchProxyError as e:
                return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {e}"}))]

        elif name == "add_relationship":
            source_id = arguments.get("source_id")
            predicate = arguments.get("predicate")
            target = arguments.get("target")

            if not source_id or not predicate or not target:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing required parameters: source_id, predicate, and target"})
                )]

        elif name == "remove_relationship":
            source_id = arguments.get("source_id")
            predicate = arguments.get("predicate")
            target = arguments.get("target")

            if not source_id or not predicate or not target:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing required parameters: source_id, predicate, and target"})
                )]

            try:
                result = _remove_relationship(
                    source_id=source_id,
                    predicate=predicate,
                    target=target
                )
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": f"Failed to remove relationship: {str(e)}"})
                )]

            try:
                result = _add_relationship(
                    source_id=source_id,
                    predicate=predicate,
                    target=target
                )
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": f"Failed to add relationship: {str(e)}"})
                )]

        elif name == "delete_concept":
            concept_id = arguments.get("concept_id")
            simulate = arguments.get("simulate", True)

            if not concept_id:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing concept_id parameter"})
                )]

            result = simulate_or_delete_concept(concept_id, execute=not simulate)
            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "merge_concepts":
            source_id = arguments.get("source_id")
            target_id = arguments.get("target_id")
            simulate = arguments.get("simulate", True)

            if not source_id or not target_id:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing source_id or target_id parameter"})
                )]

            result = merge_concepts(source_id, target_id, simulate=simulate)
            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "update_concept":
            concept_id = arguments.get("concept_id")
            update_data = arguments.get("update_data")

            if not concept_id:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing concept_id parameter"})
                )]
            if not update_data or not isinstance(update_data, dict):
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": "Missing or invalid update_data dictionary"})
                )]

            try:
                result = update_concept(concept_id=concept_id, update_data=update_data)
                if result:
                    return [TextContent(
                        type="text",
                        text=json.dumps({
                            "success": True,
                            "concept_id": concept_id,
                            "updated_fields": list(update_data.keys())
                        }, indent=2)
                    )]
                else:
                    return [TextContent(
                        type="text",
                        text=json.dumps({"error": "Update failed or concept not found", "success": False})
                    )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": str(e), "success": False})
                )]

        elif name == "search_knowledge_base":
            query = arguments.get("query")
            if not query:
                return [TextContent(type="text", text=json.dumps({"error": "Missing query parameter"}))]

            try:
                service = get_rag_service()

                # Build permissions context for org-scoped RAG filtering
                permissions_context = {}
                try:
                    from flask import session as flask_session
                    if flask_session.get("user_id"):
                        permissions_context["user_id"] = flask_session.get("user_id")
                    if flask_session.get("org_id"):
                        permissions_context["organisation_concept_id"] = flask_session.get("org_id")
                except (ImportError, RuntimeError):
                    # Not in Flask context - permissions_context remains empty
                    pass

                results = service.query(
                    query_text=query,
                    top_k=arguments.get("top_k", 5),
                    namespace=arguments.get("namespace"),
                    permissions_context=permissions_context if permissions_context else None
                )
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "results": results,
                        "count": len(results),
                        "success": True
                    }, indent=2)
                )]
            except RAGBackendUnavailable as e:
                return [TextContent(type="text", text=json.dumps({"error": f"RAG service unavailable: {e}", "success": False}))]
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {e}", "success": False}))]

        else:
            return [TextContent(
                type="text",
                text=json.dumps({"error": f"Unknown tool: {name}"})
            )]

    except Exception as e:
        return [TextContent(
            type="text",
            text=json.dumps({"error": str(e)})
        )]


async def main():
    """Run the MCP server over stdio."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
