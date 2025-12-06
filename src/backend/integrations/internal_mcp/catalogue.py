"""Default method catalogue for the internal MCP gateway."""

from __future__ import annotations

from typing import List

from .gateway import MethodCatalogue, MethodDefinition
from .schemas import Schema


def _get_vontology_tree(**kwargs):
    from ...vontology.utils_vontology import get_vontology_tree

    return get_vontology_tree(**kwargs)


def _get_concept_by_concept_id(**kwargs):
    from ...services.concept_relation_service import build_concept_relations_payload
    from ...services.concept_service import (
        enrich_concept_with_text_relations,
        get_concept_by_concept_id,
    )

    concept_id = kwargs.get("concept_id")
    if not concept_id:
        raise ValueError("concept_id is required")

    concept = get_concept_by_concept_id(concept_id=concept_id)

    # Use shared enrichment logic (migrate-on-read + fetch names from text relations)
    if not concept:
        return concept

    concept = enrich_concept_with_text_relations(concept)

    include_relations_arg1 = bool(kwargs.get("include_relations_arg1"))
    include_relations_any_arg = bool(kwargs.get("include_relations_any_arg"))
    include_text_relations_arg1 = kwargs.get("include_text_relations_arg1", False)
    predicate_filter = kwargs.get("predicate_filter")
    limit = kwargs.get("limit")
    offset = kwargs.get("offset")
    include_concept_preview = kwargs.get("include_concept_preview", True)

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

    return concept


def _get_context(**kwargs):
    from ...services.settings_service import (
        get_active_llm_setting,
        get_preferred_language,
        get_setting
    )
    from datetime import datetime, timezone

    # Get active model setting (returns dict with model name and provider)
    model_setting = get_active_llm_setting()

    context = {
        "user": None,
        "organisation": None,
        "llm_model": model_setting.get("model") if isinstance(model_setting, dict) else model_setting,
        "llm_provider": model_setting.get("provider") if isinstance(model_setting, dict) else None,
        "language": get_preferred_language(),
        "fetch_counts_on_load": get_setting("fetch_counts_on_load"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "note": "User and organisation context managed client-side (localStorage) per JVNAUTOSCI-628"
    }

    # Try to get user info if available (deprecated, but kept for backward compatibility)
    user_email = get_setting("current_user_email")
    if user_email:
        context["user"] = {"email": user_email}

    # Organisation (usually client-side)
    org_id = get_setting("current_organisation_id")
    if org_id:
        context["organisation"] = {"id": org_id}

    return context


def _get_paper_metadata(**kwargs):
    import asyncio
    from .arxiv_proxy_mcp import get_arxiv_proxy, ArxivProxyError

    arxiv_id = kwargs.get("arxiv_id")
    if not arxiv_id:
        return {"error": "Missing required parameter: arxiv_id", "success": False}

    async def _async_metadata():
        try:
            proxy = await get_arxiv_proxy()
            return await proxy.get_paper_metadata(arxiv_id=arxiv_id)
        except ArxivProxyError as e:
            return {"error": str(e), "success": False}
        except Exception as e:
            return {"error": f"Unexpected error: {e}", "success": False}

    return asyncio.run(_async_metadata())


def _create_concepts(**kwargs):
    from ...vontology.utils_vontology import create_vontology_concept

    parent_id = kwargs.get("parent_id")
    concepts = kwargs.get("concepts", [])

    if not parent_id:
        return {"error": "Missing required parameter: parent_id"}
    if not concepts or not isinstance(concepts, list):
        return {"error": "Missing or invalid 'concepts' array"}

    results = []
    for concept_data in concepts:
        if not isinstance(concept_data, dict):
            results.append({"error": "Concept must be an object", "data": concept_data})
            continue

        name = concept_data.get("name")
        kind = concept_data.get("kind", "type")  # Default to type

        if not name:
            results.append({"error": "Concept missing required 'name' field", "data": concept_data})
            continue

        # Map kind to create_as_instance parameter
        create_as_instance = (kind == "instance")

        result = create_vontology_concept(
            parent_id=parent_id,
            new_concept_name=name,
            create_as_instance=create_as_instance,
            description=concept_data.get("description"),
            notes=concept_data.get("notes")
        )
        results.append(result)

    return {
        "results": results,
        "total": len(concepts),
        "successful": sum(1 for r in results if isinstance(r, dict) and r.get("success"))
    }


def _extract_annotations(**kwargs):
    from ...services.annotation_extraction_service import extract_annotations

    return extract_annotations(**kwargs)


def _search_concepts(**kwargs):
    from ...services.concept_search_service import search_concepts

    return search_concepts(**kwargs)


def _add_names_to_concept(**kwargs):
    from ...services.text_value_service import upsert_text_for_concept
    from ...db.repositories.concepts_repository import ConceptsRepository

    concept_id = kwargs.get("concept_id")
    names = kwargs.get("names")

    if not concept_id:
        return {"error": "Missing 'concept_id' parameter"}
    if not names or not isinstance(names, list) or len(names) == 0:
        return {"error": "Missing or invalid 'names' array"}

    # Verify concept exists
    concept = ConceptsRepository.find_one({"concept_id": concept_id})
    if not concept:
        return {"error": f"Concept '{concept_id}' not found"}

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
                    "text_value_id": str(result["text_value_id"]),
                    "relation_id": str(result["relation_id"])
                })
            else:
                errors.append({"index": idx, "name": name_text, "error": "Failed to add"})
        except Exception as e:
            errors.append({"index": idx, "name": name_text, "error": str(e)})

    return {
        "success": len(errors) == 0,
        "concept_id": concept_id,
        "added_count": len(results),
        "error_count": len(errors),
        "results": results,
        "errors": errors if errors else []
    }


def _add_relationship(**kwargs):
    """Add a relationship between two concepts or from concept to text value."""
    from ...db.repositories.concepts_repository import ConceptsRepository
    from ...services.text_value_service import upsert_text_for_concept

    source_id = kwargs.get("source_id")
    predicate = kwargs.get("predicate")
    target = kwargs.get("target")

    if not source_id:
        return {"success": False, "error": "Missing 'source_id' parameter"}
    if not predicate:
        return {"success": False, "error": "Missing 'predicate' parameter"}
    if not target:
        return {"success": False, "error": "Missing 'target' parameter"}

    if source_id == target:
        return {"success": False, "error": "Source and target cannot be the same"}

    try:
        repo = ConceptsRepository

        # Check if source exists
        src = repo.find_one({"concept_id": source_id})
        if not src:
            return {"success": False, "error": f"Source concept '{source_id}' not found"}

        # Determine if this is a text predicate (binary_text_predicate instance)
        is_text_predicate = False
        if predicate.startswith('#V#'):
            pred_doc = repo.find_one({"concept_id": predicate}, {"relationships.is_an_instance_of": 1})
            if pred_doc:
                instance_of = pred_doc.get('relationships', {}).get('is_an_instance_of', [])
                if isinstance(instance_of, str):
                    instance_of = [instance_of]
                is_text_predicate = '#V#binary_text_predicate' in instance_of

        # Handle text predicates (target is text value, not concept)
        if is_text_predicate:
            result = upsert_text_for_concept(
                subject_concept_id=source_id,
                predicate=predicate,
                text=target,
                lang='en',
                provenance={"source": "add_relationship"}
            )
            return {
                "success": True,
                "relationship_type": "text_relation",
                "source_id": source_id,
                "predicate": predicate,
                "target": target,
                "text_value_id": str(result.get("text_value_id")),
                "relation_id": str(result.get("relation_id"))
            }

        # Handle concept-to-concept relationships
        # Check if target concept exists
        tgt = repo.find_one({"concept_id": target})
        if not tgt:
            return {"success": False, "error": f"Target concept '{target}' not found"}

        # Map common predicate names to database field names
        predicate_map = {
            "instance_of": "is_an_instance_of",
            "instanceOf": "is_an_instance_of",
            "type_of": "is_a_type_of",
            "typeOf": "is_a_type_of",
            "subtype": "has_subtype",
            "instance": "has_instance"
        }
        rel_kind = predicate_map.get(predicate, predicate)

        # Read current relationships
        existing = repo.find_one({"concept_id": source_id}, {f"relationships.{rel_kind}": 1}) or {}
        rels = (existing.get("relationships") or {})
        curr = rels.get(rel_kind)

        # Normalize to array
        if isinstance(curr, str):
            curr_list = [curr] if curr else []
        elif isinstance(curr, list):
            curr_list = curr
        else:
            curr_list = []

        # Check if already exists
        if target in curr_list:
            return {
                "success": True,
                "message": "Relationship already exists",
                "source_id": source_id,
                "predicate": rel_kind,
                "target": target,
                "already_existed": True
            }

        # Add the relationship
        update_result = repo.update_one(
            {"concept_id": source_id},
            {"$addToSet": {f"relationships.{rel_kind}": target}}
        )

        if update_result.modified_count > 0 or update_result.matched_count > 0:
            return {
                "success": True,
                "source_id": source_id,
                "predicate": rel_kind,
                "target": target,
                "added": update_result.modified_count > 0
            }
        else:
            return {"success": False, "error": "Failed to add relationship"}

    except Exception as e:
        return {"success": False, "error": f"Exception: {str(e)}"}


def _remove_relationship(**kwargs):
    """Remove a relationship between two concepts. Text relations removal is not supported here."""
    from ...db.repositories.concepts_repository import ConceptsRepository

    source_id = kwargs.get("source_id")
    predicate = kwargs.get("predicate")
    target = kwargs.get("target")

    if not source_id:
        return {"success": False, "error": "Missing 'source_id' parameter"}
    if not predicate:
        return {"success": False, "error": "Missing 'predicate' parameter"}
    if not target:
        return {"success": False, "error": "Missing 'target' parameter"}

    try:
        repo = ConceptsRepository
        # Verify source concept exists
        src = repo.find_one({"concept_id": source_id})
        if not src:
            return {"success": False, "error": f"Source concept '{source_id}' not found"}

        # Map common predicate aliases to stored field names
        predicate_map = {
            "instance_of": "is_an_instance_of",
            "instanceOf": "is_an_instance_of",
            "type_of": "is_a_type_of",
            "typeOf": "is_a_type_of",
            "subtype": "has_subtype",
            "instance": "has_instance",
        }
        rel_kind = predicate_map.get(predicate, predicate)

        # Ensure the relationship field exists (optional sanity)
        existing = repo.find_one({"concept_id": source_id}, {f"relationships.{rel_kind}": 1}) or {}
        rels = (existing.get("relationships") or {})
        curr = rels.get(rel_kind)
        # Normalise to list
        if isinstance(curr, str):
            curr_list = [curr] if curr else []
        elif isinstance(curr, list):
            curr_list = curr
        else:
            curr_list = []

        if target not in curr_list:
            return {
                "success": True,
                "message": "Relationship not present",
                "source_id": source_id,
                "predicate": rel_kind,
                "target": target,
                "already_absent": True,
            }

        update_result = repo.update_one(
            {"concept_id": source_id},
            {"$pull": {f"relationships.{rel_kind}": target}},
        )

        if update_result.modified_count > 0:
            return {
                "success": True,
                "source_id": source_id,
                "predicate": rel_kind,
                "target": target,
                "removed": True,
            }
        else:
            # Matched but no modification (race or duplicate state)
            return {
                "success": True,
                "source_id": source_id,
                "predicate": rel_kind,
                "target": target,
                "removed": False,
            }
    except Exception as e:
        return {"success": False, "error": f"Exception: {str(e)}"}


# arXiv MCP proxy handlers
def _search_arxiv(**kwargs):
    import asyncio
    from .arxiv_proxy_mcp import get_arxiv_proxy, ArxivProxyError

    async def _async_search():
        try:
            proxy = await get_arxiv_proxy()
            return await proxy.search_arxiv(
                query=kwargs.get("query", ""),
                max_results=kwargs.get("max_results", 10),
                sort_by=kwargs.get("sort_by", "relevance"),
                sort_order=kwargs.get("sort_order", "descending"),
            )
        except ArxivProxyError as e:
            return {"error": str(e), "success": False}
        except Exception as e:
            return {"error": f"Unexpected error: {e}", "success": False}

    return asyncio.run(_async_search())





def _download_paper(**kwargs):
    import asyncio
    from .arxiv_proxy_mcp import get_arxiv_proxy, ArxivProxyError

    arxiv_id = kwargs.get("arxiv_id")
    if not arxiv_id:
        return {"error": "Missing required parameter: arxiv_id", "success": False}

    async def _async_download():
        try:
            proxy = await get_arxiv_proxy()
            return await proxy.download_paper(
                arxiv_id=arxiv_id,
                filename=kwargs.get("filename"),
            )
        except ArxivProxyError as e:
            return {"error": str(e), "success": False}
        except Exception as e:
            return {"error": f"Unexpected error: {e}", "success": False}

    return asyncio.run(_async_download())


def _list_papers(**kwargs):
    import asyncio
    from .arxiv_proxy_mcp import get_arxiv_proxy, ArxivProxyError

    async def _async_list():
        try:
            proxy = await get_arxiv_proxy()
            return await proxy.list_papers()
        except ArxivProxyError as e:
            return {"error": str(e), "success": False}
        except Exception as e:
            return {"error": f"Unexpected error: {e}", "success": False}

    return asyncio.run(_async_list())


def _read_paper(**kwargs):
    import asyncio
    from .arxiv_proxy_mcp import get_arxiv_proxy, ArxivProxyError

    arxiv_id = kwargs.get("arxiv_id")
    if not arxiv_id:
        return {"error": "Missing required parameter: arxiv_id", "success": False}

    async def _async_read():
        try:
            proxy = await get_arxiv_proxy()
            return await proxy.read_paper(arxiv_id=arxiv_id)
        except ArxivProxyError as e:
            return {"error": str(e), "success": False}
        except Exception as e:
            return {"error": f"Unexpected error: {e}", "success": False}

    return asyncio.run(_async_read())


# Search MCP handlers
def _search_web(**kwargs):
    import asyncio
    from .search_proxy_mcp import get_search_proxy, SearchProxyError

    query = kwargs.get("query")
    if not query:
        return {"error": "Missing required parameter: query", "success": False}

    async def _async_search():
        try:
            proxy = await get_search_proxy()
            return await proxy.search(
                query=query,
                max_results=kwargs.get("max_results", 10),
                search_depth=kwargs.get("search_depth", "basic"),
                include_domains=kwargs.get("include_domains"),
                exclude_domains=kwargs.get("exclude_domains"),
                include_answer=kwargs.get("include_answer", False),
                include_raw_content=kwargs.get("include_raw_content", False),
                include_images=kwargs.get("include_images", False),
            )
        except SearchProxyError as e:
            return {"error": str(e), "success": False}
        except Exception as e:
            return {"error": f"Unexpected error: {e}", "success": False}

    return asyncio.run(_async_search())


def _context_search(**kwargs):
    import asyncio
    from .search_proxy_mcp import get_search_proxy, SearchProxyError

    query = kwargs.get("query")
    context = kwargs.get("context")

    if not query:
        return {"error": "Missing required parameter: query", "success": False}
    if not context:
        return {"error": "Missing required parameter: context", "success": False}

    async def _async_context_search():
        try:
            proxy = await get_search_proxy()
            return await proxy.context_search(
                query=query,
                context=context,
                max_results=kwargs.get("max_results", 10),
                search_depth=kwargs.get("search_depth", "basic"),
                include_answer=kwargs.get("include_answer", False),
            )
        except SearchProxyError as e:
            return {"error": str(e), "success": False}
        except Exception as e:
            return {"error": f"Unexpected error: {e}", "success": False}

    return asyncio.run(_async_context_search())


def _qna_search(**kwargs):
    import asyncio
    from .search_proxy_mcp import get_search_proxy, SearchProxyError

    query = kwargs.get("query")
    if not query:
        return {"error": "Missing required parameter: query", "success": False}

    async def _async_qna_search():
        try:
            proxy = await get_search_proxy()
            return await proxy.qna_search(
                query=query,
                max_results=kwargs.get("max_results", 5),
                search_depth=kwargs.get("search_depth", "advanced"),
            )
        except SearchProxyError as e:
            return {"error": str(e), "success": False}
        except Exception as e:
            return {"error": f"Unexpected error: {e}", "success": False}

    return asyncio.run(_async_qna_search())


def _extract_url(**kwargs):
    import asyncio
    from .search_proxy_mcp import get_search_proxy, SearchProxyError

    url = kwargs.get("url")
    if not url:
        return {"error": "Missing required parameter: url", "success": False}

    async def _async_extract():
        try:
            proxy = await get_search_proxy()
            return await proxy.extract(url=url)
        except SearchProxyError as e:
            return {"error": str(e), "success": False}
        except Exception as e:
            return {"error": f"Unexpected error: {e}", "success": False}

    return asyncio.run(_async_extract())


def _delete_concept(**kwargs):
    from ...vontology.utils_vontology import simulate_or_delete_concept

    concept_id = kwargs.get("concept_id")
    simulate = kwargs.get("simulate", True)

    if not concept_id:
        return {"error": "Missing concept_id parameter"}

    return simulate_or_delete_concept(concept_id, execute=not simulate)


def _merge_concepts(**kwargs):
    from ...services.concept_merge_service import merge_concepts

    source_id = kwargs.get("source_id")
    target_id = kwargs.get("target_id")
    simulate = kwargs.get("simulate", True)

    if not source_id or not target_id:
        return {"error": "Missing source_id or target_id parameter"}

    return merge_concepts(source_id, target_id, simulate=simulate)


def _update_concept(**kwargs):
    from ...services.concept_service import update_concept

    concept_id = kwargs.get("concept_id")
    update_data = kwargs.get("update_data")

    if not concept_id:
        return {"error": "Missing required parameter: concept_id"}
    if not update_data or not isinstance(update_data, dict):
        return {"error": "Missing or invalid 'update_data' dictionary"}

    try:
        result = update_concept(concept_id=concept_id, update_data=update_data)
        if result:
            return {"success": True, "concept_id": concept_id, "updated_fields": list(update_data.keys())}
        else:
            return {"success": False, "error": "Update failed or concept not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _tree_input_schema() -> Schema:
    return Schema(
        required={},
        optional={"root_concept": str},
        allow_unknown=True,
        description="get_vontology_tree input",
    )


def _tree_output_schema() -> Schema:
    return Schema(
        required={"tree": list},
        optional={},
        allow_unknown=True,
        description="get_vontology_tree output",
    )


def _concept_fetch_input_schema() -> Schema:
    return Schema(
        required={"concept_id": str},
        optional={
            "include_relations_arg1": (bool,),
            "include_relations_any_arg": (bool,),
            "include_text_relations_arg1": (bool, str),
            "predicate_filter": (list,),
            "limit": (int,),
            "offset": (int,),
            "include_concept_preview": (bool,),
        },
        allow_unknown=True,
        description=(
            "get_concept_by_concept_id input: concept_id (required), plus optional"
            " flags to include structural/text relations (include_relations_arg1,"
            " include_relations_any_arg, include_text_relations_arg1), predicate"
            " filtering, paging (limit/offset), and concept preview toggling."
        ),
    )


def _concepts_create_input_schema() -> Schema:
    return Schema(
        required={
            "parent_id": str,
            "concepts": list,
        },
        optional={},
        allow_unknown=True,
        description="create_concepts input: parent_id (str, parent concept_id), concepts (list of {name, kind?, description?, notes?}). kind: 'instance' for individuals, 'type' for subtypes (default), 'predicate' for relationships. Unknown top-level fields are ignored to accommodate orchestrator-added context (e.g., namespace).",
    )


def _concepts_create_output_schema() -> Schema:
    return Schema(
        required={
            "results": list,
            "total": int,
            "successful": int,
        },
        optional={},
        allow_unknown=True,
        description="create_concepts output: results (list of creation results), total (int), successful (int)",
    )


def _annotation_input_schema() -> Schema:
    return Schema(
        required={"text": str},
        optional={
            "use_llm": (bool, type(None)),
            "use_match": (bool,),
            "return_timings": (bool,),
        },
        allow_unknown=True,
        description="extract_annotations input",
    )


def _concept_search_input_schema() -> Schema:
    return Schema(
        required={
            "query": str,
        },
        optional={
            "filter_kind": (list, type(None)),
            "scope_root": (str, type(None)),
            "instance_of": (str, type(None)),
            "match_type": (str,),
            "exact_match": (bool,),  # Deprecated, use match_type instead
            "min_similarity": (float,),
            "include_description": (bool,),
            "include_hierarchy_path": (bool,),
            "limit": (int,),
        },
        allow_unknown=True,
        description="search_concepts input: query (str, optional - defaults to empty), match_type ('exact'|'substring'|'similarity'|'all'), min_similarity (float 0.0-1.0), filter_kind (list[str]), scope_root (str), instance_of (str concept_id - finds instances of this type), include_description (bool), include_hierarchy_path (bool), limit (int)",
    )


def _concept_search_output_schema() -> Schema:
    return Schema(
        required={
            "results": list,
            "total_count": int,
            "match_types_used": list,
            "query_info": dict,
        },
        optional={},
        allow_unknown=True,
        description="search_concepts output: results (list of {concept_id, name, kind, relevance_score, similarity_score?, hierarchy?}), total_count (int), match_types_used (list[str]), query_info (dict). hierarchy (when include_hierarchy_path=true): {primary_path, paths[], all_parents, max_depth, is_root}",
    )


def _add_names_input_schema() -> Schema:
    return Schema(
        required={
            "concept_id": str,
            "names": list,
        },
        optional={},
        allow_unknown=True,
        description="add_names input: concept_id (str, e.g., '#V#person'), names (list, array of names - each can be a string or dict with {name, language, name_type}). Strings default to en-NZ language and NL type.",
    )


def _add_names_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
            "concept_id": str,
            "added_count": int,
            "error_count": int,
        },
        optional={
            "results": (list, type(None)),
            "errors": (list, type(None)),
        },
        allow_unknown=True,
        description="add_names output: success (bool), concept_id (str), added_count (int), error_count (int), results (list of {index, name, language, name_type, text_value_id, relation_id}), errors (list of {index, name?, error})",
    )


def _add_relationship_input_schema() -> Schema:
    return Schema(
        required={
            "source_id": str,
            "predicate": str,
            "target": str,
        },
        optional={},
        allow_unknown=True,
        description="add_relationship input: source_id (str, concept ID like '#V#nikola_k._kasabov'), predicate (str, relationship type like 'instance_of', 'typeOf', or custom predicate like '#V#hasAffiliation'), target (str, target concept ID like '#V#professor' or text value for text predicates like 'Auckland University')",
    )


def _add_relationship_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
        },
        optional={
            "source_id": (str, type(None)),
            "predicate": (str, type(None)),
            "target": (str, type(None)),
            "relationship_type": (str, type(None)),
            "text_value_id": (str, type(None)),
            "relation_id": (str, type(None)),
            "added": (bool, type(None)),
            "already_existed": (bool, type(None)),
            "message": (str, type(None)),
            "error": (str, type(None)),
        },
        allow_unknown=True,
        description="add_relationship output: success (bool), source_id, predicate, target, and optional fields: relationship_type ('text_relation' or omitted for concept relations), text_value_id/relation_id (for text predicates), added (bool, false if already existed), already_existed (bool), message, error",
    )


def _remove_relationship_input_schema() -> Schema:
    return Schema(
        required={
            "source_id": str,
            "predicate": str,
            "target": str,
        },
        optional={},
        allow_unknown=True,
        description="remove_relationship input: source_id (str), predicate (alias or field name), target (str). Removes concept-to-concept relations only; text relations removal not supported here.",
    )


def _remove_relationship_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
        },
        optional={
            "source_id": (str, type(None)),
            "predicate": (str, type(None)),
            "target": (str, type(None)),
            "removed": (bool, type(None)),
            "already_absent": (bool, type(None)),
            "message": (str, type(None)),
            "error": (str, type(None)),
        },
        allow_unknown=True,
        description="remove_relationship output: success (bool), source_id, predicate, target, removed (bool), already_absent (bool when relation was not present), message, error",
    )


# arXiv MCP tool schemas
def _search_arxiv_input_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "query": (str,),
            "max_results": (int,),
            "sort_by": (str,),
            "sort_order": (str,),
        },
        allow_unknown=True,
        description="search_arxiv input: query (str, search query with boolean operators), max_results (int, default 10), sort_by (str, 'relevance'|'lastUpdatedDate'|'submittedDate'), sort_order (str, 'ascending'|'descending')",
    )


def _search_arxiv_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "results": (list, type(None)),
            "total": (int, type(None)),
            "error": (str, type(None)),
            "success": (bool, type(None)),
        },
        allow_unknown=True,
        description="search_arxiv output: results (list of papers with id, title, authors, summary, published), total (int), or error (str) if failed",
    )





def _download_paper_input_schema() -> Schema:
    return Schema(
        required={
            "arxiv_id": str,
        },
        optional={
            "filename": (str, type(None)),
        },
        allow_unknown=True,
        description="download_paper input: arxiv_id (str, e.g., '2506.16596'), filename (str, optional custom name)",
    )


def _download_paper_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "success": (bool, type(None)),
            "file_path": (str, type(None)),
            "arxiv_id": (str, type(None)),
            "error": (str, type(None)),
        },
        allow_unknown=True,
        description="download_paper output: success (bool), file_path (str, location of downloaded PDF), arxiv_id (str), or error (str) if failed",
    )


def _list_papers_input_schema() -> Schema:
    return Schema(
        required={},
        optional={},
        allow_unknown=True,
        description="list_papers input: no parameters required",
    )


def _list_papers_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "total_papers": (int, type(None)),
            "papers": (list, type(None)),
            "error": (str, type(None)),
            "success": (bool, type(None)),
        },
        allow_unknown=True,
        description="list_papers output: total_papers (int), papers (list of paper objects with title, summary, authors, links, pdf_url), or error (str) if failed",
    )


def _read_paper_input_schema() -> Schema:
    return Schema(
        required={
            "arxiv_id": str,
        },
        optional={},
        allow_unknown=True,
        description="read_paper input: arxiv_id (str, e.g., '1706.03762')",
    )


def _read_paper_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "status": (str, type(None)),
            "paper_id": (str, type(None)),
            "content": (str, type(None)),
            "error": (str, type(None)),
            "success": (bool, type(None)),
        },
        allow_unknown=True,
        description="read_paper output: status (str), paper_id (str), content (str, markdown text of paper), or error (str) if failed",
    )


# Search MCP tool schemas
def _search_web_input_schema() -> Schema:
    return Schema(
        required={
            "query": str,
        },
        optional={
            "max_results": (int,),
            "search_depth": (str,),
            "include_domains": (list, type(None)),
            "exclude_domains": (list, type(None)),
            "include_answer": (bool,),
            "include_raw_content": (bool,),
            "include_images": (bool,),
        },
        allow_unknown=True,
        description="search_web input: query (str, required), max_results (int, default 10), search_depth (str, 'basic'|'advanced', default 'basic'), include_domains (list of str, domain whitelist), exclude_domains (list of str, domain blacklist), include_answer (bool, AI-generated answer), include_raw_content (bool, full page content), include_images (bool, image URLs)",
    )


def _search_web_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "results": (list, type(None)),
            "answer": (str, type(None)),
            "images": (list, type(None)),
            "error": (str, type(None)),
            "success": (bool, type(None)),
        },
        allow_unknown=True,
        description="search_web output: results (list of {title, url, content, score}), answer (str, if include_answer=true), images (list, if include_images=true), or error (str) if failed",
    )


def _get_paper_metadata_input_schema() -> Schema:
    return Schema(
        required={
            "arxiv_id": str,
        },
        optional={},
        allow_unknown=True,
        description="get_paper_metadata input: arxiv_id (str, e.g., '2506.16596' or 'arXiv:2506.16596')",
    )


def _get_paper_metadata_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "id": (str, type(None)),
            "title": (str, type(None)),
            "authors": (list, type(None)),
            "abstract": (str, type(None)),
            "published": (str, type(None)),
            "updated": (str, type(None)),
            "categories": (list, type(None)),
            "doi": (str, type(None)),
            "pdf_url": (str, type(None)),
            "comments": (str, type(None)),
            "error": (str, type(None)),
            "success": (bool, type(None)),
        },
        allow_unknown=True,
        description="get_paper_metadata output: detailed metadata for a single arXiv paper, or error if unavailable",
    )



def _context_search_input_schema() -> Schema:
    return Schema(
        required={
            "query": str,
            "context": str,
        },
        optional={
            "max_results": (int,),
            "search_depth": (str,),
            "include_answer": (bool,),
        },
        allow_unknown=True,
        description="context_search input: query (str, required), context (str, required, contextual information to improve search), max_results (int, default 10), search_depth (str, 'basic'|'advanced', default 'basic'), include_answer (bool, AI-generated answer)",
    )


def _context_search_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "results": (list, type(None)),
            "answer": (str, type(None)),
            "error": (str, type(None)),
            "success": (bool, type(None)),
        },
        allow_unknown=True,
        description="context_search output: results (list of {title, url, content, score}), answer (str, if include_answer=true), or error (str) if failed",
    )


def _qna_search_input_schema() -> Schema:
    return Schema(
        required={
            "query": str,
        },
        optional={
            "max_results": (int,),
            "search_depth": (str,),
        },
        allow_unknown=True,
        description="qna_search input: query (str, required, question to answer), max_results (int, default 5), search_depth (str, 'basic'|'advanced', default 'advanced')",
    )


def _qna_search_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "answer": (str, type(None)),
            "results": (list, type(None)),
            "error": (str, type(None)),
            "success": (bool, type(None)),
        },
        allow_unknown=True,
        description="qna_search output: answer (str, direct answer to question), results (list of supporting sources {title, url, content, score}), or error (str) if failed",
    )


def _extract_url_input_schema() -> Schema:
    return Schema(
        required={
            "url": str,
        },
        optional={},
        allow_unknown=True,
        description="extract_url input: url (str, required, URL to extract content from)",
    )


def _extract_url_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "content": (str, type(None)),
            "title": (str, type(None)),
            "url": (str, type(None)),
            "error": (str, type(None)),
            "success": (bool, type(None)),
        },
        allow_unknown=True,
        description="extract_url output: content (str, extracted text), title (str, page title), url (str, source URL), or error (str) if failed",
    )


def _delete_concept_input_schema() -> Schema:
    return Schema(
        required={"concept_id": str},
        optional={"simulate": (bool,)},
        allow_unknown=True,
        description="delete_concept input: concept_id (str), simulate (bool, default true)"
    )


def _delete_concept_output_schema() -> Schema:
    return Schema(
        required={"success": bool},
        optional={"simulate": (bool,), "error": (str,), "operations": (list,), "warnings": (list,)},
        allow_unknown=True,
        description="delete_concept output"
    )


def _merge_concepts_input_schema() -> Schema:
    return Schema(
        required={"source_id": str, "target_id": str},
        optional={"simulate": (bool,)},
        allow_unknown=True,
        description="merge_concepts input: source_id (str), target_id (str), simulate (bool, default true)"
    )


def _merge_concepts_output_schema() -> Schema:
    return Schema(
        required={"success": bool},
        optional={"simulate": (bool,), "error": (str,), "operations": (list,), "warnings": (list,)},
        allow_unknown=True,
        description="merge_concepts output"
    )


def _update_concept_input_schema() -> Schema:
    return Schema(
        required={"concept_id": str, "update_data": dict},
        optional={},
        allow_unknown=True,
        description="update_concept input: concept_id (str), update_data (dict). Use dot notation for nested fields (e.g. {'relationships.is_an_instance_of': [...]}) to avoid overwriting entire objects."
    )


def _update_concept_output_schema() -> Schema:
    return Schema(
        required={"success": bool},
        optional={"concept_id": (str,), "updated_fields": (list,), "error": (str,)},
        allow_unknown=True,
        description="update_concept output"
    )


# RAG Tool Handlers and Schemas
def _search_knowledge_base(**kwargs):
    from ...services.rag_service import get_rag_service, RAGBackendUnavailable
    import os

    query_text = kwargs.get("query")
    if not query_text:
        return {"error": "Missing required parameter: query", "success": False}

    try:
        service = get_rag_service()  # Default backend
        # Resolve effective namespace: prefer explicit, else env, else derive from user.id if provided
        ns = kwargs.get("namespace")
        if not ns:
            ns = os.environ.get("VON_DEFAULT_NAMESPACE")
        if not ns:
            user = kwargs.get("user")
            user_id = user.get("id") if isinstance(user, dict) else None
            if isinstance(user_id, str) and user_id.strip():
                ns = f"#V#{user_id.strip().lower().replace(' ', '_')}"

        # SECURITY: Require namespace for RAG search - prevents cross-user data leakage
        if not ns:
            return {
                "error": "namespace_required",
                "message": "RAG search requires authenticated user context (namespace)",
                "success": False
            }

        # Build permissions context from Flask session for org-scoped RAG filtering
        permissions_context = {}
        try:
            from flask import session as flask_session
            if flask_session.get("user_id"):
                permissions_context["user_id"] = flask_session.get("user_id")
            if flask_session.get("org_id"):
                permissions_context["organisation_concept_id"] = flask_session.get("org_id")
        except (ImportError, RuntimeError):
            # Not in Flask context - use user_id from kwargs if available
            if isinstance(user_id, str) and user_id.strip():
                permissions_context["user_id"] = user_id.strip().lower().replace(' ', '_')

        results = service.query(
            query_text=query_text,
            top_k=kwargs.get("top_k", 5),
            namespace=ns,
            permissions_context=permissions_context if permissions_context else None,
        )
        return {
            "results": results,
            "count": len(results),
            "success": True
        }
    except RAGBackendUnavailable as e:
        return {"error": f"RAG service unavailable: {e}", "success": False}
    except Exception as e:
        return {"error": f"Unexpected error: {e}", "success": False}


def _search_knowledge_base_input_schema() -> Schema:
    return Schema(
        required={"query": str},
        optional={
            "top_k": (int,),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description="search_knowledge_base input: query (str), top_k (int, default 5), namespace (str, optional filter)",
    )


def _search_knowledge_base_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "results": (list, type(None)),
            "count": (int, type(None)),
            "error": (str, type(None)),
            "success": (bool, type(None)),
        },
        allow_unknown=True,
        description="search_knowledge_base output: results (list of {id, score, text, metadata}), count (int), or error (str)",
    )

# RAG metadata/content MCP tools
def _rag_get_status(**kwargs):
    import requests
    import os
    try:
        ns = kwargs.get('namespace') or os.environ.get('VON_DEFAULT_NAMESPACE')
        url = 'http://127.0.0.1:5002/admin/rag_status'
        if ns:
            url = f"{url}?namespace={ns}"
        res = requests.get(url, timeout=5)
        if res.ok:
            return res.json()
        return {"error": f"HTTP {res.status_code}", "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def _rag_list_indexed(**kwargs):
    from ...db.connection_manager import get_db
    import os
    db = get_db()
    if db is None:
        return {"error": "db_unavailable", "success": False}
    coll = db['interaction_sessions']
    limit = int(kwargs.get('limit', 20))
    offset = int(kwargs.get('offset', 0))

    # Resolve effective namespace: prefer explicit, else env, else derive from user.id if provided
    ns = kwargs.get("namespace")
    if not ns:
        ns = os.environ.get("VON_DEFAULT_NAMESPACE")
    if not ns:
        user = kwargs.get("user")
        user_id = user.get("id") if isinstance(user, dict) else None
        if isinstance(user_id, str) and user_id.strip():
            ns = f"#V#{user_id.strip().lower().replace(' ', '_')}"

    # SECURITY: Require namespace for RAG access - prevents cross-user data leakage
    if not ns:
        return {
            "error": "namespace_required",
            "message": "RAG access requires authenticated user context (namespace)",
            "success": False
        }

    # Build query with namespace filter
    query = {"indexing_status": "indexed", "namespace": ns}

    cursor = coll.find(query, {"_id": 1, "indexed_at": 1, "summary": 1, "history": 1, "namespace": 1}).skip(offset).limit(limit)
    items = []
    for doc in cursor:
        preview_len = 0
        if isinstance(doc.get('summary'), str):
            preview_len += len(doc['summary'])
        history = doc.get('history') or []
        if isinstance(history, list):
            for h in history:
                content = h.get('content')
                if isinstance(content, str):
                    preview_len += len(content)
        items.append({
            "session_id": str(doc.get('_id')),
            "indexed_at": str(doc.get('indexed_at')) if doc.get('indexed_at') else None,
            "preview_length": preview_len,
            "namespace": doc.get('namespace')
        })
    total = coll.count_documents(query)
    return {"items": items, "total": total, "limit": limit, "offset": offset, "namespace": ns, "success": True}


def _rag_get_item(**kwargs):
    from ...db.connection_manager import get_db
    from bson import ObjectId
    import os
    db = get_db()
    if db is None:
        return {"error": "db_unavailable", "success": False}
    session_id = kwargs.get('session_id')
    if not session_id:
        return {"error": "Missing session_id", "success": False}

    # Resolve effective namespace: prefer explicit, else env, else derive from user.id if provided
    ns = kwargs.get("namespace")
    if not ns:
        ns = os.environ.get("VON_DEFAULT_NAMESPACE")
    if not ns:
        user = kwargs.get("user")
        user_id = user.get("id") if isinstance(user, dict) else None
        if isinstance(user_id, str) and user_id.strip():
            ns = f"#V#{user_id.strip().lower().replace(' ', '_')}"

# SECURITY: Require namespace for RAG access - prevents cross-user data leakage
    if not ns:
        return {
            "error": "namespace_required",
            "message": "RAG access requires authenticated user context (namespace)",
            "success": False
        }

    coll = db['interaction_sessions']
    try:
        query = {"_id": ObjectId(session_id), "namespace": ns}
    except Exception:
        query = {"_id": session_id, "namespace": ns}

    doc = coll.find_one(query)
    if not doc:
        return {"error": "not_found", "success": False}
    # Build a safe preview
    preview = []
    if isinstance(doc.get('summary'), str):
        preview.append(doc['summary'])
    history = doc.get('history') or []
    if isinstance(history, list):
        for h in history:
            c = h.get('content')
            if isinstance(c, str):
                preview.append(c)
    return {
        "session_id": str(doc.get('_id')),
        "indexing_status": doc.get('indexing_status'),
        "indexed_at": str(doc.get('indexed_at')) if doc.get('indexed_at') else None,
        "namespace": doc.get('namespace'),
        "preview": "\n\n".join(preview)[:4000],
        "success": True
    }


def build_default_catalogue() -> MethodCatalogue:
    """Return a catalogue pre-populated with the baseline method set."""

    catalogue = MethodCatalogue()
    concept_search_input_schema = _concept_search_input_schema()
    concept_search_output_schema = _concept_search_output_schema()
    definitions: List[MethodDefinition] = [
        MethodDefinition(
            name="get_context",
            handler=_get_context,
            input_schema=Schema(
                required={},
                optional={},
                allow_unknown=False,
                description="get_context input: no parameters required"
            ),
            output_schema=Schema(
                required={
                    "llm_model": (str, type(None)),
                    "language": str,
                    "timestamp": str
                },
                optional={
                    "user": (dict, type(None)),
                    "organisation": (dict, type(None)),
                    "llm_provider": (str, type(None)),
                    "fetch_counts_on_load": (bool, type(None)),
                    "note": (str, type(None))
                },
                allow_unknown=True,
                description="get_context output: context info including user, org, llm_model (string), llm_provider, language. User/org managed client-side per JVNAUTOSCI-628."
            ),
            category="read",
            description="Get current server-side context: active LLM model (string), provider, language preference, and runtime settings. NOTE: User and organisation information is managed client-side (localStorage) per JVNAUTOSCI-628 and may not be available here. Use when you need to know what model/language is configured.",
        ),
        MethodDefinition(
            name="get_tree",
            handler=_get_vontology_tree,
            input_schema=_tree_input_schema(),
            output_schema=_tree_output_schema(),
            category="read",
            description="Get complete ontology tree structure. Use when user wants to browse/explore the whole taxonomy or understand overall organization. Returns nested hierarchy starting from 'thing' root.",
        ),
        MethodDefinition(
            name="fetch_concept",
            handler=_get_concept_by_concept_id,
            input_schema=_concept_fetch_input_schema(),
            output_schema=None,
            category="read",
            description="Fetch full details of ONE specific concept by its ID (format: #V#concept_name). Use when you already know the exact concept_id and need complete information (description, predicates, relationships). Don't use for searching.",
        ),
        MethodDefinition(
            name="create_concepts",
            handler=_create_concepts,
            input_schema=_concepts_create_input_schema(),
            output_schema=_concepts_create_output_schema(),
            category="write",
            description="Create one or more concepts (instances, types, or predicates). Each concept needs name and kind ('instance' for individuals, 'type' for subtypes/default, 'predicate' for relationships). Accepts array of {name, kind?, description?, notes?}. Supports singleton arrays. Use add_names afterward for alternative names/translations.",
        ),
        MethodDefinition(
            name="extract_annotations",
            handler=_extract_annotations,
            input_schema=_annotation_input_schema(),
            output_schema=None,
            category="read",
            description="Find concept mentions in text and suggest ontology links. Use when user provides text/document and wants to identify which ontology concepts appear in it. Returns concept matches with context. Not for general concept search.",
        ),
        MethodDefinition(
            name="search_concepts",
            handler=_search_concepts,
            input_schema=concept_search_input_schema,
            output_schema=concept_search_output_schema,
            category="read",
            description="Find concepts. For 'list instances of X': use instance_of='#V#type' param (e.g., instance_of='#V#researcher'). For 'find X': use query param. Other key params: filter_kind=['individual'|'type'], include_hierarchy_path=true (shows paths), match_type='exact'|'substring'|'similarity'.",
        ),
        MethodDefinition(
            name="vontology_concept_search",
            handler=_search_concepts,
            input_schema=concept_search_input_schema,
            output_schema=concept_search_output_schema,
            category="read",
            description="Namespaced alias for concept search used by the MCP orchestrator. Same parameters as search_concepts (query required; pass empty string when using instance_of filters).",
        ),
        MethodDefinition(
            name="add_names",
            handler=_add_names_to_concept,
            input_schema=_add_names_input_schema(),
            output_schema=_add_names_output_schema(),
            category="write",
            description="Add one or more names/aliases/synonyms to a concept using text relations (hasName predicate). Supports bulk operations and multilingual names. Pass array of strings for simple names (defaults: en-NZ, NL) or objects for control. Name types: NL (Natural Language - standard names/translations, default), ABBR (Abbreviation - 'EU', 'NATO', 'PhD'), CODE (URIs/system IDs). Language codes: en-NZ (default), en-US, fr, de, es, it, mi (Māori), zh (Chinese), ja (Japanese), etc. Examples: names=['EU member', 'EU state'] or names=[{name:'État membre', language:'fr'}, {name:'EU MS', name_type:'ABBR'}].",
        ),
        MethodDefinition(
            name="add_relationship",
            handler=_add_relationship,
            input_schema=_add_relationship_input_schema(),
            output_schema=_add_relationship_output_schema(),
            category="write",
            description="Add a relationship between two concepts or from a concept to a text value. Use to add instance_of/typeOf relationships (e.g., add '#V#professor' as instance_of for a person), custom predicates (e.g., '#V#hasAffiliation' → 'Auckland University'), or any binary relationship. Supports both concept-to-concept relations (target is concept ID) and text predicates (target is text value). Common predicates: 'instance_of'/'instanceOf' (maps to is_an_instance_of), 'typeOf' (maps to is_a_type_of), or custom predicates like '#V#hasAffiliation', '#V#founderOf', '#V#hasResearchInterest'. Examples: source_id='#V#nikola_k._kasabov', predicate='instance_of', target='#V#professor' OR source_id='#V#nikola_k._kasabov', predicate='#V#hasAffiliation', target='Auckland University of Technology'.",
        ),
        MethodDefinition(
            name="remove_relationship",
            handler=_remove_relationship,
            input_schema=_remove_relationship_input_schema(),
            output_schema=_remove_relationship_output_schema(),
            category="write",
            description="Remove a relationship between two concepts (concept-to-concept only). Use to clean incorrect type/instance links or other structural predicates. Text relation removal is not supported in this tool.",
        ),
        MethodDefinition(
            name="delete_concept",
            handler=_delete_concept,
            input_schema=_delete_concept_input_schema(),
            output_schema=_delete_concept_output_schema(),
            category="write",
            description="Deletes a concept and handles its relationships. Can simulate the deletion first to see impact. Use when you need to remove a concept from the ontology. Returns a report of operations and warnings.",
        ),
        MethodDefinition(
            name="merge_concepts",
            handler=_merge_concepts,
            input_schema=_merge_concepts_input_schema(),
            output_schema=_merge_concepts_output_schema(),
            category="write",
            description="Merges a source concept into a target concept. Moves relationships, names, and text values, then deletes the source. Can simulate first. Use when you have duplicate concepts and want to consolidate them into one.",
        ),
        MethodDefinition(
            name="update_concept",
            handler=_update_concept,
            input_schema=_update_concept_input_schema(),
            output_schema=_update_concept_output_schema(),
            category="write",
            description="Update specific fields of a concept. Use when you need to modify properties or relationships directly (e.g. fixing ontology errors, changing 'kind' by updating relationships). Supports dot notation in update_data keys for partial updates of nested objects.",
        ),
        # arXiv MCP tools
        MethodDefinition(
            name="search_arxiv",
            handler=_search_arxiv,
            input_schema=_search_arxiv_input_schema(),
            output_schema=_search_arxiv_output_schema(),
            category="read",
            timeout_sec=30.0,
            description="Search arXiv.org for scholarly articles. Use when user asks to find papers by author, keyword, topic, or date range. Returns list of papers with id, title, authors, summary, and publication date. Supports boolean operators in query (AND, OR, NOT). Example: 'causal reasoning AND neural networks'. Results can be sorted by relevance, submission date, or last updated date.",
        ),
        MethodDefinition(
            name="get_paper_metadata",
            handler=_get_paper_metadata,
            input_schema=_get_paper_metadata_input_schema(),
            output_schema=_get_paper_metadata_output_schema(),
            category="read",
            timeout_sec=20.0,
            description="Get detailed metadata for a single arXiv paper (title, authors, abstract, categories, DOI, pdf_url). Use when a user needs paper details without downloading the PDF.",
        ),

        MethodDefinition(
            name="download_paper",
            handler=_download_paper,
            input_schema=_download_paper_input_schema(),
            output_schema=_download_paper_output_schema(),
            category="write",
            timeout_sec=60.0,
            description="Download PDF of an arXiv paper to local storage (data/arxiv_papers/). Use when user asks to download/save/fetch a paper. Returns file path where PDF was saved. Accepts optional filename parameter for custom naming (defaults to arxiv_id.pdf).",
        ),
        MethodDefinition(
            name="list_papers",
            handler=_list_papers,
            input_schema=_list_papers_input_schema(),
            output_schema=_list_papers_output_schema(),
            category="read",
            timeout_sec=15.0,
            description="List all arXiv papers that have been downloaded to local storage (data/arxiv_papers/). Use when user asks 'what papers do I have?', 'list downloaded papers', or similar. Returns list of all locally stored papers with their metadata (title, authors, summary, links). No parameters required.",
        ),
        MethodDefinition(
            name="read_paper",
            handler=_read_paper,
            input_schema=_read_paper_input_schema(),
            output_schema=_read_paper_output_schema(),
            category="read",
            timeout_sec=20.0,
            description="Read the full content of a downloaded arXiv paper in markdown format. Use when user asks to read, summarise, analyse, or extract information from a downloaded paper. Paper must be downloaded first (use download_paper if needed). Returns markdown-formatted text content of the paper. Requires arxiv_id parameter (e.g., '1706.03762').",
        ),
        # Search MCP tools (Tavily)
        MethodDefinition(
            name="search_web",
            handler=_search_web,
            input_schema=_search_web_input_schema(),
            output_schema=_search_web_output_schema(),
            category="read",
            timeout_sec=15.0,
            description="⚠️ USE FOR RECENT/CURRENT INFORMATION ⚠️ Search the web for information published after your training cutoff. REQUIRED when user asks for: recent, latest, current, new, breaking, today's, this week's, 2024+, 2025+, 'what's new', 'recent advances', 'latest research', 'current developments'. Returns web pages with titles, URLs, content snippets, and relevance scores. Supports advanced search (search_depth='advanced'), domain filtering (include_domains/exclude_domains), AI-generated answers (include_answer=true), full page content (include_raw_content=true), and images (include_images=true).",
        ),
        MethodDefinition(
            name="context_search",
            handler=_context_search,
            input_schema=_context_search_input_schema(),
            output_schema=_context_search_output_schema(),
            category="read",
            timeout_sec=15.0,
            description="Context-aware web search that uses provided context to improve search relevance. Use when you have background information that would help narrow or focus the search. The context parameter should contain relevant information about the topic, domain, or specific requirements. More precise than basic search when you have contextual information. Example: query='latest research', context='machine learning interpretability in healthcare applications'.",
        ),
        MethodDefinition(
            name="qna_search",
            handler=_qna_search,
            input_schema=_qna_search_input_schema(),
            output_schema=_qna_search_output_schema(),
            category="read",
            timeout_sec=20.0,
            description="Question-answering optimized search that returns a direct answer to a specific question. Use when user asks a direct question that needs a factual answer from the web. Returns both a concise answer and supporting sources. Uses advanced search depth by default for better answer quality. Best for factual questions like 'When was X founded?', 'What is the population of Y?', 'Who invented Z?', 'How does X work?'. More focused and answer-oriented than general search.",
        ),
        MethodDefinition(
            name="extract_url",
            handler=_extract_url,
            input_schema=_extract_url_input_schema(),
            output_schema=_extract_url_output_schema(),
            category="read",
            timeout_sec=15.0,
            description="Extract and return the main text content from a specific URL. Use when user provides a URL and wants to read, analyse, or extract information from that specific web page. Returns cleaned text content and page title. Useful for reading articles, documentation, or any web page content. Example: 'read this article: https://example.com/article', 'extract content from this URL'.",
        ),
        MethodDefinition(
            name="search_knowledge_base",
            handler=_search_knowledge_base,
            input_schema=_search_knowledge_base_input_schema(),
            output_schema=_search_knowledge_base_output_schema(),
            category="read",
            timeout_sec=30.0,
            description="Search the internal knowledge base (RAG) for documents and indexed content. Use when user asks about internal documents, policies, or specific indexed knowledge that is not in the ontology or on the public web. Returns semantically relevant text chunks.",
        ),
        MethodDefinition(
            name="rag_get_status",
            handler=_rag_get_status,
            input_schema=Schema(required={}, optional={}, allow_unknown=True, description="No input"),
            output_schema=None,
            category="read",
            description="Get RAG status: totals, eligible counts, indexed/pending/failed/skipped. Mirrors /admin/rag_status."
        ),
        MethodDefinition(
            name="rag_list_indexed",
            handler=_rag_list_indexed,
            input_schema=Schema(required={}, optional={"limit": (int,), "offset": (int,), "namespace": (str, type(None))}, allow_unknown=True, description="List indexed sessions with optional namespace filter"),
            output_schema=None,
            category="read",
            description="List all RAG-indexed chat sessions/conversations for the current user. Returns total count and session metadata. Use this to answer 'how many RAG sessions' or 'what conversations are indexed'. Namespace filtered automatically."
        ),
        MethodDefinition(
            name="rag_get_item",
            handler=_rag_get_item,
            input_schema=Schema(required={"session_id": str}, optional={"namespace": (str, type(None))}, allow_unknown=True, description="Fetch one indexed session with optional namespace filter"),
            output_schema=None,
            category="read",
            description="Get one indexed item (session) with a safe text preview. Respects namespace isolation."
        )
    ]

    for definition in definitions:
        catalogue.register(definition)

    return catalogue
