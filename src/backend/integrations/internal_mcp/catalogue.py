"""Default method catalogue for the internal MCP gateway."""

from __future__ import annotations

from typing import Any, List

from .gateway import MethodCatalogue, MethodDefinition
from .schemas import Schema
from .orchestrator import InternalMCPChatOrchestrator
from src.backend.services.prompt_template_service import PromptTemplateService


def _run_async_compat(async_fn):
    """Run an async function from sync code.

    If we're already inside a running event loop (e.g. when called via the
    internal chat orchestrator), running `asyncio.run()` would raise.
    In that case, we execute the coroutine in a fresh event loop in a worker
    thread and block until completion.
    """

    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(async_fn())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(async_fn())).result()


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

    if any(
        [include_relations_arg1, include_relations_any_arg, include_text_relations_arg1]
    ):
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
        get_setting,
    )
    from datetime import datetime, timezone

    # Get active model setting (returns dict with model name and provider)
    model_setting = get_active_llm_setting()

    context = {
        "user": None,
        "organisation": None,
        "llm_model": (
            model_setting.get("model")
            if isinstance(model_setting, dict)
            else model_setting
        ),
        "llm_provider": (
            model_setting.get("provider") if isinstance(model_setting, dict) else None
        ),
        "language": get_preferred_language(),
        "fetch_counts_on_load": get_setting("fetch_counts_on_load"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "note": "User and organisation context managed client-side (localStorage) per JVNAUTOSCI-628",
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


def _get_client_capabilities(**kwargs):
    """Return the current client-reported capabilities snapshot (if any).

    Notes:
    - Stored per server-side session.
    - Client-reported and non-authoritative.
    - May be None if the client has not reported yet or no request context.
    """

    from ...services.client_capabilities_service import get_client_capabilities_snapshot

    return {"success": True, "capabilities": get_client_capabilities_snapshot()}


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

    return _run_async_compat(_async_metadata)


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
            results.append(
                {"error": "Concept missing required 'name' field", "data": concept_data}
            )
            continue

        # Map kind to create_as_instance parameter
        create_as_instance = kind == "instance"

        result = create_vontology_concept(
            parent_id=parent_id,
            new_concept_name=name,
            create_as_instance=create_as_instance,
            description=concept_data.get("description"),
            notes=concept_data.get("notes"),
        )
        results.append(result)

    return {
        "results": results,
        "total": len(concepts),
        "successful": sum(
            1 for r in results if isinstance(r, dict) and r.get("success")
        ),
    }


def _extract_annotations(**kwargs):
    from ...services.annotation_extraction_service import extract_annotations

    return extract_annotations(**kwargs)


def _search_concepts(**kwargs):
    from ...services.concept_search_service import search_concepts

    return search_concepts(**kwargs)


def _upsert_text_relation(**kwargs):
    from ...services.text_value_service import upsert_text_for_concept
    from ...services.rag_text_relation_change_hook_service import (
        maybe_sync_concept_text_relations_to_rag,
    )

    concept_id = kwargs.get("concept_id")
    predicate = kwargs.get("predicate")
    text = kwargs.get("text")
    language = kwargs.get("language", "en-NZ")
    context = kwargs.get("context")
    namespace = kwargs.get("namespace")

    if not concept_id:
        return {"success": False, "error": "Missing 'concept_id' parameter"}
    if not predicate:
        return {"success": False, "error": "Missing 'predicate' parameter"}
    if not text:
        return {"success": False, "error": "Missing 'text' parameter"}

    try:
        result = upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate=predicate,
            text=text,
            lang=language,
            context=context,
        )

        # Best-effort hook: if the orchestrator provided an authenticated namespace,
        # keep concept text-relations RAG index fresh.
        maybe_sync_concept_text_relations_to_rag(
            namespace=namespace,
            concept_id=concept_id,
            predicate=predicate,
        )

        text_preview = text[:100] + "..." if len(text) > 100 else text
        return {
            "success": True,
            "text_value_id": str(result.get("text_value_id")),
            "relation_id": str(result.get("relation_id")),
            "relation_created": result.get("relation_created"),
            "predicate": predicate,
            "text_preview": text_preview,
            "language": language,
        }
    except Exception as exc:
        return {"success": False, "error": f"Failed to upsert text relation: {exc}"}


def _get_text_relations(**kwargs):
    from ...services.text_value_service import get_texts_for_concept

    concept_id = kwargs.get("concept_id")
    predicate = kwargs.get("predicate")
    language = kwargs.get("language")
    limit = kwargs.get("limit", 50)

    if not concept_id:
        return {"error": "Missing 'concept_id' parameter"}

    try:
        relations = get_texts_for_concept(
            subject_concept_id=concept_id,
            predicate=predicate,
            lang=language,
            limit=limit,
        )

        # Add text previews for long content
        for relation in relations:
            text = relation.get("text", "")
            if len(text) > 200:
                relation["text_preview"] = text[:200] + "..."

        return {
            "concept_id": concept_id,
            "relations_found": len(relations),
            "relations": relations,
        }
    except Exception as exc:
        return {"error": f"Failed to get text relations: {exc}"}


def _update_text_relation(**kwargs):
    from ...services.text_value_service import update_text_relation_text
    from ...services.rag_text_relation_change_hook_service import (
        maybe_sync_concept_text_relations_to_rag,
    )

    concept_id = kwargs.get("concept_id")
    relation_id = kwargs.get("relation_id")
    new_text = kwargs.get("new_text")
    language = kwargs.get("language", "en-NZ")
    namespace = kwargs.get("namespace")

    if not concept_id:
        return {"error": "Missing 'concept_id' parameter"}
    if not relation_id:
        return {"error": "Missing 'relation_id' parameter"}
    if not new_text:
        return {"error": "Missing 'new_text' parameter"}

    try:
        result = update_text_relation_text(
            subject_concept_id=concept_id,
            relation_id=relation_id,
            new_text=new_text,
            lang=language,
        )

        maybe_sync_concept_text_relations_to_rag(
            namespace=namespace,
            concept_id=concept_id,
            predicate=result.get("predicate"),
        )

        old_preview = str(result.get("old_text", ""))[:100]
        new_preview = new_text[:100] + "..." if len(new_text) > 100 else new_text

        return {
            "success": True,
            "relation_id": relation_id,
            "old_text_preview": old_preview,
            "new_text_preview": new_preview,
            "text_value_id": str(result.get("text_value_id")),
        }
    except Exception as exc:
        return {"error": f"Failed to update text relation: {exc}"}


def _delete_text_relation(**kwargs):
    from ...services.text_value_service import (
        delete_text_relation,
        delete_text_relation_by_predicate_and_text,
    )
    from ...services.rag_text_relation_change_hook_service import (
        maybe_delete_text_relation_doc_from_rag,
    )

    concept_id = kwargs.get("concept_id")
    relation_id = kwargs.get("relation_id")
    predicate = kwargs.get("predicate")
    text = kwargs.get("text")
    language = kwargs.get("language")
    garbage_collect = bool(kwargs.get("garbage_collect"))
    namespace = kwargs.get("namespace")

    if not concept_id:
        return {"error": "Missing 'concept_id' parameter"}

    try:
        if relation_id:
            # Delete by relation ID (preferred)
            result = delete_text_relation(
                subject_concept_id=concept_id,
                relation_id=relation_id,
                garbage_collect=garbage_collect,
            )

            maybe_delete_text_relation_doc_from_rag(
                namespace=namespace,
                relation_id=relation_id,
            )

            return {
                "success": True,
                "deleted_relation_id": relation_id,
                "deleted_text_preview": None,
                "text_value_cleaned_up": bool(
                    result.get(
                        "orphaned_text_value_deleted",
                        result.get("text_value_cleaned_up", False),
                    )
                ),
            }
        elif predicate and text:
            # Delete by predicate + text match
            result = delete_text_relation_by_predicate_and_text(
                subject_concept_id=concept_id,
                predicate=predicate,
                text=text,
                lang=language,
                garbage_collect=garbage_collect,
            )

            maybe_delete_text_relation_doc_from_rag(
                namespace=namespace,
                relation_id=result.get("relation_id"),
            )

            return {
                "success": True,
                "deleted_relation_id": result.get("relation_id"),
                "deleted_text_preview": text[:100],
                "text_value_cleaned_up": result.get(
                    "orphaned_text_value_deleted", False
                ),
            }
        else:
            return {
                "error": "Must provide either relation_id or both predicate and text"
            }
    except Exception as exc:
        return {"error": f"Failed to delete text relation: {exc}"}


def _get_text_relations_summary(**kwargs):
    from ...services.text_value_service import get_text_relations_summary

    concept_id = kwargs.get("concept_id")
    predicates = kwargs.get("predicates")
    languages = kwargs.get("languages")
    max_relation_ids_per_group = kwargs.get("max_relation_ids_per_group", 25)

    if not concept_id:
        return {"error": "Missing 'concept_id' parameter", "success": False}

    try:
        return get_text_relations_summary(
            concept_id,
            predicates=predicates,
            languages=languages,
            max_relation_ids_per_group=max_relation_ids_per_group,
        )
    except Exception as exc:
        return {"error": f"Failed to summarise text relations: {exc}", "success": False}


def _upsert_singleton_text_relation(**kwargs):
    from ...services.text_value_service import upsert_singleton_text_relation

    concept_id = kwargs.get("concept_id")
    predicate = kwargs.get("predicate")
    text = kwargs.get("text")
    language = kwargs.get("language", "en-NZ")
    policy = kwargs.get("policy", "replace_others")
    garbage_collect = kwargs.get("garbage_collect")
    provenance = kwargs.get("provenance")
    context = kwargs.get("context")

    if not concept_id:
        return {"error": "Missing 'concept_id' parameter", "success": False}
    if not predicate:
        return {"error": "Missing 'predicate' parameter", "success": False}
    if not text:
        return {"error": "Missing 'text' parameter", "success": False}

    try:
        return upsert_singleton_text_relation(
            subject_concept_id=concept_id,
            predicate=predicate,
            text=text,
            lang=language,
            policy=policy,
            provenance=provenance,
            context=context,
            garbage_collect=(
                True if garbage_collect is None else bool(garbage_collect)
            ),
        )
    except Exception as exc:
        return {
            "error": f"Failed to upsert singleton text relation: {exc}",
            "success": False,
        }


def _concept_exists(**kwargs):
    from ...db.repositories.concepts_repository import ConceptsRepository
    from ...security.access_control import can_access_concept
    from ...vontology.code_concepts_registry import is_code_concept_id

    concept_id = kwargs.get("concept_id")
    if not concept_id:
        return {"error": "Missing 'concept_id' parameter", "success": False}

    try:
        doc = ConceptsRepository.find_one({"concept_id": concept_id}, {"_id": 1})
        exists = bool(doc) or is_code_concept_id(concept_id)
        accessible = can_access_concept(concept_id)
        return {
            "success": True,
            "concept_id": concept_id,
            "exists": exists,
            "accessible": accessible,
        }
    except Exception as exc:
        return {"error": f"Failed to check concept existence: {exc}", "success": False}


def _fetch_concept_content(**kwargs):
    from ...vontology.utils_vontology import get_vontology_node_content

    concept_id = kwargs.get("concept_id")
    reconstruct_md = kwargs.get("reconstruct_md", True)
    if not concept_id:
        return {"error": "Missing 'concept_id' parameter", "success": False}

    try:
        payload = get_vontology_node_content(
            concept_id, reconstruct_md=bool(reconstruct_md)
        )
        payload["success"] = "error" not in payload
        return payload
    except Exception as exc:
        return {"error": f"Failed to fetch concept content: {exc}", "success": False}


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
            language = "en-NZ"
            name_type = "NL"
        elif isinstance(name_obj, dict):
            name_text = name_obj.get("name")
            language = name_obj.get("language", "en-NZ")
            name_type = name_obj.get("name_type", "NL")
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
                context={"name_type": name_type},
            )
            if result:
                results.append(
                    {
                        "index": idx,
                        "name": name_text.strip(),
                        "language": language,
                        "name_type": name_type,
                        "text_value_id": str(result["text_value_id"]),
                        "relation_id": str(result["relation_id"]),
                    }
                )
            else:
                errors.append(
                    {"index": idx, "name": name_text, "error": "Failed to add"}
                )
        except Exception as e:
            errors.append({"index": idx, "name": name_text, "error": str(e)})

    return {
        "success": len(errors) == 0,
        "concept_id": concept_id,
        "added_count": len(results),
        "error_count": len(errors),
        "results": results,
        "errors": errors if errors else [],
    }


def _add_relationship(**kwargs):
    """Add a relationship between two concepts or from concept to text value."""
    from ...db.repositories.concepts_repository import ConceptsRepository
    from ...services.text_value_service import upsert_text_for_concept

    def _err(
        code: str,
        message: str,
        *,
        details: dict | None = None,
    ) -> dict:
        # Keep backward compatibility: preserve the top-level 'error' string.
        return {
            "success": False,
            "error": message,
            "error_code": code,
            "error_details": details or {},
        }

    source_id = kwargs.get("source_id")
    predicate = kwargs.get("predicate")
    target = kwargs.get("target")

    if not source_id:
        return _err(
            "missing_parameter",
            "Missing 'source_id' parameter",
            details={"missing": ["source_id"]},
        )
    if not predicate:
        return _err(
            "missing_parameter",
            "Missing 'predicate' parameter",
            details={"missing": ["predicate"]},
        )
    if not target:
        return _err(
            "missing_parameter",
            "Missing 'target' parameter",
            details={"missing": ["target"]},
        )

    if source_id == target:
        return _err(
            "relationship_self_reference",
            "Source and target cannot be the same",
            details={"source_id": source_id, "target": target},
        )

    try:
        repo = ConceptsRepository

        from ...vontology.code_concepts_registry import (
            build_virtual_concept_doc,
            is_code_concept_id,
        )
        from ...vontology.utils_vontology import is_predicate

        # Check if source exists
        src = repo.find_one({"concept_id": source_id})
        if not src:
            return _err(
                "source_concept_not_found",
                f"Source concept '{source_id}' not found",
                details={"role": "source", "concept_id": source_id},
            )

        # Common text predicates are frequently provided either as plain predicate IDs
        # (e.g. 'hasContent') or V-prefixed IDs (e.g. '#V#hasContent'). Treat these
        # as text relations without requiring a predicate concept to exist.
        predicate_str = (
            predicate.strip() if isinstance(predicate, str) else str(predicate)
        )
        structural_aliases = {
            "#V#is_a_type_of": "is_a_type_of",
            "#V#has_subtype": "has_subtype",
            "#V#is_an_instance_of": "is_an_instance_of",
            "#V#has_instance": "has_instance",
            "#V#related_to": "related_to",
        }
        if predicate_str in structural_aliases:
            predicate_str = structural_aliases[predicate_str]
        structural_aliases = {
            "#V#is_a_type_of": "is_a_type_of",
            "#V#has_subtype": "has_subtype",
            "#V#is_an_instance_of": "is_an_instance_of",
            "#V#has_instance": "has_instance",
            "#V#related_to": "related_to",
        }
        if predicate_str in structural_aliases:
            predicate_str = structural_aliases[predicate_str]
        predicate_normalised = (
            predicate_str[3:] if predicate_str.startswith("#V#") else predicate_str
        )
        well_known_text_predicates = {"hasContent", "hasDescription", "hasName"}
        if predicate_normalised in well_known_text_predicates:
            target_text = target if isinstance(target, str) else str(target)
            result = upsert_text_for_concept(
                subject_concept_id=source_id,
                predicate=predicate_normalised,
                text=target_text,
                lang="en",
                provenance={"source": "add_relationship"},
            )
            return {
                "success": True,
                "relationship_type": "text_relation",
                "source_id": source_id,
                "predicate": predicate_normalised,
                "predicate_input": predicate,
                "target": target_text,
                "text_value_id": str(result.get("text_value_id")),
                "relation_id": str(result.get("relation_id")),
            }

        # Determine if this is a text predicate (binary_text_predicate instance)
        is_text_predicate = False
        if predicate_str.startswith("#V#"):
            pred_doc = repo.find_one(
                {"concept_id": predicate_str}, {"relationships.is_an_instance_of": 1}
            )
            if pred_doc:
                instance_of = pred_doc.get("relationships", {}).get(
                    "is_an_instance_of", []
                )
                if isinstance(instance_of, str):
                    instance_of = [instance_of]
                is_text_predicate = "#V#binary_text_predicate" in instance_of

        # Handle text predicates (target is text value, not concept)
        if is_text_predicate:
            result = upsert_text_for_concept(
                subject_concept_id=source_id,
                predicate=predicate_str,
                text=target,
                lang="en",
                provenance={"source": "add_relationship"},
            )
            return {
                "success": True,
                "relationship_type": "text_relation",
                "source_id": source_id,
                "predicate": predicate_str,
                "target": target,
                "text_value_id": str(result.get("text_value_id")),
                "relation_id": str(result.get("relation_id")),
            }

        # Handle concept-to-concept relationships
        # Check if target concept exists
        tgt = repo.find_one({"concept_id": target})
        if not tgt:
            return _err(
                "target_concept_not_found",
                f"Target concept '{target}' not found",
                details={"role": "target", "concept_id": target},
            )

        # Map common predicate names to database field names
        predicate_map = {
            "instance_of": "is_an_instance_of",
            "instanceOf": "is_an_instance_of",
            "type_of": "is_a_type_of",
            "typeOf": "is_a_type_of",
            "subtype": "has_subtype",
            "instance": "has_instance",
        }
        rel_kind = predicate_map.get(predicate_str, predicate_str)

        # Guardrail: concept-to-concept relationship predicates must be either:
        # - one of the structural relationship kinds (is_a_type_of, has_subtype, ...), or
        # - a Vontology predicate concept id (starts with #V# and exists as a predicate).
        from ...db.repositories.concepts_repository import RELATIONSHIP_KINDS

        if isinstance(rel_kind, str) and rel_kind in RELATIONSHIP_KINDS:
            pass
        elif isinstance(rel_kind, str) and rel_kind.startswith("#V#"):
            pred_doc = repo.find_one(
                {"concept_id": rel_kind}, {"concept_id": 1, "relationships": 1}
            )
            if pred_doc is None and is_code_concept_id(rel_kind):
                pred_doc = build_virtual_concept_doc(rel_kind)
            if pred_doc is None:
                return _err(
                    "predicate_concept_not_found",
                    (
                        f"Predicate concept '{rel_kind}' not found. "
                        "Create it as a predicate in the Vontology (e.g. as an instance of #V#predicate) "
                        "before using it as a relationship."
                    ),
                    details={
                        "role": "predicate",
                        "predicate": rel_kind,
                        "predicate_input": predicate,
                    },
                )
            if not is_predicate(pred_doc):
                return _err(
                    "predicate_concept_not_typed",
                    (
                        f"Concept '{rel_kind}' exists but is not typed as a predicate. "
                        "Predicates must be instances of #V#predicate (or a predicate subtype)."
                    ),
                    details={
                        "role": "predicate",
                        "predicate": rel_kind,
                        "predicate_input": predicate,
                        "required_instance_of": "#V#predicate",
                    },
                )
        else:
            return _err(
                "invalid_relationship_predicate",
                (
                    "Invalid relationship predicate. For concept-to-concept relationships, "
                    "use a structural predicate (e.g. 'typeOf', 'instance_of') or a '#V#...' predicate concept id."
                ),
                details={"predicate_input": predicate, "predicate_canonical": rel_kind},
            )

        # For structural predicates, maintain inverse consistency like the HTTP API.
        inverse_map = {
            "is_a_type_of": ("has_subtype", target, source_id),
            "has_subtype": ("is_a_type_of", target, source_id),
            "is_an_instance_of": ("has_instance", target, source_id),
            "has_instance": ("is_an_instance_of", target, source_id),
            "related_to": ("related_to", target, source_id),
        }
        has_inverse = isinstance(rel_kind, str) and rel_kind in inverse_map

        # Ensure relationship storage is list-typed for the forward predicate.
        # This prevents $addToSet failures when legacy data stored a single string.
        repo._ensure_relationship_array(source_id, rel_kind)

        forward_exists_before = False
        existing = (
            repo.find_one({"concept_id": source_id}, {f"relationships.{rel_kind}": 1})
            or {}
        )
        rels = existing.get("relationships") or {}
        curr = rels.get(rel_kind)
        if isinstance(curr, list):
            forward_exists_before = target in curr
        elif isinstance(curr, str):
            forward_exists_before = target == curr

        forward_update = repo.update_one(
            {"concept_id": source_id},
            {"$addToSet": {f"relationships.{rel_kind}": target}},
        )

        inverse_payload = None
        inverse_failed = None
        if has_inverse:
            inv_kind, inv_src, inv_tgt = inverse_map[rel_kind]
            try:
                repo._ensure_relationship_array(inv_src, inv_kind)
                inverse_update = repo.update_one(
                    {"concept_id": inv_src},
                    {"$addToSet": {f"relationships.{inv_kind}": inv_tgt}},
                )
                if (
                    inverse_update.matched_count > 0
                    and inverse_update.modified_count == 0
                ):
                    # Defensive verification: if we didn't modify anything, ensure the
                    # inverse edge is actually present (it may have already existed or
                    # been added concurrently).
                    inv_existing = (
                        repo.find_one(
                            {"concept_id": inv_src},
                            {f"relationships.{inv_kind}": 1},
                        )
                        or {}
                    )
                    inv_rels = inv_existing.get("relationships") or {}
                    inv_curr = inv_rels.get(inv_kind)
                    inverse_present_after = False
                    if isinstance(inv_curr, list):
                        inverse_present_after = inv_tgt in inv_curr
                    elif isinstance(inv_curr, str):
                        inverse_present_after = inv_tgt == inv_curr
                    if not inverse_present_after:
                        inverse_failed = {
                            "predicate": inv_kind,
                            "source_id": inv_src,
                            "target": inv_tgt,
                            "exception_type": "InverseNoOp",
                            "error": "Inverse relationship update did not persist",
                        }
                inverse_payload = {
                    "predicate": inv_kind,
                    "source_id": inv_src,
                    "target": inv_tgt,
                    "added": inverse_update.modified_count > 0,
                    "matched": inverse_update.matched_count > 0,
                }
            except Exception as exc:
                inverse_failed = {
                    "predicate": inv_kind,
                    "source_id": inv_src,
                    "target": inv_tgt,
                    "exception_type": type(exc).__name__,
                    "error": str(exc),
                }

        forward_ok = forward_update.matched_count > 0
        forward_added = forward_update.modified_count > 0

        if forward_ok and not forward_added and not forward_exists_before:
            # Defensive verification: if we didn't modify anything and the edge did
            # not appear to exist pre-write, confirm the edge exists post-write.
            # This prevents returning success when the update is effectively a no-op.
            verify = (
                repo.find_one(
                    {"concept_id": source_id},
                    {f"relationships.{rel_kind}": 1},
                )
                or {}
            )
            verify_rels = verify.get("relationships") or {}
            verify_curr = verify_rels.get(rel_kind)
            present_after = False
            if isinstance(verify_curr, list):
                present_after = target in verify_curr
            elif isinstance(verify_curr, str):
                present_after = target == verify_curr
            if present_after:
                forward_exists_before = True
            else:
                return _err(
                    "relationship_add_noop",
                    "Relationship update did not persist",
                    details={
                        "source_id": source_id,
                        "predicate": rel_kind,
                        "target": target,
                        "matched": forward_update.matched_count > 0,
                        "modified": forward_update.modified_count > 0,
                    },
                )

        if not forward_ok:
            return _err(
                "relationship_add_failed",
                "Failed to add relationship",
                details={
                    "source_id": source_id,
                    "predicate": rel_kind,
                    "target": target,
                },
            )

        if inverse_failed:
            return _err(
                "relationship_add_partial_failure",
                "Relationship added, but inverse relationship update failed",
                details={
                    "forward": {
                        "source_id": source_id,
                        "predicate": rel_kind,
                        "target": target,
                        "added": forward_added,
                        "already_existed": bool(
                            forward_exists_before and not forward_added
                        ),
                    },
                    "inverse_failed": inverse_failed,
                },
            )

        response: dict[str, Any] = {
            "success": True,
            "source_id": source_id,
            "predicate": rel_kind,
            "target": target,
            "added": forward_added,
            "already_existed": bool(forward_exists_before and not forward_added),
        }
        if inverse_payload:
            response["inverse"] = inverse_payload
        return response

    except Exception as e:
        return _err(
            "exception",
            f"Exception: {str(e)}",
            details={
                "source_id": source_id,
                "predicate": predicate,
                "target": target,
                "exception_type": type(e).__name__,
            },
        )


def _remove_relationship(**kwargs):
    """Remove a relationship between two concepts. Text relations removal is not supported here."""
    from ...db.repositories.concepts_repository import ConceptsRepository

    def _err(
        code: str,
        message: str,
        *,
        details: dict | None = None,
    ) -> dict:
        # Keep backward compatibility: preserve the top-level 'error' string.
        return {
            "success": False,
            "error": message,
            "error_code": code,
            "error_details": details or {},
        }

    source_id = kwargs.get("source_id")
    predicate = kwargs.get("predicate")
    target = kwargs.get("target")

    if not source_id:
        return _err(
            "missing_parameter",
            "Missing 'source_id' parameter",
            details={"missing": ["source_id"]},
        )
    if not predicate:
        return _err(
            "missing_parameter",
            "Missing 'predicate' parameter",
            details={"missing": ["predicate"]},
        )
    if not target:
        return _err(
            "missing_parameter",
            "Missing 'target' parameter",
            details={"missing": ["target"]},
        )

    try:
        repo = ConceptsRepository

        from ...vontology.code_concepts_registry import (
            build_virtual_concept_doc,
            is_code_concept_id,
        )
        from ...vontology.utils_vontology import is_predicate

        # Verify source concept exists
        src = repo.find_one({"concept_id": source_id})
        if not src:
            return _err(
                "source_concept_not_found",
                f"Source concept '{source_id}' not found",
                details={"role": "source", "concept_id": source_id},
            )

        # This tool only supports concept-to-concept relationships. Provide an
        # explicit, agent-readable error when users attempt to remove text relations.
        predicate_str = (
            predicate.strip() if isinstance(predicate, str) else str(predicate)
        )
        predicate_normalised = (
            predicate_str[3:] if predicate_str.startswith("#V#") else predicate_str
        )
        well_known_text_predicates = {"hasContent", "hasDescription", "hasName"}
        if predicate_normalised in well_known_text_predicates:
            return _err(
                "unsupported_text_relation_removal",
                "Text relation removal is not supported by remove_relationship",
                details={
                    "predicate_input": predicate,
                    "predicate": predicate_normalised,
                },
            )

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

        # Determine if this is a text predicate (binary_text_predicate instance)
        if isinstance(rel_kind, str) and rel_kind.startswith("#V#"):
            pred_doc = repo.find_one(
                {"concept_id": rel_kind}, {"relationships.is_an_instance_of": 1}
            )
            if pred_doc:
                instance_of = pred_doc.get("relationships", {}).get(
                    "is_an_instance_of", []
                )
                if isinstance(instance_of, str):
                    instance_of = [instance_of]
                if "#V#binary_text_predicate" in instance_of:
                    return _err(
                        "unsupported_text_relation_removal",
                        "Text relation removal is not supported by remove_relationship",
                        details={"predicate": rel_kind, "predicate_input": predicate},
                    )

        # Guardrail: concept-to-concept relationship predicates must be either:
        # - one of the structural relationship kinds (is_a_type_of, has_subtype, ...), or
        # - a Vontology predicate concept id (starts with #V# and exists as a predicate).
        from ...db.repositories.concepts_repository import RELATIONSHIP_KINDS

        if isinstance(rel_kind, str) and rel_kind in RELATIONSHIP_KINDS:
            pass
        elif isinstance(rel_kind, str) and rel_kind.startswith("#V#"):
            pred_doc = repo.find_one(
                {"concept_id": rel_kind}, {"concept_id": 1, "relationships": 1}
            )
            if pred_doc is None and is_code_concept_id(rel_kind):
                pred_doc = build_virtual_concept_doc(rel_kind)
            if pred_doc is None:
                return _err(
                    "predicate_concept_not_found",
                    (
                        f"Predicate concept '{rel_kind}' not found. "
                        "Create it as a predicate in the Vontology (e.g. as an instance of #V#predicate) "
                        "before using it as a relationship."
                    ),
                    details={
                        "role": "predicate",
                        "predicate": rel_kind,
                        "predicate_input": predicate,
                    },
                )
            if not is_predicate(pred_doc):
                return _err(
                    "predicate_concept_not_typed",
                    (
                        f"Concept '{rel_kind}' exists but is not typed as a predicate. "
                        "Predicates must be instances of #V#predicate (or a predicate subtype)."
                    ),
                    details={
                        "role": "predicate",
                        "predicate": rel_kind,
                        "predicate_input": predicate,
                        "required_instance_of": "#V#predicate",
                    },
                )
        else:
            return _err(
                "invalid_relationship_predicate",
                (
                    "Invalid relationship predicate. For concept-to-concept relationships, "
                    "use a structural predicate (e.g. 'typeOf', 'instance_of') or a '#V#...' predicate concept id."
                ),
                details={"predicate_input": predicate, "predicate_canonical": rel_kind},
            )

        # Ensure the relationship field exists (optional sanity)
        existing = (
            repo.find_one({"concept_id": source_id}, {f"relationships.{rel_kind}": 1})
            or {}
        )
        rels = existing.get("relationships") or {}
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
        return _err(
            "exception",
            f"Exception: {str(e)}",
            details={
                "source_id": source_id,
                "predicate": predicate,
                "target": target,
                "exception_type": type(e).__name__,
            },
        )


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

    return _run_async_compat(_async_search)


def _download_paper(**kwargs):
    import asyncio
    from .arxiv_proxy_mcp import get_arxiv_proxy, ArxivProxyError

    arxiv_id = kwargs.get("arxiv_id")
    if not arxiv_id:
        return {"error": "Missing required parameter: arxiv_id", "success": False}

    # If the PDF is already present in the local arXiv cache, prefer finalise_cached_paper so
    # we can upload to durable storage and register the Computer File Copy without calling
    # the upstream MCP server again.
    try:
        import os
        from pathlib import Path

        from src.backend.security.access_control import get_effective_user_concept_id

        from .arxiv_proxy_mcp import _find_cached_pdf_for_arxiv_id

        user_concept_id = get_effective_user_concept_id()
        if user_concept_id:
            workspace_root = Path(__file__).parent.parent.parent.parent.parent
            storage_path = workspace_root / "data" / "arxiv_cache"
            env_storage = os.environ.get("ARXIV_CACHE_PATH") or os.environ.get(
                "ARXIV_STORAGE_PATH"
            )
            if env_storage:
                storage_path = Path(env_storage)

            cached = _find_cached_pdf_for_arxiv_id(storage_path, str(arxiv_id))
            if cached is not None:
                finalised = _finalise_cached_paper(
                    arxiv_id=arxiv_id,
                    name=kwargs.get("filename"),
                    delete_local_cache=kwargs.get("delete_local_cache"),
                )
                if isinstance(finalised, dict) and finalised.get("success") is True:
                    return finalised
    except Exception:
        # Best-effort: if anything goes wrong with cache detection/finalisation,
        # fall back to the normal download behaviour.
        pass

    async def _async_download():
        try:
            proxy = await get_arxiv_proxy()
            stored = await proxy.download_paper(
                arxiv_id=arxiv_id,
                filename=kwargs.get("filename"),
            )

            # If authenticated, always register the Computer File Copy (even on a fresh
            # download). The proxy already stores the PDF in durable blob storage.
            try:
                from pathlib import Path

                from src.backend.security.access_control import (
                    get_effective_user_concept_id,
                )
                from src.backend.services.computer_file_copy_service import (
                    create_computer_file_copy_instance,
                )

                user_concept_id = get_effective_user_concept_id()
                if (
                    user_concept_id
                    and isinstance(stored, dict)
                    and stored.get("success") is True
                ):
                    storage = stored.get("storage")
                    if isinstance(storage, dict):
                        record = create_computer_file_copy_instance(
                            type_concept_id="#V#arxiv_pdf_file",
                            user_concept_id=str(user_concept_id),
                            name=str(
                                kwargs.get("filename")
                                or Path(str(stored.get("file_path") or "")).name
                                or f"{arxiv_id}.pdf"
                            ),
                            sha256=str(stored.get("sha256") or ""),
                            size_bytes=int(stored.get("size_bytes") or 0),
                            content_type="application/pdf",
                            blob_backend=str(storage.get("backend") or ""),
                            blob_key=str(storage.get("key") or ""),
                            blob_uri=str(storage.get("uri") or ""),
                            metadata={
                                "source": "arxiv",
                                "arxiv_id": str(stored.get("arxiv_id") or arxiv_id),
                                "original_path": str(stored.get("file_path") or ""),
                            },
                        )
                        stored = dict(stored)
                        stored["computer_file_copy_concept_id"] = record.concept_id
                        stored["uploaded_at"] = record.uploaded_at

                        # Best-effort: link this file copy to a stable Paper-on-arXiv instance.
                        try:
                            from src.backend.services.arxiv_paper_link_service import (
                                link_file_copy_to_arxiv_paper,
                            )

                            link_result = link_file_copy_to_arxiv_paper(
                                user_concept_id=str(user_concept_id),
                                arxiv_id=str(stored.get("arxiv_id") or arxiv_id),
                                file_copy_concept_id=str(record.concept_id),
                            )
                            if isinstance(link_result, dict) and link_result.get(
                                "paper_concept_id"
                            ):
                                stored["paper_concept_id"] = link_result.get(
                                    "paper_concept_id"
                                )
                        except Exception:
                            pass

                    # Best-effort cache cleanup (delete local cached PDF) after durable upload.
                    delete_local_cache = kwargs.get("delete_local_cache")
                    if delete_local_cache is None:
                        delete_local_cache = True
                    if bool(delete_local_cache):
                        try:
                            import os

                            workspace_root = Path(
                                __file__
                            ).parent.parent.parent.parent.parent
                            cache_root = workspace_root / "data" / "arxiv_cache"
                            env_storage = os.environ.get(
                                "ARXIV_CACHE_PATH"
                            ) or os.environ.get("ARXIV_STORAGE_PATH")
                            if env_storage:
                                cache_root = Path(env_storage)

                            cached_path = Path(str(stored.get("file_path") or ""))
                            local_deleted = False
                            local_error = None
                            try:
                                resolved_cache = cache_root.resolve()
                                resolved_file = cached_path.resolve()
                                if (
                                    resolved_cache in resolved_file.parents
                                    and resolved_file.is_file()
                                ):
                                    resolved_file.unlink()
                                    local_deleted = True
                            except Exception as exc:  # pragma: no cover - best effort
                                local_error = str(exc)

                            stored = dict(stored)
                            stored["local_cache_deleted"] = local_deleted
                            if local_error:
                                stored["local_cache_delete_error"] = local_error
                        except Exception:  # pragma: no cover - best effort
                            pass

            except Exception:
                # Defensive: registration/cleanup must not break the download itself.
                pass

            return stored
        except ArxivProxyError as e:
            return {"error": str(e), "success": False}
        except Exception as e:
            return {"error": f"Unexpected error: {e}", "success": False}

    return _run_async_compat(_async_download)


def _finalise_cached_paper(**kwargs):
    """Upload an already-cached arXiv PDF to the blob store and register a Computer File Copy."""

    arxiv_id = kwargs.get("arxiv_id")
    if not arxiv_id:
        return {"error": "Missing required parameter: arxiv_id", "success": False}

    from src.backend.services.blob_uploads import BlobUploadError, put_bytes_durable

    try:
        import hashlib
        import os
        from pathlib import Path

        from src.backend.security.access_control import get_effective_user_concept_id
        from src.backend.services.computer_file_copy_service import (
            create_computer_file_copy_instance,
        )

        from .arxiv_proxy_mcp import (
            _arxiv_markdown_blob_key,
            _arxiv_pdf_blob_key,
            _find_cached_markdown_for_arxiv_id,
            _find_cached_pdf_for_arxiv_id,
            _normalise_arxiv_id,
        )

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return {
                "success": False,
                "error": "not_authenticated",
                "message": "Authentication required to register file copies.",
            }

        workspace_root = Path(__file__).parent.parent.parent.parent.parent
        storage_path = workspace_root / "data" / "arxiv_cache"
        env_storage = os.environ.get("ARXIV_CACHE_PATH") or os.environ.get(
            "ARXIV_STORAGE_PATH"
        )
        if env_storage:
            storage_path = Path(env_storage)

        cached = _find_cached_pdf_for_arxiv_id(storage_path, str(arxiv_id))
        if cached is None:
            return {
                "success": False,
                "error": "cached_pdf_not_found",
                "message": f"No cached arXiv PDF found for {arxiv_id} under {storage_path}",
            }

        data = cached.read_bytes()
        size_bytes = len(data)
        sha256 = hashlib.sha256(data).hexdigest()
        storage_key = _arxiv_pdf_blob_key(str(arxiv_id))
        stable_id = _normalise_arxiv_id(str(arxiv_id))

        stored = put_bytes_durable(
            key=storage_key,
            data=data,
            content_type="application/pdf",
            sha256=sha256,
            size_bytes=size_bytes,
            metadata={
                "source": "arxiv",
                "arxiv_id": stable_id,
                "original_path": str(cached),
            },
        )

        ref = stored.ref

        record = create_computer_file_copy_instance(
            type_concept_id="#V#arxiv_pdf_file",
            user_concept_id=str(user_concept_id),
            name=str(kwargs.get("name") or cached.name),
            sha256=sha256,
            size_bytes=size_bytes,
            content_type="application/pdf",
            blob_backend=str(ref.backend),
            blob_key=str(ref.key),
            blob_uri=str(ref.uri),
            metadata={
                "source": "arxiv",
                "arxiv_id": stable_id,
                "original_path": str(cached),
            },
        )

        paper_concept_id = None
        try:
            from src.backend.services.arxiv_paper_link_service import (
                link_file_copy_to_arxiv_paper,
            )

            link_result = link_file_copy_to_arxiv_paper(
                user_concept_id=str(user_concept_id),
                arxiv_id=stable_id,
                file_copy_concept_id=str(record.concept_id),
            )
            if isinstance(link_result, dict):
                paper_concept_id = link_result.get("paper_concept_id")
        except Exception:
            paper_concept_id = None

        include_markdown = kwargs.get("include_markdown")
        if include_markdown is None:
            include_markdown = True

        markdown_payload: dict[str, Any] | None = None
        markdown_cached: Path | None = None
        markdown_local_deleted = False
        markdown_local_error = None

        if include_markdown:
            markdown_payload = {"status": "not_found"}
            try:
                markdown_cached = _find_cached_markdown_for_arxiv_id(
                    storage_path, str(arxiv_id)
                )
                if markdown_cached is not None:
                    markdown_data = markdown_cached.read_bytes()
                    markdown_size = len(markdown_data)
                    markdown_sha256 = hashlib.sha256(markdown_data).hexdigest()
                    markdown_storage_key = _arxiv_markdown_blob_key(str(arxiv_id))

                    markdown_stored = put_bytes_durable(
                        key=markdown_storage_key,
                        data=markdown_data,
                        content_type="text/markdown",
                        sha256=markdown_sha256,
                        size_bytes=markdown_size,
                        metadata={
                            "source": "arxiv",
                            "arxiv_id": stable_id,
                            "original_path": str(markdown_cached),
                            "format": "markdown",
                        },
                    )

                    markdown_ref = markdown_stored.ref
                    markdown_record = create_computer_file_copy_instance(
                        type_concept_id="#V#arxiv_markdown_file",
                        user_concept_id=str(user_concept_id),
                        name=str(markdown_cached.name),
                        sha256=markdown_sha256,
                        size_bytes=markdown_size,
                        content_type="text/markdown",
                        blob_backend=str(markdown_ref.backend),
                        blob_key=str(markdown_ref.key),
                        blob_uri=str(markdown_ref.uri),
                        metadata={
                            "source": "arxiv",
                            "arxiv_id": stable_id,
                            "original_path": str(markdown_cached),
                            "format": "markdown",
                        },
                    )

                    try:
                        from src.backend.services.arxiv_paper_link_service import (
                            link_file_copy_to_arxiv_paper,
                        )

                        link_result = link_file_copy_to_arxiv_paper(
                            user_concept_id=str(user_concept_id),
                            arxiv_id=stable_id,
                            file_copy_concept_id=str(markdown_record.concept_id),
                        )
                        if (
                            paper_concept_id is None
                            and isinstance(link_result, dict)
                            and link_result.get("paper_concept_id")
                        ):
                            paper_concept_id = link_result.get("paper_concept_id")
                    except Exception:
                        pass

                    markdown_payload = {
                        "status": "uploaded",
                        "file_path": str(markdown_cached),
                        "size_bytes": markdown_size,
                        "sha256": markdown_sha256,
                        "storage": {
                            "backend": markdown_ref.backend,
                            "key": markdown_ref.key,
                            "uri": markdown_ref.uri,
                        },
                        "computer_file_copy_concept_id": markdown_record.concept_id,
                        "uploaded_at": markdown_record.uploaded_at,
                        "local_cache_deleted": False,
                        "local_cache_delete_error": None,
                    }
            except Exception as exc:
                markdown_payload = {"status": "error", "error": str(exc)}
        else:
            markdown_payload = {"status": "skipped"}

        # Best-effort cache cleanup (delete local cached PDF) after durable upload.
        delete_local_cache = kwargs.get("delete_local_cache")
        if delete_local_cache is None:
            delete_local_cache = True
        local_deleted = False
        local_error = None
        if bool(delete_local_cache):
            try:
                resolved_cache_root = storage_path.resolve()
                resolved_cached = cached.resolve()
                if (
                    resolved_cache_root in resolved_cached.parents
                    and resolved_cached.is_file()
                ):
                    resolved_cached.unlink()
                    local_deleted = True
            except Exception as exc:  # pragma: no cover - best effort
                local_error = str(exc)
        if bool(delete_local_cache) and markdown_cached is not None:
            try:
                resolved_cache_root = storage_path.resolve()
                resolved_markdown = markdown_cached.resolve()
                if (
                    resolved_cache_root in resolved_markdown.parents
                    and resolved_markdown.is_file()
                ):
                    resolved_markdown.unlink()
                    markdown_local_deleted = True
            except Exception as exc:  # pragma: no cover - best effort
                markdown_local_error = str(exc)

        if markdown_payload is not None:
            markdown_payload["local_cache_deleted"] = markdown_local_deleted
            markdown_payload["local_cache_delete_error"] = markdown_local_error

        return {
            "success": True,
            "arxiv_id": stable_id,
            "file_path": str(cached),
            "size_bytes": size_bytes,
            "sha256": sha256,
            "storage": {
                "backend": ref.backend,
                "key": ref.key,
                "uri": ref.uri,
            },
            "computer_file_copy_concept_id": record.concept_id,
            "paper_concept_id": paper_concept_id,
            "uploaded_at": record.uploaded_at,
            "local_cache_deleted": local_deleted,
            "local_cache_delete_error": local_error,
            "markdown": markdown_payload,
        }
    except BlobUploadError as exc:
        return {"success": False, "error": f"blob_store_upload_failed: {exc}"}
    except Exception as exc:
        return {"success": False, "error": f"Unexpected error: {exc}"}


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

    return _run_async_compat(_async_list)


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

    return _run_async_compat(_async_read)


# Blob/file-copy retrieval
def _read_file_copy(**kwargs):
    import os
    from ...security.access_control import (
        get_effective_user_concept_id,
        override_current_user,
        override_current_organisation,
    )
    from ...services.computer_file_copy_service import fetch_file_copy_bytes

    concept_id = kwargs.get("concept_id") or kwargs.get("file_copy_concept_id")
    if not isinstance(concept_id, str) or not concept_id.strip():
        return {"error": "Missing required parameter: concept_id", "success": False}

    max_bytes = kwargs.get("max_bytes")
    if max_bytes is None:
        max_bytes = 5_000_000
    else:
        try:
            max_bytes = int(max_bytes)
        except (TypeError, ValueError):
            return {"error": "Invalid max_bytes", "success": False}
        if max_bytes <= 0:
            return {"error": "max_bytes must be positive", "success": False}

    as_text = kwargs.get("as_text")
    if as_text is None:
        as_text = True
    encoding = kwargs.get("encoding") or "utf-8"
    allow_large = bool(kwargs.get("allow_large", False))

    if allow_large:
        try:
            max_override = int(
                os.environ.get("VON_READ_FILE_COPY_MAX_BYTES_OVERRIDE", "20000000")
            )
        except Exception:
            max_override = 20_000_000
        if max_override <= 0:
            max_override = 20_000_000
        if max_bytes is None or max_bytes > max_override:
            max_bytes = max_override

    namespace = kwargs.get("namespace")
    user_part = None
    org_part = None
    if isinstance(namespace, str) and "@" in namespace:
        user_part, org_part = namespace.split("@", 1)
        user_part = (user_part or "").strip() or None
        org_part = (org_part or "").strip() or None
        if org_part and not org_part.startswith("#V#"):
            org_part = f"#V#{org_part}"
    elif isinstance(namespace, str) and namespace.strip():
        user_part = namespace.strip()

    def _run_read():
        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return {"error": "missing_user_context", "success": False}

        result = fetch_file_copy_bytes(
            file_copy_concept_id=concept_id,
            max_bytes=max_bytes,
            allow_large=allow_large,
        )
        if not isinstance(result, dict) or result.get("success") is not True:
            error = result.get("error") if isinstance(result, dict) else "not_found"
            payload = {"success": False, "error": error, "concept_id": concept_id}
            if error == "file_too_large":
                payload["size_bytes"] = result.get("size_bytes")
                payload["max_bytes"] = result.get("max_bytes")
                payload["limit_override_env"] = "VON_READ_FILE_COPY_MAX_BYTES_OVERRIDE"
                payload["limit_override_hint"] = (
                    "Set VON_READ_FILE_COPY_MAX_BYTES_OVERRIDE to raise the maximum file size for read_file_copy."
                )
            return payload

        info = result.get("info")
        data_bytes = result.get("data") or b""

        payload = {
            "success": True,
            "concept_id": getattr(info, "concept_id", concept_id),
            "original_filename": getattr(info, "original_filename", None),
            "content_type": getattr(info, "content_type", None),
            "size_bytes": getattr(info, "size_bytes", None),
            "byte_length": len(data_bytes),
            "blob": {
                "backend": getattr(info, "blob_backend", None),
                "key": getattr(info, "blob_key", None),
                "uri": getattr(info, "blob_uri", None),
            },
        }

        if as_text:
            is_pdf = False
            try:
                content_type = getattr(info, "content_type", None)
                original_name = getattr(info, "original_filename", None)
                if isinstance(content_type, str) and content_type.lower().startswith(
                    "application/pdf"
                ):
                    is_pdf = True
                elif isinstance(original_name, str) and original_name.lower().endswith(
                    ".pdf"
                ):
                    is_pdf = True
            except Exception:
                is_pdf = False

            if is_pdf:
                text: str | None = None
                extraction_method: str | None = None
                extraction_error: str | None = None

                try:
                    import fitz  # type: ignore[import-not-found] # PyMuPDF

                    doc = fitz.open(stream=bytes(data_bytes), filetype="pdf")
                    extracted_pages: list[str] = []
                    for page in doc:
                        extracted_pages.append(page.get_text("text"))
                    text = "\n".join(extracted_pages).strip()
                    extraction_method = "pymupdf"
                    if not text:
                        try:
                            import pytesseract  # type: ignore[import-not-found]
                            from PIL import Image  # type: ignore[import-not-found]

                            ocr_pages: list[str] = []
                            for page in doc:
                                pix = page.get_pixmap(dpi=200)
                                img = Image.frombytes(
                                    "RGB",
                                    (pix.width, pix.height),
                                    pix.samples,
                                )
                                ocr_pages.append(pytesseract.image_to_string(img))
                            ocr_text = "\n".join(ocr_pages).strip()
                            if ocr_text:
                                text = ocr_text
                                extraction_method = "pymupdf_ocr"
                        except Exception as exc:
                            extraction_error = str(exc)
                except ModuleNotFoundError as exc:
                    extraction_method = "pymupdf_missing"
                    extraction_error = str(exc)
                except Exception as exc:
                    extraction_method = "pymupdf_failed"
                    extraction_error = str(exc)

                if not text:
                    try:
                        import io
                        from pdfminer.high_level import extract_text  # type: ignore[import-not-found]

                        text = extract_text(io.BytesIO(bytes(data_bytes))).strip()
                        if text:
                            extraction_method = "pdfminer"
                            extraction_error = None
                    except Exception as exc:
                        if extraction_method is None:
                            extraction_method = "pdfminer_failed"
                            extraction_error = str(exc)
                        elif extraction_error:
                            extraction_error = f"{extraction_error}; pdfminer: {exc}"
                        else:
                            extraction_error = str(exc)

                if text:
                    payload["text"] = text
                    payload["encoding"] = "utf-8"
                    if extraction_method:
                        payload["text_extraction"] = extraction_method
                else:
                    if extraction_method:
                        payload["text_extraction"] = extraction_method
                    if extraction_error:
                        payload["text_extraction_error"] = extraction_error
                    try:
                        text = bytes(data_bytes).decode(str(encoding), errors="replace")
                    except LookupError:
                        return {"success": False, "error": "invalid_encoding"}
                    payload["text"] = text
                    payload["encoding"] = str(encoding)
            else:
                try:
                    content_type = getattr(info, "content_type", None)
                    original_name = getattr(info, "original_filename", None)
                    is_image = False
                    if isinstance(
                        content_type, str
                    ) and content_type.lower().startswith("image/"):
                        is_image = True
                    elif isinstance(
                        original_name, str
                    ) and original_name.lower().endswith(
                        (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif")
                    ):
                        is_image = True

                    if is_image:
                        try:
                            import pytesseract  # type: ignore[import-not-found]
                            from PIL import Image  # type: ignore[import-not-found]
                            import io

                            image = Image.open(io.BytesIO(bytes(data_bytes)))
                            ocr_text = pytesseract.image_to_string(image).strip()
                            payload["text"] = ocr_text
                            payload["encoding"] = "utf-8"
                            payload["text_extraction"] = "image_ocr"
                        except Exception as exc:
                            payload["text_extraction_error"] = str(exc)
                            text = bytes(data_bytes).decode(
                                str(encoding), errors="replace"
                            )
                            payload["text"] = text
                            payload["encoding"] = str(encoding)
                    else:
                        text = bytes(data_bytes).decode(str(encoding), errors="replace")
                        payload["text"] = text
                        payload["encoding"] = str(encoding)
                except LookupError:
                    return {"success": False, "error": "invalid_encoding"}
        else:
            import base64

            payload["bytes_base64"] = base64.b64encode(bytes(data_bytes)).decode(
                "ascii"
            )
            payload["bytes_base64_encoding"] = "base64"

        return payload

    if user_part or org_part:
        with override_current_user(user_part), override_current_organisation(org_part):
            return _run_read()

    return _run_read()


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

    return _run_async_compat(_async_search)


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

    return _run_async_compat(_async_context_search)


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

    return _run_async_compat(_async_qna_search)


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

    return _run_async_compat(_async_extract)


def _resilient_extract_url(**kwargs):
    """Extract content from a URL with deterministic fallbacks.

    This is intended to handle common failure modes of `extract_url`, especially
    JavaScript-rendered profile pages that return empty/blocked content. The
    handler first attempts direct extraction. If that fails, it performs a web
    search and attempts extraction on a small number of candidate URLs.
    """

    from urllib.parse import unquote, urlparse
    import re

    primary_url = kwargs.get("url")
    if not primary_url:
        return {"error": "Missing required parameter: url", "success": False}

    fallback_query = kwargs.get("fallback_query")
    context = kwargs.get("context")

    max_fallback_results = int(kwargs.get("max_fallback_results", 5) or 5)
    max_extracts = int(kwargs.get("max_extracts", 4) or 4)
    max_chars = int(kwargs.get("max_chars", 12000) or 12000)
    min_content_chars = int(kwargs.get("min_content_chars", 200) or 200)
    search_depth = kwargs.get("search_depth", "basic")
    include_domains = kwargs.get("include_domains")
    exclude_domains = kwargs.get("exclude_domains")

    def _safe_text(value):
        if value is None:
            return ""
        if not isinstance(value, str):
            return str(value)
        return value

    def _truncate(text: str) -> str:
        if max_chars <= 0:
            return text

        if len(text) <= max_chars:
            return text
        return text[:max_chars]

    def _summarise_attempt(result: dict, attempted_url: str) -> dict:
        content = _safe_text(result.get("content"))
        title = result.get("title")
        return {
            "url": attempted_url,
            "success": bool(result.get("success")) and bool(content.strip()),
            "title": title,
            "content_chars": len(content),
            "error": result.get("error"),
        }

    def _derive_query(url: str) -> str:
        parsed = urlparse(url)
        host = parsed.netloc or ""
        last_segment = parsed.path.rstrip("/").split("/")[-1]
        last_segment = unquote(last_segment)
        last_segment = re.sub(r"[-_]+", " ", last_segment)
        last_segment = re.sub(r"\s+", " ", last_segment).strip()
        if last_segment and host:
            return f"{last_segment} {host}"
        if last_segment:
            return last_segment
        return host or url

    attempted: list[dict] = []

    primary_result = _extract_url(url=primary_url)
    attempted.append(_summarise_attempt(primary_result, primary_url))
    primary_content = _safe_text(primary_result.get("content"))
    if bool(primary_result.get("success")) and primary_content.strip():
        return {
            "success": True,
            "primary_url": primary_url,
            "extracted_from_url": primary_url,
            "title": primary_result.get("title"),
            "content": _truncate(primary_content),
            "attempted": attempted,
            "search": {
                "attempted": False,
                "query": None,
                "used_context": False,
                "results_count": 0,
                "candidate_urls": [],
            },
        }

    if not isinstance(fallback_query, str) or not fallback_query.strip():
        fallback_query = _derive_query(primary_url)

    if context:
        search_result = _context_search(
            query=fallback_query,
            context=context,
            max_results=max_fallback_results,
            search_depth=search_depth,
            include_answer=False,
        )
        used_context = True
    else:
        search_result = _search_web(
            query=fallback_query,
            max_results=max_fallback_results,
            search_depth=search_depth,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            include_answer=False,
            include_raw_content=False,
            include_images=False,
        )
        used_context = False

    results = search_result.get("results") or []
    candidate_urls: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        candidate_url = item.get("url")
        if not isinstance(candidate_url, str) or not candidate_url:
            continue
        if candidate_url == primary_url:
            continue
        if candidate_url in candidate_urls:
            continue
        candidate_urls.append(candidate_url)

    best_payload: dict | None = None
    best_score: float | None = None

    for candidate_url in candidate_urls[: max(0, max_extracts)]:
        candidate_result = _extract_url(url=candidate_url)
        attempted.append(_summarise_attempt(candidate_result, candidate_url))
        candidate_content = _safe_text(candidate_result.get("content")).strip()
        if not (bool(candidate_result.get("success")) and candidate_content):
            continue
        if len(candidate_content) < min_content_chars:
            continue

        score = float(len(candidate_content))
        lowered = candidate_content.lower()
        for needle in (
            "biography",
            "research",
            "publications",
            "education",
            "university",
        ):
            if needle in lowered:
                score += 250.0

        if best_score is None or score > best_score:
            best_score = score
            best_payload = {
                "url": candidate_url,
                "title": candidate_result.get("title"),
                "content": candidate_content,
            }

    if best_payload is not None:
        return {
            "success": True,
            "primary_url": primary_url,
            "extracted_from_url": best_payload.get("url"),
            "title": best_payload.get("title"),
            "content": _truncate(_safe_text(best_payload.get("content"))),
            "attempted": attempted,
            "search": {
                "attempted": True,
                "query": fallback_query,
                "used_context": used_context,
                "results_count": len(results) if isinstance(results, list) else 0,
                "candidate_urls": candidate_urls,
            },
        }

    error_message = primary_result.get("error")
    if not error_message and isinstance(search_result, dict):
        error_message = search_result.get("error")
    if not error_message:
        error_message = "No extractable fallback sources found"

    return {
        "success": False,
        "primary_url": primary_url,
        "extracted_from_url": None,
        "title": None,
        "content": None,
        "attempted": attempted,
        "search": {
            "attempted": True,
            "query": fallback_query,
            "used_context": used_context,
            "results_count": len(results) if isinstance(results, list) else 0,
            "candidate_urls": candidate_urls,
        },
        "error": error_message,
    }


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
            return {
                "success": True,
                "concept_id": concept_id,
                "updated_fields": list(update_data.keys()),
            }
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
            "namespace": (str, type(None)),
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


def _upsert_text_relation_input_schema() -> Schema:
    return Schema(
        required={
            "concept_id": str,
            "predicate": str,
            "text": str,
        },
        optional={
            "namespace": (str, type(None)),
            "language": str,
            "context": (dict, type(None)),
        },
        allow_unknown=True,
        description="upsert_text_relation input: concept_id (str), predicate (str, e.g., 'hasContent', 'hasDescription'), text (str), namespace (str, optional), language (str, optional, default 'en-NZ'), context (dict, optional metadata)",
    )


def _upsert_text_relation_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
        },
        optional={
            "text_value_id": (str, type(None)),
            "relation_id": (str, type(None)),
            "relation_created": (bool, type(None)),
            "predicate": (str, type(None)),
            "text_preview": (str, type(None)),
            "language": (str, type(None)),
            "error": (str, type(None)),
        },
        allow_unknown=True,
        description="upsert_text_relation output: success (bool), text_value_id (str), relation_id (str), relation_created (bool), predicate (str), text_preview (str), language (str), error (str if failed)",
    )


def _get_text_relations_input_schema() -> Schema:
    return Schema(
        required={
            "concept_id": str,
        },
        optional={
            "predicate": (str, type(None)),
            "language": (str, type(None)),
            "limit": (int, type(None)),
        },
        allow_unknown=True,
        description="get_text_relations input: concept_id (str), predicate (str, optional filter), language (str, optional filter), limit (int, optional, default 50)",
    )


def _get_text_relations_output_schema() -> Schema:
    return Schema(
        required={
            "concept_id": str,
            "relations_found": int,
        },
        optional={
            "relations": (list, type(None)),
            "error": (str, type(None)),
        },
        allow_unknown=True,
        description="get_text_relations output: concept_id (str), relations_found (int), relations (list of {text, lang, text_value_id, predicate, relation_id, context, text_preview?}), error (str if failed)",
    )


def _update_text_relation_input_schema() -> Schema:
    return Schema(
        required={
            "concept_id": str,
            "relation_id": str,
            "new_text": str,
        },
        optional={
            "language": (str, type(None)),
        },
        allow_unknown=True,
        description="update_text_relation input: concept_id (str), relation_id (str), new_text (str), language (str, optional)",
    )


def _update_text_relation_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
        },
        optional={
            "relation_id": (str, type(None)),
            "old_text_preview": (str, type(None)),
            "new_text_preview": (str, type(None)),
            "text_value_id": (str, type(None)),
            "error": (str, type(None)),
        },
        allow_unknown=True,
        description="update_text_relation output: success (bool), relation_id (str), old_text_preview (str), new_text_preview (str), text_value_id (str), error (str if failed)",
    )


def _delete_text_relation_input_schema() -> Schema:
    return Schema(
        required={
            "concept_id": str,
        },
        optional={
            "relation_id": (str, type(None)),
            "predicate": (str, type(None)),
            "text": (str, type(None)),
            "language": (str, type(None)),
            "garbage_collect": (bool,),
        },
        allow_unknown=True,
        description="delete_text_relation input: concept_id (str), relation_id (str, optional - preferred method), predicate (str, optional for predicate+text deletion), text (str, optional with predicate), language (str, optional), garbage_collect (bool, optional - if true, delete orphaned text_values)",
    )


def _get_text_relations_summary_input_schema() -> Schema:
    return Schema(
        required={"concept_id": str},
        optional={
            "predicates": (list, type(None)),
            "languages": (list, type(None)),
            "max_relation_ids_per_group": (int, type(None)),
        },
        allow_unknown=True,
        description="get_text_relations_summary input: concept_id (str), predicates (list[str] optional), languages (list[str] optional), max_relation_ids_per_group (int optional, default 25)",
    )


def _get_text_relations_summary_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
            "concept_id": str,
            "groups": list,
            "groups_found": int,
            "total_relations_scanned": int,
            "max_relation_ids_per_group": int,
        },
        optional={"error": (str, type(None))},
        allow_unknown=True,
        description="get_text_relations_summary output: success, concept_id, groups[{predicate, language, count, relation_ids, latest_relation_id, latest_updated_at}], groups_found, total_relations_scanned",
    )


def _upsert_singleton_text_relation_input_schema() -> Schema:
    return Schema(
        required={
            "concept_id": str,
            "predicate": str,
            "text": str,
        },
        optional={
            "language": str,
            "policy": str,
            "garbage_collect": (bool, type(None)),
            "provenance": (dict, type(None)),
            "context": (dict, type(None)),
        },
        allow_unknown=True,
        description="upsert_singleton_text_relation input: concept_id (str), predicate (str), text (str), language (str optional default en-NZ), policy (str optional default replace_others), garbage_collect (bool optional default true), provenance/context (dict optional)",
    )


def _upsert_singleton_text_relation_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
            "concept_id": str,
            "predicate": str,
            "language": str,
            "kept_relation_id": str,
            "replaced_relation_ids": list,
            "replaced_count": int,
            "relation_created": bool,
        },
        optional={
            "text_value_id": (str, type(None)),
            "error": (str, type(None)),
        },
        allow_unknown=True,
        description="upsert_singleton_text_relation output: success, kept_relation_id, replaced_relation_ids/count, relation_created, text_value_id",
    )


def _concept_exists_input_schema() -> Schema:
    return Schema(
        required={"concept_id": str},
        optional={},
        allow_unknown=True,
        description="concept_exists input: concept_id (str)",
    )


def _concept_exists_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
            "concept_id": str,
            "exists": bool,
            "accessible": bool,
        },
        optional={"error": (str, type(None))},
        allow_unknown=True,
        description="concept_exists output: success, concept_id, exists (bool), accessible (bool)",
    )


def _fetch_concept_content_input_schema() -> Schema:
    return Schema(
        required={"concept_id": str},
        optional={"reconstruct_md": (bool, type(None))},
        allow_unknown=True,
        description="fetch_concept_content input: concept_id (str), reconstruct_md (bool optional default true)",
    )


def _delete_text_relation_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
        },
        optional={
            "deleted_relation_id": (str, type(None)),
            "deleted_text_preview": (str, type(None)),
            "text_value_cleaned_up": (bool, type(None)),
            "error": (str, type(None)),
        },
        allow_unknown=True,
        description="delete_text_relation output: success (bool), deleted_relation_id (str), deleted_text_preview (str), text_value_cleaned_up (bool), error (str if failed)",
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


def _resolve_concept_by_name(**kwargs):
    from ...services.concept_resolution_service import resolve_concept_by_name

    name = kwargs.get("name")
    if name is None or not str(name).strip():
        return {
            "success": False,
            "status": "not_found",
            "error": "Missing 'name' parameter",
            "resolved_concept_id": None,
            "candidates": [],
            "audit": [],
        }

    return resolve_concept_by_name(
        name=str(name),
        preferred_languages=kwargs.get("preferred_languages"),
        allowed_languages=kwargs.get("allowed_languages"),
        instance_of=kwargs.get("instance_of"),
        match_code_strings=bool(kwargs.get("match_code_strings", True)),
        normalisation_level=str(kwargs.get("normalisation_level", "default")),
        max_results=int(kwargs.get("max_results", 5)),
    )


def _resolve_concept_by_name_input_schema() -> Schema:
    return Schema(
        required={
            "name": str,
        },
        optional={
            "preferred_languages": (list, type(None)),
            "allowed_languages": (list, type(None)),
            "instance_of": (str, type(None)),
            "match_code_strings": (bool, type(None)),
            "normalisation_level": (str, type(None)),
            "max_results": (int, type(None)),
        },
        allow_unknown=True,
        description=(
            "resolve_concept_by_name input: name (str) plus optional language preferences/constraints, "
            "instance_of restriction, code-string matching toggle, normalisation_level, max_results"
        ),
    )


def _resolve_concept_by_name_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
            "status": str,
            "resolved_concept_id": (str, type(None)),
            "candidates": list,
            "audit": list,
        },
        optional={
            "match": (dict, type(None)),
            "error": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "resolve_concept_by_name output: status in {resolved, ambiguous, not_found} with "
            "resolved_concept_id, optional match info, candidates (for ambiguous), and audit steps"
        ),
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
            "relationship_type": (str, type(None)),
            "message": (str, type(None)),
            "source_id": (str, type(None)),
            "predicate": (str, type(None)),
            "target": (str, type(None)),
            "already_existed": (bool, type(None)),
            "added": (bool, type(None)),
            "text_value_id": (str, type(None)),
            "relation_id": (str, type(None)),
            "error": (str, type(None)),
            "error_code": (str, type(None)),
            "error_details": (dict, type(None)),
        },
        allow_unknown=True,
        description="add_relationship output: success (bool), relationship_type (str), message (str), source_id (str), predicate (str), target (str), already_existed (bool), added (bool), text_value_id (str), relation_id (str), error (str), error_code (str), error_details (dict)",
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
        description="remove_relationship input: source_id (str, concept ID), predicate (str, relationship type like 'instance_of', 'typeOf', or custom predicate), target (str, target concept ID). Only concept-to-concept relationships are supported.",
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
            "error_code": (str, type(None)),
            "error_details": (dict, type(None)),
        },
        allow_unknown=True,
        description="remove_relationship output: success (bool), source_id, predicate, target, removed (bool), already_absent (bool when relation was not present), message, error, error_code (str), error_details (dict)",
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
            "delete_local_cache": (bool, type(None)),
        },
        allow_unknown=True,
        description=(
            "download_paper input: arxiv_id (str, e.g., '2506.16596'), filename (str, optional custom name), "
            "delete_local_cache (bool, optional; default true when authenticated)"
        ),
    )


def _download_paper_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "success": (bool, type(None)),
            "file_path": (str, type(None)),
            "arxiv_id": (str, type(None)),
            "version": (int, type(None)),
            "size_bytes": (int, type(None)),
            "sha256": (str, type(None)),
            "storage": (dict, type(None)),
            "computer_file_copy_concept_id": (str, type(None)),
            "uploaded_at": (str, type(None)),
            "local_cache_deleted": (bool, type(None)),
            "local_cache_delete_error": (str, type(None)),
            "error": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "download_paper output: success (bool), file_path (str, local cache path), "
            "storage (dict with backend/key/uri for durable blob-store location), arxiv_id (str), "
            "version (int, optional), size_bytes (int, optional), sha256 (str, optional), "
            "computer_file_copy_concept_id (str, optional when authenticated), uploaded_at (iso str, optional), "
            "local_cache_deleted (bool, optional), local_cache_delete_error (str, optional), "
            "or error (str) if failed"
        ),
    )


def _finalise_cached_paper_input_schema() -> Schema:
    return Schema(
        required={
            "arxiv_id": str,
        },
        optional={
            "name": (str, type(None)),
            "delete_local_cache": (bool, type(None)),
            "include_markdown": (bool, type(None)),
        },
        allow_unknown=True,
        description=(
            "finalise_cached_paper input: arxiv_id (str, e.g., '2506.16596v2'); "
            "name (str, optional concept/display name override); "
            "delete_local_cache (bool, optional; default true); "
            "include_markdown (bool, optional; default true)"
        ),
    )


def _finalise_cached_paper_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "success": (bool, type(None)),
            "arxiv_id": (str, type(None)),
            "file_path": (str, type(None)),
            "size_bytes": (int, type(None)),
            "sha256": (str, type(None)),
            "storage": (dict, type(None)),
            "computer_file_copy_concept_id": (str, type(None)),
            "uploaded_at": (str, type(None)),
            "local_cache_deleted": (bool, type(None)),
            "local_cache_delete_error": (str, type(None)),
            "markdown": (dict, type(None)),
            "message": (str, type(None)),
            "error": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "finalise_cached_paper output: success (bool), file_path (local cached PDF path), "
            "storage (dict with backend/key/uri), sha256 (str), size_bytes (int), "
            "computer_file_copy_concept_id (str) and uploaded_at (iso str), "
            "local_cache_deleted (bool), local_cache_delete_error (str, optional), "
            "markdown (dict, optional), or error (str)."
        ),
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
        description=(
            "list_papers output: total_papers (int), papers (list of cached paper objects; "
            "typically includes file_path, filename, size_bytes, arxiv_id, version), "
            "or error (str) if failed"
        ),
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


def _read_file_copy_input_schema() -> Schema:
    return Schema(
        required={
            "concept_id": str,
        },
        optional={
            "max_bytes": (int, type(None)),
            "encoding": (str, type(None)),
            "as_text": (bool, type(None)),
            "allow_large": (bool, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "read_file_copy input: concept_id (str for a #V#computer_file_copy instance), "
            "max_bytes (int, optional default 5000000), encoding (str, default utf-8), "
            "as_text (bool, default true; if false returns base64), allow_large (bool, default false; "
            "when true, max_bytes may be raised up to VON_READ_FILE_COPY_MAX_BYTES_OVERRIDE, default 20000000), "
            "namespace (optional user@org override)."
        ),
    )


def _read_file_copy_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "success": (bool, type(None)),
            "error": (str, type(None)),
            "concept_id": (str, type(None)),
            "original_filename": (str, type(None)),
            "content_type": (str, type(None)),
            "size_bytes": (int, type(None)),
            "byte_length": (int, type(None)),
            "blob": (dict, type(None)),
            "text": (str, type(None)),
            "encoding": (str, type(None)),
            "bytes_base64": (str, type(None)),
            "bytes_base64_encoding": (str, type(None)),
            "max_bytes": (int, type(None)),
            "text_extraction": (str, type(None)),
            "text_extraction_error": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "read_file_copy output: success (bool), concept_id (str), original_filename (str), "
            "content_type (str), size_bytes (int), byte_length (int), blob (dict), "
            "text (str, when as_text=true) or bytes_base64 (str, when as_text=false), "
            "encoding (str, when as_text=true), text_extraction (str, optional), "
            "or error (str) if failed."
        ),
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


def _resilient_extract_url_input_schema() -> Schema:
    return Schema(
        required={
            "url": str,
        },
        optional={
            "fallback_query": (str,),
            "context": (str,),
            "max_fallback_results": (int,),
            "max_extracts": (int,),
            "search_depth": (str,),
            "max_chars": (int,),
            "min_content_chars": (int,),
            "include_domains": (list,),
            "exclude_domains": (list,),
        },
        allow_unknown=True,
        description=(
            "resilient_extract_url input: url (str, required), optional fallback_query (str, overrides derived search query), "
            "context (str, optional context for context_search), max_fallback_results (int, default 5), max_extracts (int, default 4), "
            "search_depth ('basic'|'advanced', default 'basic'), max_chars (int, default 12000), min_content_chars (int, default 200), "
            "include_domains/exclude_domains (list of domains)."
        ),
    )


def _resilient_extract_url_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "success": (bool, type(None)),
            "primary_url": (str, type(None)),
            "extracted_from_url": (str, type(None)),
            "title": (str, type(None)),
            "content": (str, type(None)),
            "attempted": (list, type(None)),
            "search": (dict, type(None)),
            "error": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "resilient_extract_url output: best-effort extraction with fallbacks. Returns content/title plus attempted extractions and search provenance."
        ),
    )


def _delete_concept_input_schema() -> Schema:
    return Schema(
        required={"concept_id": str},
        optional={"simulate": (bool,)},
        allow_unknown=True,
        description="delete_concept input: concept_id (str), simulate (bool, default true)",
    )


def _delete_concept_output_schema() -> Schema:
    return Schema(
        required={"success": bool},
        optional={
            "simulate": (bool,),
            "error": (str,),
            "operations": (list,),
            "warnings": (list,),
        },
        allow_unknown=True,
        description="delete_concept output",
    )


def _merge_concepts_input_schema() -> Schema:
    return Schema(
        required={"source_id": str, "target_id": str},
        optional={"simulate": (bool,)},
        allow_unknown=True,
        description="merge_concepts input: source_id (str), target_id (str), simulate (bool, default true)",
    )


def _merge_concepts_output_schema() -> Schema:
    return Schema(
        required={"success": bool},
        optional={
            "simulate": (bool,),
            "error": (str,),
            "operations": (list,),
            "warnings": (list,),
        },
        allow_unknown=True,
        description="merge_concepts output",
    )


def _update_concept_input_schema() -> Schema:
    return Schema(
        required={"concept_id": str, "update_data": dict},
        optional={},
        allow_unknown=True,
        description="update_concept input: concept_id (str), update_data (dict). Use dot notation for nested fields (e.g. {'relationships.is_an_instance_of': [...]}) to avoid overwriting entire objects.",
    )


def _update_concept_output_schema() -> Schema:
    return Schema(
        required={"success": bool},
        optional={"concept_id": (str,), "updated_fields": (list,), "error": (str,)},
        allow_unknown=True,
        description="update_concept output",
    )


# RAG Tool Handlers and Schemas
def _resolve_rag_namespace_from_kwargs(kwargs: dict) -> dict:
    """Resolve namespace for RAG tools and report provenance.

    SECURITY: Do not infer namespaces from arbitrary client-provided user ids.
    Prefer explicit namespace (tool payload) and allow an env default for dev.
    """

    import os

    raw = kwargs.get("namespace")
    if isinstance(raw, str) and raw.strip():
        return {
            "namespace": raw.strip(),
            "namespace_source": "request.namespace",
            "namespace_resolution_note": None,
        }

    env_ns = os.environ.get("VON_DEFAULT_NAMESPACE")
    if isinstance(env_ns, str) and env_ns.strip():
        return {
            "namespace": env_ns.strip(),
            "namespace_source": "env.VON_DEFAULT_NAMESPACE",
            "namespace_resolution_note": "fallback",
        }

    return {
        "namespace": None,
        "namespace_source": "missing",
        "namespace_resolution_note": "namespace_required",
    }


def _with_rag_provenance(*, payload: dict, item_kind: str, source_system: str) -> dict:
    result = dict(payload)
    provenance = {
        "item_kind": item_kind,
        "source_system": source_system,
        "namespace": payload.get("namespace"),
        "namespace_source": payload.get("namespace_source"),
    }
    existing = result.get("provenance")
    if isinstance(existing, dict):
        provenance = {**existing, **provenance}
    result["provenance"] = provenance
    return result


def _search_knowledge_base(**kwargs):
    from ...services.rag_service import get_rag_service, RAGBackendUnavailable

    import time

    query_text = kwargs.get("query")
    if not query_text:
        return {"error": "Missing required parameter: query", "success": False}

    try:
        service = get_rag_service()  # Default backend
        ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
        ns = ns_report.get("namespace")

        # SECURITY: Require namespace for RAG search - prevents cross-user data leakage
        if not ns:
            return {
                "error": "namespace_required",
                "message": "RAG search requires authenticated user context (namespace)",
                **ns_report,
                "success": False,
            }

        # Build permissions context from Flask session for org-scoped RAG filtering.
        # IMPORTANT: Use concept IDs (e.g. #V#user) rather than email/usernames.
        permissions_context = {}
        try:
            from flask import session as flask_session

            user_concept_id = flask_session.get("user_concept_id")
            if isinstance(user_concept_id, str) and user_concept_id.strip():
                permissions_context["user_id"] = user_concept_id.strip()
            elif flask_session.get("user_id"):
                # Backwards compatibility: some sessions store a non-concept user_id.
                # Fall back to that only if we don't have a concept ID.
                permissions_context["user_id"] = flask_session.get("user_id")
            org_concept_id = flask_session.get("organisation_concept_id")
            if not org_concept_id:
                # Backwards compatibility for older session key.
                org_concept_id = flask_session.get("org_id")
            if org_concept_id:
                permissions_context["organisation_concept_id"] = org_concept_id
        except (ImportError, RuntimeError):
            # Not in Flask context (e.g., external MCP stdio server).
            # Derive the same effective filtering keys we use in-server when possible.
            user = kwargs.get("user")
            user_id = user.get("id") if isinstance(user, dict) else None
            if isinstance(user_id, str) and user_id.strip():
                # IMPORTANT: Use concept IDs verbatim (case sensitive, e.g. #V#person).
                permissions_context["user_id"] = user_id.strip()

            org_concept_id = kwargs.get("organisation_concept_id")
            if isinstance(org_concept_id, str) and org_concept_id.strip():
                permissions_context["organisation_concept_id"] = org_concept_id.strip()
            elif (
                isinstance(kwargs.get("org_id"), str)
                and str(kwargs.get("org_id")).strip()
            ):
                permissions_context["organisation_concept_id"] = str(
                    kwargs.get("org_id")
                ).strip()

            # If the namespace is in the user@org form, it contains enough
            # information to derive both IDs without trusting arbitrary inputs.
            if isinstance(ns, str) and "@" in ns:
                user_part, org_part = ns.split("@", 1)
                if user_part.strip() and "user_id" not in permissions_context:
                    permissions_context["user_id"] = user_part.strip()
                if (
                    org_part.strip()
                    and "organisation_concept_id" not in permissions_context
                ):
                    org_part_clean = org_part.strip()
                    if not org_part_clean.startswith("#V#"):
                        org_part_clean = f"#V#{org_part_clean}"
                    permissions_context["organisation_concept_id"] = org_part_clean

        # Optional semantic filtering (handled by the backend).
        mode = kwargs.get("mode")
        if isinstance(mode, str) and mode.strip():
            permissions_context["mode"] = mode.strip()

        requested_type = kwargs.get("type")
        if isinstance(requested_type, str) and requested_type.strip():
            permissions_context["type"] = requested_type.strip()

        predicate = kwargs.get("predicate")
        if isinstance(predicate, str) and predicate.strip():
            permissions_context["predicate"] = predicate.strip()

        predicates = kwargs.get("predicates")
        if isinstance(predicates, list):
            permissions_context["predicates"] = predicates

        start = time.perf_counter()
        results = service.query(
            query_text=query_text,
            top_k=kwargs.get("top_k", 5),
            namespace=ns,
            permissions_context=permissions_context if permissions_context else None,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        # Provenance-first: stamp RAG retrieval results so downstream consumers cannot
        # mistake them for chat history or KA sessions.
        stamped_results = []
        if isinstance(results, list):
            for row in results:
                if not isinstance(row, dict):
                    stamped_results.append(row)
                    continue
                meta = row.get("metadata")
                if not isinstance(meta, dict):
                    meta = {}
                meta = {
                    **meta,
                    "item_kind": "rag_chunk",
                    "source_system": "rag.llamaindex",
                    "namespace": ns,
                    "namespace_source": ns_report.get("namespace_source"),
                }
                stamped_results.append({**row, "metadata": meta})
        else:
            stamped_results = results

        return {
            "results": stamped_results,
            "count": len(stamped_results) if isinstance(stamped_results, list) else 0,
            "elapsed_ms": elapsed_ms,
            "effective_namespace": ns,
            "effective_namespace_source": ns_report.get("namespace_source"),
            **ns_report,
            "success": True,
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
            "mode": (str,),
            "type": (str,),
            "predicate": (str,),
            "predicates": (list,),
            "org_id": (str,),
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


def _search_concept_descriptions(**kwargs):
    """Semantic search over concept descriptions only.

    This is a thin wrapper over search_knowledge_base that applies:
    - mode=concepts
    - predicate=hasDescription
    """

    wrapped = dict(kwargs)
    wrapped.setdefault("mode", "concepts")
    wrapped.setdefault("predicate", "hasDescription")
    return _search_knowledge_base(**wrapped)


def _search_concept_descriptions_input_schema() -> Schema:
    return Schema(
        required={"query": str},
        optional={
            "top_k": (int,),
            "namespace": (str, type(None)),
            "org_id": (str,),
        },
        allow_unknown=True,
        description="search_concept_descriptions input: query (str), top_k (int, default 5), namespace (str, required for security)",
    )


def _search_concept_descriptions_output_schema() -> Schema:
    return _search_knowledge_base_output_schema()


def _index_concept_text(**kwargs):
    """Force reindex for a single concept's text relations within a namespace."""

    concept_id = kwargs.get("concept_id")
    if not isinstance(concept_id, str) or not concept_id.strip():
        return {"success": False, "error": "Missing required parameter: concept_id"}

    ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
    ns = ns_report.get("namespace")
    if not ns:
        return {
            "error": "namespace_required",
            "message": "RAG indexing requires authenticated user context (namespace)",
            **ns_report,
            "success": False,
        }

    from ...services.rag_text_relation_sync_service import sync_text_relations_to_rag

    payload = sync_text_relations_to_rag(
        namespace=ns,
        concept_ids=[concept_id.strip()],
        predicates=kwargs.get("predicates"),
        languages=kwargs.get("languages"),
        limit=int(kwargs.get("limit", 5000)),
        batch_size=int(kwargs.get("batch_size", 200)),
    )
    return {**payload, **ns_report}


def _index_concept_text_input_schema() -> Schema:
    return Schema(
        required={"concept_id": str},
        optional={
            "namespace": (str, type(None)),
            "predicates": (list,),
            "languages": (list,),
            "limit": (int,),
            "batch_size": (int,),
        },
        allow_unknown=True,
        description="index_concept_text input: concept_id (str), namespace (str, optional), and optional predicate/language filters",
    )


def _index_concept_text_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "success": (bool, type(None)),
            "error": (str, type(None)),
            "namespace": (str, type(None)),
            "total_candidates": (int, type(None)),
            "added": (int, type(None)),
            "failed": (int, type(None)),
        },
        allow_unknown=True,
        description="index_concept_text output: sync report payload",
    )


def _get_related_concepts(**kwargs):
    """Find concepts with similar descriptions (vector similarity)."""

    concept_id = kwargs.get("concept_id")
    if not isinstance(concept_id, str) or not concept_id.strip():
        return {"success": False, "error": "Missing required parameter: concept_id"}

    ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
    ns = ns_report.get("namespace")
    if not ns:
        return {
            "error": "namespace_required",
            "message": "RAG search requires authenticated user context (namespace)",
            **ns_report,
            "success": False,
        }

    # Attempt to seed the similarity query from the concept description text.
    # We scope access by (user, org) implied by the namespace to avoid cross-namespace reads.
    seed_text = kwargs.get("seed_text")
    if not isinstance(seed_text, str) or not seed_text.strip():
        try:
            from ...security.access_control import (
                override_current_user,
                override_current_organisation,
            )
            from ...services.text_value_service import get_texts_for_concept

            user_part = None
            org_part = None
            if isinstance(ns, str) and "@" in ns:
                user_part, org_part = ns.split("@", 1)
            user_part = (user_part or "").strip() or None
            org_part = (org_part or "").strip() or None
            if org_part and not org_part.startswith("#V#"):
                org_part = f"#V#{org_part}"

            with (
                override_current_user(user_part),
                override_current_organisation(org_part),
            ):
                texts = get_texts_for_concept(
                    concept_id.strip(), predicate="hasDescription", limit=1
                )
            if texts and isinstance(texts[0], dict):
                seed_text = texts[0].get("text")
        except Exception:
            seed_text = None

    if not isinstance(seed_text, str) or not seed_text.strip():
        return {
            "success": False,
            "error": "missing_seed_text",
            "message": "No hasDescription text found for concept; pass seed_text explicitly to proceed",
            **ns_report,
        }

    from ...services.rag_service import get_rag_service, RAGBackendUnavailable

    try:
        service = get_rag_service()

        user_id = None
        org_id = None
        if isinstance(ns, str) and "@" in ns:
            user_id, org_id = ns.split("@", 1)
            user_id = user_id.strip() or None
            org_id = org_id.strip() or None
            if org_id and not org_id.startswith("#V#"):
                org_id = f"#V#{org_id}"

        permissions_context = {
            "mode": "concepts",
            "predicate": "hasDescription",
        }
        if user_id:
            permissions_context["user_id"] = user_id
        if org_id:
            permissions_context["organisation_concept_id"] = org_id

        results = service.query(
            query_text=seed_text.strip(),
            top_k=int(kwargs.get("top_k", 10)),
            namespace=ns,
            permissions_context=permissions_context,
        )
    except RAGBackendUnavailable as e:
        return {"success": False, "error": f"RAG service unavailable: {e}", **ns_report}

    filtered = []
    for row in results or []:
        if not isinstance(row, dict):
            continue
        meta = row.get("metadata")
        if not isinstance(meta, dict):
            continue
        if (
            meta.get("concept_id") == concept_id.strip()
            or meta.get("subject_concept_id") == concept_id.strip()
        ):
            continue
        filtered.append(row)

    return {
        "success": True,
        "concept_id": concept_id.strip(),
        "seed_text": seed_text.strip(),
        "results": filtered,
        "count": len(filtered),
        **ns_report,
    }


def _get_related_concepts_input_schema() -> Schema:
    return Schema(
        required={"concept_id": str},
        optional={
            "namespace": (str, type(None)),
            "top_k": (int,),
            "seed_text": (str,),
        },
        allow_unknown=True,
        description="get_related_concepts input: concept_id (str), namespace (str, required), optional top_k and seed_text",
    )


def _get_related_concepts_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "success": (bool, type(None)),
            "error": (str, type(None)),
            "concept_id": (str, type(None)),
            "seed_text": (str, type(None)),
            "results": (list, type(None)),
            "count": (int, type(None)),
        },
        allow_unknown=True,
        description="get_related_concepts output: similar concept description chunks",
    )


# Jira schema helpers
def _jira_search_input_schema() -> Schema:
    return Schema(
        required={"jql": str},
        optional={
            "max_results": (int,),
            "start_at": (int,),
            "next_page_token": (str,),
            "fields": (list,),
        },
        allow_unknown=True,
        description=(
            "jira_search input: jql (str, required) plus optional max_results, next_page_token, start_at (deprecated), and fields (list of field names)."
        ),
    )


def _jira_get_issue_input_schema() -> Schema:
    return Schema(
        required={"issue_key": str},
        optional={"fields": (list,)},
        allow_unknown=True,
        description="jira_get_issue input: issue_key (str, required), optional fields (list of field names)",
    )


def _jira_add_comment_input_schema() -> Schema:
    return Schema(
        required={"issue_key": str, "comment": str},
        optional={},
        allow_unknown=True,
        description="jira_add_comment input: issue_key (str) and comment (str) both required",
    )


def _jira_transition_input_schema() -> Schema:
    return Schema(
        required={"issue_key": str, "transition_id": str},
        optional={},
        allow_unknown=True,
        description="jira_transition input: issue_key (str) and transition_id (str) required",
    )


def _jira_create_issue_input_schema() -> Schema:
    return Schema(
        required={
            "project_key": str,
            "issue_type": str,
            "summary": str,
        },
        optional={
            "description": (str, type(None)),
            "parent": (str, type(None)),
            "assignee_account_id": (str, type(None)),
            "labels": (list, type(None)),
            "dry_run": (bool,),
            "approved": (bool,),
            "execute": (bool,),
            "request_id": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "jira_create_issue input: project_key, issue_type, summary (required). "
            "Optional description/parent/assignee_account_id/labels. "
            "Guardrails: dry_run (default true), approved (per-write confirmation), execute (requires VON_INTERNAL_MCP_JIRA_EXECUTE_MODE=1)."
        ),
    )


def _jira_update_issue_input_schema() -> Schema:
    return Schema(
        required={
            "issue_key": str,
            "update_fields": dict,
        },
        optional={
            "dry_run": (bool,),
            "approved": (bool,),
            "execute": (bool,),
            "request_id": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "jira_update_issue input: issue_key (required) and update_fields (dict of Jira fields to update). "
            "Guardrails: dry_run (default true), approved (per-write confirmation), execute (requires VON_INTERNAL_MCP_JIRA_EXECUTE_MODE=1)."
        ),
    )


def _jira_link_issue_input_schema() -> Schema:
    return Schema(
        required={
            "inward_issue_key": str,
            "outward_issue_key": str,
            "link_type": str,
        },
        optional={
            "comment": (str, type(None)),
            "dry_run": (bool,),
            "approved": (bool,),
            "execute": (bool,),
            "request_id": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "jira_link_issue input: inward_issue_key, outward_issue_key, link_type (required; e.g. 'Relates'). "
            "Optional comment. Guardrails: dry_run (default true), approved, execute (requires VON_INTERNAL_MCP_JIRA_EXECUTE_MODE=1)."
        ),
    )


def _jira_get_myself_input_schema() -> Schema:
    return Schema(
        required={},
        optional={},
        allow_unknown=True,
        description="jira_get_myself input: no arguments",
    )


def _jira_get_auth_config_input_schema() -> Schema:
    return Schema(
        required={},
        optional={},
        allow_unknown=False,
        description="jira_get_auth_config input: no arguments",
    )


def _jira_get_auth_config_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
            "token_present": bool,
            "token_length": int,
            "env_keys_used": dict,
            "notes": str,
        },
        optional={
            "base_url": (str, type(None)),
            "email": (str, type(None)),
        },
        allow_unknown=False,
        description=(
            "jira_get_auth_config output: base_url/email/token_present/token_length and which env keys were used. "
            "Never returns the token."
        ),
    )


def _jira_generic_output_schema(action: str) -> Schema:
    return Schema(
        required={},
        optional={
            "error": (str, type(None)),
            "success": (bool, type(None)),
        },
        allow_unknown=True,
        description=(
            f"jira_{action} output: passes through Jira API response and optional error/success fields."
        ),
    )


# RAG metadata/content MCP tools
def _rag_get_status(**kwargs):
    import requests
    import os

    try:
        ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
        ns = ns_report.get("namespace") or os.environ.get("VON_DEFAULT_NAMESPACE")
        detail = kwargs.get("detail")
        base_url = (
            os.environ.get("VON_HTTP_BASE_URL")
            or os.environ.get("VON_WEB_BASE_URL")
            or "http://127.0.0.1:5000"
        )
        base_url = base_url.rstrip("/")
        url = f"{base_url}/admin/rag_status"
        if ns:
            url = f"{url}?namespace={ns}"
        if detail:
            url = f"{url}{'&' if '?' in url else '?'}detail=1"
        res = requests.get(url, timeout=5)
        if res.ok:
            payload = res.json()
            if isinstance(payload, dict):
                payload.update(ns_report)
                payload = _with_rag_provenance(
                    payload=payload,
                    item_kind="rag_status_report",
                    source_system="http.admin_rag_status",
                )
            return payload
        return {"error": f"HTTP {res.status_code}", "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def _resolve_rag_collection_from_kwargs(kwargs: dict) -> dict[str, object]:
    """Resolve a user-provided collection selector.

    This exists to prevent silent defaults: responses should echo what the
    caller asked for vs what was actually used.
    """

    raw = kwargs.get("collection")
    if raw is None:
        return {
            "requested_collection": None,
            "effective_collection": "ka_sessions",
            "collection_source": "default",
            "collection_resolution_note": "default",
        }

    if not isinstance(raw, str):
        return {
            "requested_collection": raw,
            "effective_collection": "ka_sessions",
            "collection_source": "default",
            "collection_resolution_note": "invalid",
        }

    cleaned = raw.strip()
    if not cleaned:
        return {
            "requested_collection": raw,
            "effective_collection": "ka_sessions",
            "collection_source": "default",
            "collection_resolution_note": "empty",
        }

    lowered = cleaned.lower()
    aliases = {
        "ka": "ka_sessions",
        "ka_session": "ka_sessions",
        "ka_sessions": "ka_sessions",
        "interaction": "ka_sessions",
        "interaction_session": "ka_sessions",
        "interaction_sessions": "ka_sessions",
        "indexed_sessions": "ka_sessions",
        "indexed": "ka_sessions",
        "chat": "chat_history_sessions",
        "chat_session": "chat_history_sessions",
        "chat_sessions": "chat_history_sessions",
        "chat_history": "chat_history_sessions",
        "chat_history_session": "chat_history_sessions",
        "chat_history_sessions": "chat_history_sessions",
        "text_relations": "vontology_text_relations",
        "text_relation": "vontology_text_relations",
        "text": "vontology_text_relations",
        "vontology_text_relations": "vontology_text_relations",
    }
    effective = aliases.get(lowered, lowered)
    return {
        "requested_collection": cleaned,
        "effective_collection": effective,
        "collection_source": (
            "request.collection_alias" if effective != lowered else "request.collection"
        ),
        "collection_resolution_note": (
            f"alias:{lowered}" if effective != lowered else None
        ),
    }


def _rag_list_collections(**kwargs):
    ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
    ns = ns_report.get("namespace")

    # SECURITY: Require namespace for RAG access - prevents cross-user data leakage
    if not ns:
        return {
            "error": "namespace_required",
            "message": "RAG access requires authenticated user context (namespace)",
            **ns_report,
            "success": False,
        }

    collections = [
        {
            "collection": "ka_sessions",
            "label": "Indexed KA interaction sessions",
            "description": (
                "Interaction sessions stored in MongoDB (interaction_sessions) that have been indexed. "
                "Use when you want to list or inspect KA sessions, not individual vector-store chunks."
            ),
            "list_tool": "rag_list_indexed",
            "get_tool": "rag_get_item",
            "search_tool": "search_knowledge_base",
            "list_supported": True,
            "get_supported": True,
            "search_supported": True,
            "list_supported_reason": None,
            "get_supported_reason": None,
            "item_kind": "ka_interaction_session",
            "source_system": "mongo.interaction_sessions",
        },
        {
            "collection": "chat_history_sessions",
            "label": "Chat history sessions",
            "description": (
                "Chat session documents stored in MongoDB (chat_history). These may be indexed into the vector store "
                "incrementally per message. Use when you want to list or inspect chat sessions."
            ),
            "list_tool": "rag_list_indexed",
            "get_tool": "rag_get_item",
            "search_tool": "search_knowledge_base",
            "list_supported": True,
            "get_supported": True,
            "search_supported": True,
            "list_supported_reason": None,
            "get_supported_reason": None,
            "item_kind": "chat_history_session",
            "source_system": "mongo.chat_history",
        },
        {
            "collection": "rag_documents",
            "label": "Vector-store documents/chunks",
            "description": (
                "Semantic search index content (vector-store chunks). Not directly listable yet; use search_knowledge_base "
                "to retrieve relevant chunks, or rag_get_status for counts."
            ),
            "list_tool": None,
            "get_tool": None,
            "search_tool": "search_knowledge_base",
            "list_supported": False,
            "get_supported": False,
            "search_supported": True,
            "list_supported_reason": "not_listable",
            "get_supported_reason": "not_addressable",
            "item_kind": "rag_chunk",
            "source_system": "rag.llamaindex",
        },
        {
            "collection": "vontology_text_relations",
            "label": "Vontology text relations",
            "description": (
                "Concept-linked text values stored in MongoDB (text_relations + text_values), such as hasName/hasDescription/hasContent. "
                "These can be indexed into the vector-store so they are discoverable via search_knowledge_base. "
                "Use rag_sync_text_relations to (re)index them for a namespace."
            ),
            "list_tool": "rag_list_indexed",
            "get_tool": "rag_get_item",
            "search_tool": "search_knowledge_base",
            "list_supported": True,
            "get_supported": True,
            "search_supported": True,
            "list_supported_reason": None,
            "get_supported_reason": None,
            "item_kind": "vontology_text_relation",
            "source_system": "mongo.text_relations",
        },
    ]

    payload = {
        "collections": collections,
        "count": len(collections),
        "effective_namespace": ns,
        "effective_namespace_source": ns_report.get("namespace_source"),
        **ns_report,
        "success": True,
    }
    return _with_rag_provenance(
        payload=payload,
        item_kind="rag_collection_list",
        source_system="internal_mcp.catalogue",
    )


def _rag_list_indexed(**kwargs):
    from ...db.connection_manager import get_db

    db = get_db()
    if db is None:
        return {"error": "db_unavailable", "success": False}
    collection_report = _resolve_rag_collection_from_kwargs(kwargs)
    collection = collection_report.get("effective_collection")
    limit = int(kwargs.get("limit", 20))
    offset = int(kwargs.get("offset", 0))

    ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
    ns = ns_report.get("namespace")

    # SECURITY: Require namespace for RAG access - prevents cross-user data leakage
    if not ns:
        return {
            "error": "namespace_required",
            "message": "RAG access requires authenticated user context (namespace)",
            **ns_report,
            "success": False,
        }

    if not isinstance(collection, str) or not collection:
        collection = "ka_sessions"

    if collection == "ka_sessions":
        coll = db["interaction_sessions"]

        # Build query with namespace filter
        query = {"indexing_status": "indexed", "namespace": ns}

        cursor = (
            coll.find(
                query,
                {
                    "_id": 1,
                    "indexed_at": 1,
                    "summary": 1,
                    "history": 1,
                    "namespace": 1,
                },
            )
            .skip(offset)
            .limit(limit)
        )
        items = []
        for doc in cursor:
            preview_len = 0
            if isinstance(doc.get("summary"), str):
                preview_len += len(doc["summary"])
            history = doc.get("history") or []
            if isinstance(history, list):
                for h in history:
                    content = h.get("content")
                    if isinstance(content, str):
                        preview_len += len(content)
            items.append(
                {
                    "collection": collection,
                    "session_id": str(doc.get("_id")),
                    "indexed_at": (
                        str(doc.get("indexed_at")) if doc.get("indexed_at") else None
                    ),
                    "preview_length": preview_len,
                    "namespace": doc.get("namespace"),
                    "item_kind": "ka_interaction_session",
                    "source_system": "mongo.interaction_sessions",
                    "namespace_source": ns_report.get("namespace_source"),
                }
            )
        total = coll.count_documents(query)
        payload = {
            "collection": collection,
            **collection_report,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "effective_namespace": ns,
            "effective_namespace_source": ns_report.get("namespace_source"),
            **ns_report,
            "success": True,
        }

        return _with_rag_provenance(
            payload=payload,
            item_kind="rag_indexed_session_list",
            source_system="mongo.interaction_sessions",
        )

    if collection == "chat_history_sessions":
        coll = db["chat_history"]

        query = {"namespace": ns}
        cursor = (
            coll.find(
                query,
                {
                    "session_id": 1,
                    "created_at": 1,
                    "updated_at": 1,
                    "history": 1,
                    "namespace": 1,
                    "rag_indexed_success": 1,
                    "rag_indexed_failed": 1,
                },
            )
            .skip(offset)
            .limit(limit)
        )
        items = []
        for doc in cursor:
            preview_len = 0
            history = doc.get("history") or []
            if isinstance(history, list):
                for h in history:
                    content = h.get("content")
                    if isinstance(content, str):
                        preview_len += len(content)
            items.append(
                {
                    "collection": collection,
                    "session_id": doc.get("session_id"),
                    "created_at": (
                        str(doc.get("created_at")) if doc.get("created_at") else None
                    ),
                    "updated_at": (
                        str(doc.get("updated_at")) if doc.get("updated_at") else None
                    ),
                    "preview_length": preview_len,
                    "message_count": len(history) if isinstance(history, list) else 0,
                    "rag_indexed_success": doc.get("rag_indexed_success"),
                    "rag_indexed_failed": doc.get("rag_indexed_failed"),
                    "namespace": doc.get("namespace"),
                    "item_kind": "chat_history_session",
                    "source_system": "mongo.chat_history",
                    "namespace_source": ns_report.get("namespace_source"),
                }
            )

        total = coll.count_documents(query)
        payload = {
            "collection": collection,
            **collection_report,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "effective_namespace": ns,
            "effective_namespace_source": ns_report.get("namespace_source"),
            **ns_report,
            "success": True,
        }
        return _with_rag_provenance(
            payload=payload,
            item_kind="rag_chat_session_list",
            source_system="mongo.chat_history",
        )

    if collection == "vontology_text_relations":
        from ...services.rag_text_relation_sync_service import (
            list_text_relation_index_items,
        )

        scan_limit = int(kwargs.get("scan_limit", 5000))
        predicates = kwargs.get("predicates")
        if predicates is not None and not isinstance(predicates, list):
            return {
                "error": "invalid_predicates",
                "message": "predicates must be an array of strings",
                "collection": collection,
                **collection_report,
                "effective_namespace": ns,
                "effective_namespace_source": ns_report.get("namespace_source"),
                **ns_report,
                "success": False,
            }

        languages = kwargs.get("languages")
        if languages is not None and not isinstance(languages, list):
            return {
                "error": "invalid_languages",
                "message": "languages must be an array of strings",
                "collection": collection,
                **collection_report,
                "effective_namespace": ns,
                "effective_namespace_source": ns_report.get("namespace_source"),
                **ns_report,
                "success": False,
            }

        report = list_text_relation_index_items(
            namespace=ns,
            limit=limit,
            offset=offset,
            scan_limit=scan_limit,
            predicates=predicates,
            languages=languages,
        )

        raw_items = report.get("items") if isinstance(report, dict) else None
        items = []
        if isinstance(raw_items, list):
            for row in raw_items:
                if not isinstance(row, dict):
                    continue
                items.append(
                    {
                        "collection": collection,
                        "session_id": row.get("relation_id"),
                        "relation_id": row.get("relation_id"),
                        "subject_concept_id": row.get("subject_concept_id"),
                        "predicate": row.get("predicate"),
                        "lang": row.get("lang"),
                        "preview_length": row.get("preview_length"),
                        "updated_at": row.get("updated_at"),
                        "namespace": ns,
                        "item_kind": "vontology_text_relation",
                        "source_system": "mongo.text_relations",
                        "namespace_source": ns_report.get("namespace_source"),
                    }
                )

        payload = {
            "collection": collection,
            **collection_report,
            "items": items,
            "total": report.get("total") if isinstance(report, dict) else None,
            "scanned": report.get("scanned") if isinstance(report, dict) else None,
            "scan_limit": (
                report.get("scan_limit") if isinstance(report, dict) else scan_limit
            ),
            "limit": limit,
            "offset": offset,
            "effective_namespace": ns,
            "effective_namespace_source": ns_report.get("namespace_source"),
            **ns_report,
            "success": True,
        }

        return _with_rag_provenance(
            payload=payload,
            item_kind="rag_text_relation_list",
            source_system="mongo.text_relations",
        )

    return {
        "error": "unknown_collection",
        "message": f"Unknown collection: {collection}",
        "collection": collection,
        **collection_report,
        "effective_namespace": ns,
        "effective_namespace_source": ns_report.get("namespace_source"),
        **ns_report,
        "success": False,
    }


def _rag_get_item(**kwargs):
    from ...db.connection_manager import get_db
    from bson import ObjectId

    db = get_db()
    if db is None:
        return {"error": "db_unavailable", "success": False}
    collection_report = _resolve_rag_collection_from_kwargs(kwargs)
    collection = collection_report.get("effective_collection")
    session_id = kwargs.get("session_id")
    if not session_id:
        return {"error": "Missing session_id", "success": False}

    ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
    ns = ns_report.get("namespace")

    # SECURITY: Require namespace for RAG access - prevents cross-user data leakage
    if not ns:
        return {
            "error": "namespace_required",
            "message": "RAG access requires authenticated user context (namespace)",
            **ns_report,
            "success": False,
        }

    if not isinstance(collection, str) or not collection:
        collection = "ka_sessions"

    if collection == "ka_sessions":
        coll = db["interaction_sessions"]
        try:
            query = {"_id": ObjectId(session_id), "namespace": ns}
        except Exception:
            query = {"_id": session_id, "namespace": ns}

        doc = coll.find_one(query)
        if not doc:
            return {"error": "not_found", "success": False}

        # Build a safe preview
        preview = []
        if isinstance(doc.get("summary"), str):
            preview.append(doc["summary"])
        history = doc.get("history") or []
        if isinstance(history, list):
            for h in history:
                c = h.get("content")
                if isinstance(c, str):
                    preview.append(c)
        payload = {
            "collection": collection,
            **collection_report,
            "session_id": str(doc.get("_id")),
            "indexing_status": doc.get("indexing_status"),
            "indexed_at": (
                str(doc.get("indexed_at")) if doc.get("indexed_at") else None
            ),
            "namespace": doc.get("namespace"),
            "preview": "\n\n".join(preview)[:4000],
            "item_kind": "ka_interaction_session",
            "source_system": "mongo.interaction_sessions",
            "namespace_source": ns_report.get("namespace_source"),
            "effective_namespace": ns,
            "effective_namespace_source": ns_report.get("namespace_source"),
            **ns_report,
            "success": True,
        }

        return _with_rag_provenance(
            payload=payload,
            item_kind="rag_indexed_session_item",
            source_system="mongo.interaction_sessions",
        )

    if collection == "chat_history_sessions":
        coll = db["chat_history"]
        doc = coll.find_one({"session_id": session_id, "namespace": ns})
        if not doc:
            return {"error": "not_found", "success": False}

        history = doc.get("history") or []
        preview_parts = []
        if isinstance(history, list):
            for h in history:
                c = h.get("content")
                if isinstance(c, str):
                    preview_parts.append(c)

        payload = {
            "collection": collection,
            **collection_report,
            "session_id": doc.get("session_id"),
            "created_at": (
                str(doc.get("created_at")) if doc.get("created_at") else None
            ),
            "updated_at": (
                str(doc.get("updated_at")) if doc.get("updated_at") else None
            ),
            "namespace": doc.get("namespace"),
            "message_count": len(history) if isinstance(history, list) else 0,
            "preview": "\n\n".join(preview_parts)[:4000],
            "item_kind": "chat_history_session",
            "source_system": "mongo.chat_history",
            "namespace_source": ns_report.get("namespace_source"),
            "effective_namespace": ns,
            "effective_namespace_source": ns_report.get("namespace_source"),
            **ns_report,
            "success": True,
        }
        return _with_rag_provenance(
            payload=payload,
            item_kind="rag_chat_session_item",
            source_system="mongo.chat_history",
        )

    if collection == "vontology_text_relations":
        from ...services.rag_text_relation_sync_service import get_text_relation_preview

        doc = get_text_relation_preview(namespace=ns, relation_id=session_id)
        if doc is None:
            return {
                "error": "not_found",
                "collection": collection,
                **collection_report,
                "effective_namespace": ns,
                "effective_namespace_source": ns_report.get("namespace_source"),
                **ns_report,
                "success": False,
            }

        payload = {
            "collection": collection,
            **collection_report,
            "session_id": session_id,
            "relation_id": doc.metadata.get("relation_id"),
            "subject_concept_id": doc.metadata.get("subject_concept_id"),
            "predicate": doc.metadata.get("predicate"),
            "lang": doc.metadata.get("lang"),
            "namespace": ns,
            "preview": (doc.text or "")[:4000],
            "item_kind": "vontology_text_relation",
            "source_system": "mongo.text_relations",
            "namespace_source": ns_report.get("namespace_source"),
            "effective_namespace": ns,
            "effective_namespace_source": ns_report.get("namespace_source"),
            **ns_report,
            "success": True,
        }
        return _with_rag_provenance(
            payload=payload,
            item_kind="rag_text_relation_item",
            source_system="mongo.text_relations",
        )

    return {
        "error": "unknown_collection",
        "message": f"Unknown collection: {collection}",
        "collection": collection,
        **collection_report,
        "effective_namespace": ns,
        "effective_namespace_source": ns_report.get("namespace_source"),
        **ns_report,
        "success": False,
    }


def _rag_sync_text_relations(**kwargs):
    from ...services.rag_text_relation_sync_service import sync_text_relations_to_rag

    ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
    ns = ns_report.get("namespace")

    # SECURITY: Require namespace for RAG access - prevents cross-user data leakage
    if not ns:
        return {
            "error": "namespace_required",
            "message": "RAG sync requires an explicit namespace (e.g. #V#user@org)",
            **ns_report,
            "success": False,
        }

    predicates = kwargs.get("predicates")
    if predicates is not None and not isinstance(predicates, list):
        return {
            "error": "invalid_predicates",
            "message": "predicates must be an array of strings",
            **ns_report,
            "success": False,
        }

    languages = kwargs.get("languages")
    if languages is not None and not isinstance(languages, list):
        return {
            "error": "invalid_languages",
            "message": "languages must be an array of strings",
            **ns_report,
            "success": False,
        }

    limit = int(kwargs.get("limit", 5000))
    batch_size = int(kwargs.get("batch_size", 200))

    payload = sync_text_relations_to_rag(
        namespace=ns,
        predicates=predicates,
        languages=languages,
        limit=limit,
        batch_size=batch_size,
    )
    if isinstance(payload, dict):
        payload.update(ns_report)
        payload = _with_rag_provenance(
            payload=payload,
            item_kind="rag_sync_text_relations",
            source_system="services.rag_text_relation_sync_service",
        )
    return payload


# Gmail MCP handlers (read-only surface)
def _gmail_list_messages(**kwargs):
    from ...integrations.google import gmail_service as gs

    profile = kwargs.get("profile") or kwargs.get("profile_id")
    if not profile:
        return {"error": "Missing required parameter: profile", "success": False}

    try:
        max_results = kwargs.get("max_results")
        if max_results is None:
            max_results = kwargs.get("maxResults")
        return gs.list_messages(
            profile_id=profile,
            query=kwargs.get("query"),
            label_ids=kwargs.get("label_ids"),
            max_results=max_results or 25,
            audit_context={
                "namespace": kwargs.get("namespace"),
                "source": "internal_mcp_gateway",
                "tool": "gmail_list_messages",
            },
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Gmail list failed: {exc}", "success": False}


def _gmail_get_message(**kwargs):
    from ...integrations.google import gmail_service as gs

    profile = kwargs.get("profile") or kwargs.get("profile_id")
    message_id = kwargs.get("message_id")
    if not profile or not message_id:
        return {
            "error": "Missing required parameters: profile and message_id",
            "success": False,
        }

    try:
        return gs.get_message(
            profile_id=profile,
            message_id=message_id,
            format=kwargs.get("format", "metadata"),
            audit_context={
                "namespace": kwargs.get("namespace"),
                "source": "internal_mcp_gateway",
                "tool": "gmail_get_message",
            },
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Gmail get message failed: {exc}", "success": False}


def _gmail_get_attachment(**kwargs):
    from ...integrations.google import gmail_service as gs

    profile = kwargs.get("profile") or kwargs.get("profile_id")
    message_id = kwargs.get("message_id")
    attachment_id = kwargs.get("attachment_id")
    if not profile or not message_id or not attachment_id:
        return {
            "error": "Missing required parameters: profile, message_id, attachment_id",
            "success": False,
        }

    try:
        return gs.get_attachment(
            profile_id=profile,
            message_id=message_id,
            attachment_id=attachment_id,
            audit_context={
                "namespace": kwargs.get("namespace"),
                "source": "internal_mcp_gateway",
                "tool": "gmail_get_attachment",
            },
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Gmail get attachment failed: {exc}", "success": False}


def _gmail_list_labels(**kwargs):
    from ...integrations.google import gmail_service as gs

    profile = kwargs.get("profile") or kwargs.get("profile_id")
    if not profile:
        return {"error": "Missing required parameter: profile", "success": False}

    try:
        return gs.list_labels(
            profile_id=profile,
            audit_context={
                "namespace": kwargs.get("namespace"),
                "source": "internal_mcp_gateway",
                "tool": "gmail_list_labels",
            },
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Gmail list labels failed: {exc}", "success": False}


def _gmail_modify_labels(**kwargs):
    from ...integrations.google import gmail_service as gs

    profile = kwargs.get("profile") or kwargs.get("profile_id")
    message_id = kwargs.get("message_id")
    allow_mutation = bool(kwargs.get("allow_mutation"))
    if not profile or not message_id:
        return {
            "error": "Missing required parameters: profile and message_id",
            "success": False,
        }
    if not allow_mutation:
        return {
            "error": "allow_mutation must be true to modify labels",
            "success": False,
        }

    try:
        return gs.modify_labels(
            profile_id=profile,
            message_id=message_id,
            add_labels=kwargs.get("add_labels"),
            remove_labels=kwargs.get("remove_labels"),
            allow_mutation=allow_mutation,
            audit_context={
                "namespace": kwargs.get("namespace"),
                "source": "internal_mcp_gateway",
                "tool": "gmail_modify_labels",
            },
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Gmail modify labels failed: {exc}", "success": False}


# Jira MCP handlers

_JIRA_WRITE_CACHE: dict[str, dict[str, Any]] = {}
_JIRA_WRITE_CACHE_TTL_SEC = 3600.0
_JIRA_WRITE_CACHE_MAX = 200


def _jira_project_allow_list() -> list[str]:
    import os

    raw = (
        os.getenv("VON_JIRA_PROJECT_ALLOW_LIST")
        or os.getenv("VON_JIRA_PROJECT_ALLOWLIST")
        or "JVNAUTOSCI"
    )

    projects: list[str] = []
    for part in raw.split(","):
        candidate = part.strip().upper()
        if not candidate:
            continue
        if candidate not in projects:
            projects.append(candidate)
    return projects


def _jira_project_from_issue_key(issue_key: str | None) -> str | None:
    if not isinstance(issue_key, str):
        return None
    cleaned = issue_key.strip()
    if not cleaned or "-" not in cleaned:
        return None
    return cleaned.split("-", 1)[0].upper() or None


def _jira_execute_mode_enabled() -> bool:
    import os

    return os.getenv("VON_INTERNAL_MCP_JIRA_EXECUTE_MODE", "0").lower() in {
        "1",
        "true",
    }


def _jira_write_guardrails(
    *,
    action: str,
    project_keys: list[str],
    dry_run: bool,
    approved: bool,
    execute: bool,
) -> dict[str, Any] | None:
    allowed = _jira_project_allow_list()
    if not allowed:
        return {
            "success": False,
            "error": "Jira writes are blocked: project allow-list is empty",
            "error_code": "allowlist_missing",
        }

    for project_key in project_keys:
        if not project_key or project_key.upper() not in allowed:
            return {
                "success": False,
                "error": f"Jira writes are blocked for project '{project_key}'. Allowed: {', '.join(allowed)}",
                "error_code": "project_not_allowlisted",
                "project_key": project_key,
                "allowed_projects": allowed,
                "action": action,
            }

    if dry_run:
        return None

    if execute and _jira_execute_mode_enabled():
        return None

    if approved:
        return None

    return {
        "success": False,
        "error": (
            "Approval required for Jira write. Set approved=true to confirm this write, "
            "or run in execute mode (set VON_INTERNAL_MCP_JIRA_EXECUTE_MODE=1 and pass execute=true)."
        ),
        "error_code": "approval_required",
        "action": action,
        "allowed_projects": allowed,
    }


def _jira_cache_get(tool: str, request_id: str) -> dict[str, Any] | None:
    import time

    key = f"{tool}:{request_id}"
    record = _JIRA_WRITE_CACHE.get(key)
    if not isinstance(record, dict):
        return None
    ts = record.get("timestamp")
    if not isinstance(ts, (int, float)):
        return None
    if (time.time() - float(ts)) > _JIRA_WRITE_CACHE_TTL_SEC:
        _JIRA_WRITE_CACHE.pop(key, None)
        return None
    cached_payload = record.get("payload")
    if not isinstance(cached_payload, dict):
        return None
    return dict(cached_payload)


def _jira_cache_set(tool: str, request_id: str, payload: dict[str, Any]) -> None:
    import time

    if len(_JIRA_WRITE_CACHE) >= _JIRA_WRITE_CACHE_MAX:
        # Simple eviction: drop the oldest item.
        oldest_key = None
        oldest_ts = None
        for k, v in _JIRA_WRITE_CACHE.items():
            ts = v.get("timestamp") if isinstance(v, dict) else None
            if not isinstance(ts, (int, float)):
                continue
            if oldest_ts is None or float(ts) < oldest_ts:
                oldest_ts = float(ts)
                oldest_key = k
        if oldest_key:
            _JIRA_WRITE_CACHE.pop(oldest_key, None)

    key = f"{tool}:{request_id}"
    _JIRA_WRITE_CACHE[key] = {
        "timestamp": time.time(),
        "payload": dict(payload),
    }


def _jira_search(**kwargs):
    import asyncio
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    jql = kwargs.get("jql")
    if not jql:
        return {"error": "Missing required parameter: jql", "success": False}

    async def _async_search():
        proxy = await get_jira_proxy()
        return await proxy.search(
            jql=jql,
            max_results=kwargs.get("max_results"),
            start_at=kwargs.get("start_at"),
            next_page_token=kwargs.get("next_page_token"),
            fields=kwargs.get("fields"),
        )

    try:
        return _run_async_compat(_async_search)
    except JiraProxyError as exc:
        return {"error": str(exc), "success": False}


def _jira_get_issue(**kwargs):
    import asyncio
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    issue_key = kwargs.get("issue_key")
    if not issue_key:
        return {"error": "Missing required parameter: issue_key", "success": False}

    async def _async_get_issue():
        proxy = await get_jira_proxy()
        return await proxy.get_issue(issue_key=issue_key, fields=kwargs.get("fields"))

    try:
        return _run_async_compat(_async_get_issue)
    except JiraProxyError as exc:
        return {"error": str(exc), "success": False}


def _jira_add_comment(**kwargs):
    import asyncio
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    issue_key = kwargs.get("issue_key")
    comment = kwargs.get("comment")
    if not issue_key or not comment:
        return {
            "error": "Missing required parameters: issue_key and comment",
            "success": False,
        }

    async def _async_comment():
        proxy = await get_jira_proxy()
        return await proxy.add_comment(issue_key=issue_key, comment=comment)

    try:
        return _run_async_compat(_async_comment)
    except JiraProxyError as exc:
        return {"error": str(exc), "success": False}


def _jira_transition_issue(**kwargs):
    import asyncio
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    issue_key = kwargs.get("issue_key")
    transition_id = kwargs.get("transition_id")
    if not issue_key or not transition_id:
        return {
            "error": "Missing required parameters: issue_key and transition_id",
            "success": False,
        }

    async def _async_transition():
        proxy = await get_jira_proxy()
        return await proxy.transition_issue(
            issue_key=issue_key, transition_id=transition_id
        )

    try:
        return _run_async_compat(_async_transition)
    except JiraProxyError as exc:
        return {"error": str(exc), "success": False}


def _jira_create_issue(**kwargs):
    import asyncio
    import logging
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    logger = logging.getLogger(__name__)

    project_key = kwargs.get("project_key")
    issue_type = kwargs.get("issue_type")
    summary = kwargs.get("summary")

    if not project_key or not issue_type or not summary:
        return {
            "success": False,
            "error": "Missing required parameters: project_key, issue_type, summary",
        }

    project_key_norm = str(project_key).strip().upper()
    dry_run = bool(kwargs.get("dry_run", True))
    approved = bool(kwargs.get("approved", False))
    execute = bool(kwargs.get("execute", False))
    request_id = kwargs.get("request_id")

    guardrail_error = _jira_write_guardrails(
        action="create_issue",
        project_keys=[project_key_norm],
        dry_run=dry_run,
        approved=approved,
        execute=execute,
    )
    if guardrail_error is not None:
        return guardrail_error

    if isinstance(request_id, str) and request_id.strip() and not dry_run:
        cached = _jira_cache_get("jira_create_issue", request_id.strip())
        if cached is not None:
            cached["reused"] = True
            return cached

    payload: dict[str, Any] = {
        "fields": {
            "project": {"key": project_key_norm},
            "summary": str(summary),
            "issuetype": {"name": str(issue_type)},
        }
    }

    description = kwargs.get("description")
    if isinstance(description, str) and description.strip():
        payload["fields"]["description"] = description

    parent = kwargs.get("parent")
    if isinstance(parent, str) and parent.strip():
        payload["fields"]["parent"] = {"key": parent.strip()}

    assignee_account_id = kwargs.get("assignee_account_id")
    if isinstance(assignee_account_id, str) and assignee_account_id.strip():
        payload["fields"]["assignee"] = {"accountId": assignee_account_id.strip()}

    labels = kwargs.get("labels")
    if isinstance(labels, list):
        payload["fields"]["labels"] = [str(l) for l in labels if str(l).strip()]

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "executed": False,
            "action": "create_issue",
            "project_key": project_key_norm,
            "proposed_payload": payload,
        }

    async def _async_create():
        proxy = await get_jira_proxy()
        return await proxy.create_issue(payload=payload)

    try:
        logger.info(
            "[jira_write] create_issue project=%s summary_preview=%r",
            project_key_norm,
            str(summary)[:120],
        )
        result = _run_async_compat(_async_create)
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("success", True)
            result["dry_run"] = False
            result["executed"] = True
            result["action"] = "create_issue"
            if isinstance(request_id, str) and request_id.strip():
                _jira_cache_set("jira_create_issue", request_id.strip(), result)
            return result
        return {
            "success": True,
            "dry_run": False,
            "executed": True,
            "action": "create_issue",
            "result": result,
        }
    except JiraProxyError as exc:
        return {"error": str(exc), "success": False}


def _jira_update_issue(**kwargs):
    import logging
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    logger = logging.getLogger(__name__)

    issue_key = kwargs.get("issue_key")
    update_fields = kwargs.get("update_fields")
    if not issue_key or not isinstance(update_fields, dict):
        return {
            "success": False,
            "error": "Missing required parameters: issue_key and update_fields (dict)",
        }

    issue_key_str = str(issue_key).strip()
    project_key = _jira_project_from_issue_key(issue_key_str)
    if not project_key:
        return {
            "success": False,
            "error": "Invalid issue_key format; expected PROJECT-123",
        }

    dry_run = bool(kwargs.get("dry_run", True))
    approved = bool(kwargs.get("approved", False))
    execute = bool(kwargs.get("execute", False))
    request_id = kwargs.get("request_id")

    guardrail_error = _jira_write_guardrails(
        action="update_issue",
        project_keys=[project_key],
        dry_run=dry_run,
        approved=approved,
        execute=execute,
    )
    if guardrail_error is not None:
        return guardrail_error

    if isinstance(request_id, str) and request_id.strip() and not dry_run:
        cached = _jira_cache_get("jira_update_issue", request_id.strip())
        if cached is not None:
            cached["reused"] = True
            return cached

    # Guardrail: do not allow changing the project via this helper.
    blocked_fields = {"project", "key", "id"}
    safe_fields = {
        str(k): v
        for k, v in update_fields.items()
        if isinstance(k, str) and k not in blocked_fields
    }
    if not safe_fields:
        return {
            "success": False,
            "error": "No updatable fields provided (project/key/id are not allowed)",
        }

    payload: dict[str, Any] = {"fields": safe_fields}

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "executed": False,
            "action": "update_issue",
            "issue_key": issue_key_str,
            "proposed_payload": payload,
        }

    async def _async_update():
        proxy = await get_jira_proxy()
        return await proxy.update_issue(issue_key=issue_key_str, payload=payload)

    try:
        logger.info(
            "[jira_write] update_issue key=%s fields=%s",
            issue_key_str,
            sorted(safe_fields.keys())[:25],
        )
        result = _run_async_compat(_async_update)
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("success", True)
            result["dry_run"] = False
            result["executed"] = True
            result["action"] = "update_issue"
            result["issue_key"] = issue_key_str
            if isinstance(request_id, str) and request_id.strip():
                _jira_cache_set("jira_update_issue", request_id.strip(), result)
            return result
        return {
            "success": True,
            "dry_run": False,
            "executed": True,
            "action": "update_issue",
            "issue_key": issue_key_str,
            "result": result,
        }
    except JiraProxyError as exc:
        return {"error": str(exc), "success": False}


def _jira_link_issue(**kwargs):
    import logging
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    logger = logging.getLogger(__name__)

    inward_issue_key = kwargs.get("inward_issue_key")
    outward_issue_key = kwargs.get("outward_issue_key")
    link_type = kwargs.get("link_type")
    if not inward_issue_key or not outward_issue_key or not link_type:
        return {
            "success": False,
            "error": "Missing required parameters: inward_issue_key, outward_issue_key, link_type",
        }

    inward_key = str(inward_issue_key).strip()
    outward_key = str(outward_issue_key).strip()
    inward_project = _jira_project_from_issue_key(inward_key)
    outward_project = _jira_project_from_issue_key(outward_key)
    if not inward_project or not outward_project:
        return {
            "success": False,
            "error": "Invalid issue key format; expected PROJECT-123",
        }

    dry_run = bool(kwargs.get("dry_run", True))
    approved = bool(kwargs.get("approved", False))
    execute = bool(kwargs.get("execute", False))
    request_id = kwargs.get("request_id")

    guardrail_error = _jira_write_guardrails(
        action="link_issue",
        project_keys=[inward_project, outward_project],
        dry_run=dry_run,
        approved=approved,
        execute=execute,
    )
    if guardrail_error is not None:
        return guardrail_error

    if isinstance(request_id, str) and request_id.strip() and not dry_run:
        cached = _jira_cache_get("jira_link_issue", request_id.strip())
        if cached is not None:
            cached["reused"] = True
            return cached

    payload: dict[str, Any] = {
        "type": {"name": str(link_type)},
        "inwardIssue": {"key": inward_key},
        "outwardIssue": {"key": outward_key},
    }

    comment = kwargs.get("comment")
    if isinstance(comment, str) and comment.strip():
        payload["comment"] = {"body": comment}

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "executed": False,
            "action": "link_issue",
            "inward_issue_key": inward_key,
            "outward_issue_key": outward_key,
            "link_type": str(link_type),
            "proposed_payload": payload,
        }

    async def _async_link():
        proxy = await get_jira_proxy()
        return await proxy.link_issue(payload=payload)

    try:
        logger.info(
            "[jira_write] link_issue %s -> %s (%s)",
            outward_key,
            inward_key,
            str(link_type),
        )
        result = _run_async_compat(_async_link)
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("success", True)
            result["dry_run"] = False
            result["executed"] = True
            result["action"] = "link_issue"
            result["inward_issue_key"] = inward_key
            result["outward_issue_key"] = outward_key
            result["link_type"] = str(link_type)
            if isinstance(request_id, str) and request_id.strip():
                _jira_cache_set("jira_link_issue", request_id.strip(), result)
            return result
        return {
            "success": True,
            "dry_run": False,
            "executed": True,
            "action": "link_issue",
            "result": result,
        }
    except JiraProxyError as exc:
        return {"error": str(exc), "success": False}


def _jira_get_myself(**kwargs):
    import asyncio
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    async def _async_get_myself():
        proxy = await get_jira_proxy()
        return await proxy.get_myself()

    try:
        return _run_async_compat(_async_get_myself)
    except JiraProxyError as exc:
        return {"error": str(exc), "success": False}


def _jira_get_auth_config(**kwargs):
    from .jira_proxy_mcp import inspect_jira_auth_config

    return inspect_jira_auth_config()


def _chat_get_prompt_context(
    *,
    namespace: str | None = None,
    include_content: bool = False,
    max_chars: int | None = 2000,
    **_kwargs,
):
    """Return user-specific prompt context that affects chat.

    This is intended for debugging/inspection: which prompt concepts are linked
    to the authenticated user (namespace) and what content they contribute.

    Prompts are purpose-specific types:
    - `#V#von_chat_behaviour_prompt` (system behaviour)
    - `#V#von_llm_prompt` (legacy behaviour prompt ID)
    - `#V#von_chat_narration_prompt` (TTS / presenter narration)
    """

    if not namespace or not isinstance(namespace, str) or not namespace.strip():
        return {"success": False, "error": "namespace is required"}

    if not isinstance(include_content, bool):
        include_content = False

    if max_chars is None:
        max_chars_int = 2000
    else:
        try:
            max_chars_int = int(max_chars)
        except (TypeError, ValueError):
            max_chars_int = 2000
    max_chars_int = max(0, min(max_chars_int, 20000))

    from src.backend.services.chat_auxiliary_prompt_service import (
        build_user_specific_system_prompt,
        get_user_specific_prompt_fragments,
    )

    behaviour_fragments = get_user_specific_prompt_fragments(
        namespace,
        prompt_types=(
            "#V#von_chat_behaviour_prompt",
            "#V#von_chat_behavior_prompt",
            "#V#von_llm_prompt",
        ),
    )
    narration_fragments = get_user_specific_prompt_fragments(
        namespace,
        prompt_types=("#V#von_chat_narration_prompt",),
    )

    behaviour_prompt_concept_ids = [
        f.get("concept_id")
        for f in behaviour_fragments
        if isinstance(f.get("concept_id"), str)
    ]
    narration_prompt_concept_ids = [
        f.get("concept_id")
        for f in narration_fragments
        if isinstance(f.get("concept_id"), str)
    ]

    prompt_text = build_user_specific_system_prompt(
        namespace,
        prompt_types=(
            "#V#von_chat_behaviour_prompt",
            "#V#von_chat_behavior_prompt",
            "#V#von_llm_prompt",
        ),
    )
    if (
        isinstance(prompt_text, str)
        and max_chars_int
        and len(prompt_text) > max_chars_int
    ):
        prompt_text = (
            prompt_text[:max_chars_int]
            + f"\n... [truncated {len(prompt_text) - max_chars_int} chars]"
        )

    def _format_fragments(fragments_list):
        prompt_concepts_local = []
        for fragment in fragments_list:
            concept_id = fragment.get("concept_id")
            if not isinstance(concept_id, str):
                continue
            item = {"concept_id": concept_id}
            if include_content:
                content = fragment.get("content")
                item["content"] = content if isinstance(content, str) else ""
            prompt_concepts_local.append(item)
        return prompt_concepts_local

    behaviour_prompt_concepts = _format_fragments(behaviour_fragments)
    narration_prompt_concepts = _format_fragments(narration_fragments)

    template_service = PromptTemplateService()
    classifier_prompt_id, classifier_prompt_text = template_service.resolve_prompt_text(
        InternalMCPChatOrchestrator._MISSING_TOOL_CLASSIFIER_PROMPTS,
        fallback=InternalMCPChatOrchestrator._FALLBACK_MISSING_TOOL_CALL_PROMPT,
        max_chars=max_chars_int,
    )
    retry_prompt_id, retry_prompt_text = template_service.resolve_prompt_text(
        InternalMCPChatOrchestrator._MISSING_TOOL_RETRY_PROMPTS,
        fallback=None,
        max_chars=max_chars_int,
    )
    classifier_preview = (
        classifier_prompt_text[:max_chars_int] if classifier_prompt_text else ""
    )
    retry_preview = retry_prompt_text[:max_chars_int] if retry_prompt_text else ""
    narration_preview = ""
    if narration_fragments and isinstance(narration_fragments[0], dict):
        content = narration_fragments[0].get("content")
        if isinstance(content, str):
            narration_preview = content[:max_chars_int]
    resolved_templates = {
        "missing_tool_call_classifier": {
            "prompt_id": classifier_prompt_id,
            "preview": classifier_preview,
        },
        "missing_tool_call_retry": {
            "prompt_id": retry_prompt_id,
            "preview": retry_preview,
        },
        "behaviour_prompt": {"prompt_id": None, "preview": prompt_text or ""},
        "narration_prompt": {
            "prompt_id": (
                narration_prompt_concept_ids[0]
                if narration_prompt_concept_ids
                else None
            ),
            "preview": narration_preview,
        },
    }

    return {
        "success": True,
        "namespace": namespace,
        # Backwards-compatible fields expected by some callers.
        "prompt_concept_ids": list(behaviour_prompt_concept_ids),
        "prompt_concepts": behaviour_prompt_concepts,
        # Purpose-specific fields.
        "behaviour_prompt_concept_ids": behaviour_prompt_concept_ids,
        "behaviour_prompt_concepts": behaviour_prompt_concepts,
        "narration_prompt_concept_ids": narration_prompt_concept_ids,
        "narration_prompt_concepts": narration_prompt_concepts,
        "prompt_text": prompt_text or "",
        "prompt_count": len(behaviour_prompt_concept_ids),
        "resolved_templates": resolved_templates,
    }


def _settings_get_public(
    *,
    user_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
    **_kwargs,
):
    """Return a safe subset of settings (no secrets).

    This intentionally excludes any secret values (API tokens, passwords). It may
    include *names* of env vars (e.g., which env var holds a key) as that is not
    a secret.
    """

    from src.backend.server.routes.settings_routes import get_all_settings_data
    from src.backend.services.settings_service import resolve_llm_setting

    settings = get_all_settings_data() or {}
    try:
        settings["resolved_llm"] = resolve_llm_setting(
            user_concept_id=user_concept_id,
            org_concept_id=organisation_concept_id,
        )
    except Exception:
        settings["resolved_llm"] = None

    return {"success": True, "settings": settings}


def _chat_introspect(
    *,
    namespace: str | None = None,
    organisation_concept_id: str | None = None,
    include_prompt_content: bool = False,
    include_tool_guidance_preview: bool = False,
    max_preview_chars: int | None = 800,
    include_runtime_status: bool = True,
    **_kwargs,
):
    """Return a compact description of what influences chat context.

    Primary goal: allow the assistant (via MCP) to determine what system prompt
    influences are active (including Vontology-stored prompts) and what model
    configuration is in effect, without exposing secrets.
    """

    import hashlib

    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )
    from src.backend.languagemodels.llm_interface import get_active_model_name
    from src.backend.services.settings_service import (
        get_active_llm_setting,
        resolve_llm_setting,
    )

    if not namespace or not isinstance(namespace, str) or not namespace.strip():
        return {"success": False, "error": "namespace is required"}

    # Clamp preview size
    if max_preview_chars is None:
        max_preview_chars_int = 800
    else:
        try:
            max_preview_chars_int = int(max_preview_chars)
        except (TypeError, ValueError):
            max_preview_chars_int = 800
    max_preview_chars_int = max(0, min(max_preview_chars_int, 5000))

    # User-specific prompt fragments (JVNAUTOSCI-797)
    from src.backend.services.chat_auxiliary_prompt_service import (
        build_user_specific_system_prompt,
        get_user_specific_prompt_fragments,
    )

    behaviour_fragments = get_user_specific_prompt_fragments(
        namespace,
        prompt_types=(
            "#V#von_chat_behaviour_prompt",
            "#V#von_chat_behavior_prompt",
            "#V#von_llm_prompt",
        ),
    )
    narration_fragments = get_user_specific_prompt_fragments(
        namespace,
        prompt_types=("#V#von_chat_narration_prompt",),
    )

    behaviour_prompt_concept_ids = [
        f.get("concept_id")
        for f in behaviour_fragments
        if isinstance(f.get("concept_id"), str)
    ]
    narration_prompt_concept_ids = [
        f.get("concept_id")
        for f in narration_fragments
        if isinstance(f.get("concept_id"), str)
    ]

    auxiliary_prompt_text = (
        build_user_specific_system_prompt(
            namespace,
            prompt_types=(
                "#V#von_chat_behaviour_prompt",
                "#V#von_chat_behavior_prompt",
                "#V#von_llm_prompt",
            ),
        )
        or ""
    )

    prompt_concepts: list[dict] = []
    for fragment in behaviour_fragments:
        concept_id = fragment.get("concept_id")
        if not isinstance(concept_id, str):
            continue
        item = {"concept_id": concept_id}
        if include_prompt_content:
            content = fragment.get("content")
            item["content"] = content if isinstance(content, str) else ""
        prompt_concepts.append(item)

    narration_prompt_concepts: list[dict] = []
    for fragment in narration_fragments:
        concept_id = fragment.get("concept_id")
        if not isinstance(concept_id, str):
            continue
        item = {"concept_id": concept_id}
        if include_prompt_content:
            content = fragment.get("content")
            item["content"] = content if isinstance(content, str) else ""
        narration_prompt_concepts.append(item)

    # Model / provider information
    try:
        active_model_name = get_active_model_name()
    except Exception:
        active_model_name = None

    try:
        active_llm = get_active_llm_setting()
    except Exception:
        active_llm = None

    try:
        resolved_llm = resolve_llm_setting(
            user_concept_id=namespace, org_concept_id=organisation_concept_id
        )
    except Exception:
        resolved_llm = None

    gateway_enabled = None
    orchestrator_max_tool_invocations = None
    orchestrator_tool_batch_cap = None
    if include_runtime_status:
        try:
            from flask import current_app

            gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")
            orchestrator = current_app.config.get("INTERNAL_MCP_ORCHESTRATOR")
            gateway_enabled = getattr(gateway, "enabled", None)
            orchestrator_max_tool_invocations = getattr(
                orchestrator, "_max_tool_invocations", None
            )
            orchestrator_tool_batch_cap = getattr(orchestrator, "_tool_batch_cap", None)
        except Exception:
            gateway_enabled = None
            orchestrator_max_tool_invocations = None
            orchestrator_tool_batch_cap = None

    # Tool-guidance fingerprint (stable-ish) without dumping full text by default
    tool_guidance_text = ""
    tool_guidance_hash = None
    tool_guidance_preview = None

    try:
        # Prefer the live orchestrator (includes the real tool listing) when available.
        live_orchestrator = None
        try:
            from flask import current_app

            live_orchestrator = current_app.config.get("INTERNAL_MCP_ORCHESTRATOR")
        except Exception:
            live_orchestrator = None

        if live_orchestrator is not None and hasattr(
            live_orchestrator, "_instruction_message"
        ):
            tool_guidance_text = live_orchestrator._instruction_message(  # type: ignore[attr-defined]
                user_namespace=namespace,
                auxiliary_system_prompt=auxiliary_prompt_text,
                preferred_language=None,
            )
        else:

            class _StubGateway:
                def describe_methods(self):
                    return {}

            dummy_orchestrator = InternalMCPChatOrchestrator(gateway=_StubGateway())  # type: ignore[arg-type]
            tool_guidance_text = dummy_orchestrator._instruction_message(
                user_namespace=namespace,
                auxiliary_system_prompt=auxiliary_prompt_text,
                preferred_language=None,
            )

        tool_guidance_hash = hashlib.sha256(
            tool_guidance_text.encode("utf-8")
        ).hexdigest()
        if include_tool_guidance_preview and max_preview_chars_int:
            tool_guidance_preview = tool_guidance_text[:max_preview_chars_int]
    except Exception:
        tool_guidance_hash = None

    return {
        "success": True,
        "namespace": namespace,
        "organisation_concept_id": organisation_concept_id,
        "active_model_name": active_model_name,
        "active_llm": active_llm,
        "resolved_llm": resolved_llm,
        # Backwards-compatible fields.
        "prompt_concept_ids": list(behaviour_prompt_concept_ids),
        "prompt_concepts": prompt_concepts,
        "prompt_count": len(behaviour_prompt_concept_ids),
        # Purpose-specific fields.
        "behaviour_prompt_concept_ids": behaviour_prompt_concept_ids,
        "narration_prompt_concept_ids": narration_prompt_concept_ids,
        "narration_prompt_concepts": narration_prompt_concepts,
        "tool_guidance_hash": tool_guidance_hash,
        "tool_guidance_preview": tool_guidance_preview,
        "gateway_enabled": gateway_enabled,
        "orchestrator_max_tool_invocations": orchestrator_max_tool_invocations,
        "orchestrator_tool_batch_cap": orchestrator_tool_batch_cap,
    }


def build_default_catalogue() -> MethodCatalogue:
    """Return a catalogue pre-populated with the baseline method set."""

    catalogue = MethodCatalogue()
    concept_search_input_schema = _concept_search_input_schema()
    concept_search_output_schema = _concept_search_output_schema()
    jira_search_output_schema = _jira_generic_output_schema("search")
    jira_get_issue_output_schema = _jira_generic_output_schema("get_issue")
    jira_add_comment_output_schema = _jira_generic_output_schema("add_comment")
    jira_transition_output_schema = _jira_generic_output_schema("transition")
    jira_create_issue_output_schema = _jira_generic_output_schema("create_issue")
    jira_update_issue_output_schema = _jira_generic_output_schema("update_issue")
    jira_link_issue_output_schema = _jira_generic_output_schema("link_issue")
    jira_get_myself_output_schema = _jira_generic_output_schema("get_myself")
    jira_get_auth_config_output_schema = _jira_get_auth_config_output_schema()
    gmail_list_messages_input_schema = Schema(
        required={"profile": str},
        optional={
            "query": str,
            "label_ids": list,
            "max_results": (int, type(None)),
            "maxResults": (int, type(None)),
        },
        allow_unknown=False,
        description="List Gmail messages for a profile with optional query/labels (read-only).",
    )
    gmail_get_message_input_schema = Schema(
        required={"profile": str, "message_id": str},
        optional={"format": str},
        allow_unknown=False,
        description="Fetch a Gmail message for a profile (formats: metadata|full|raw|minimal).",
    )
    gmail_get_attachment_input_schema = Schema(
        required={"profile": str, "message_id": str, "attachment_id": str},
        optional={},
        allow_unknown=False,
        description="Fetch a Gmail attachment for a profile (base64 payload).",
    )
    gmail_list_labels_input_schema = Schema(
        required={"profile": str},
        optional={},
        allow_unknown=False,
        description="List Gmail labels for a profile (read-only).",
    )
    gmail_modify_labels_input_schema = Schema(
        required={"profile": str, "message_id": str, "allow_mutation": bool},
        optional={
            "add_labels": list,
            "remove_labels": list,
        },
        allow_unknown=False,
        description="Add/remove labels on a Gmail message. Requires allow_mutation=true and gmail.modify scope.",
    )
    definitions: List[MethodDefinition] = [
        MethodDefinition(
            name="get_context",
            handler=_get_context,
            input_schema=Schema(
                required={},
                optional={},
                allow_unknown=False,
                description="get_context input: no parameters required",
            ),
            output_schema=Schema(
                required={
                    "llm_model": (str, type(None)),
                    "language": str,
                    "timestamp": str,
                },
                optional={
                    "user": (dict, type(None)),
                    "organisation": (dict, type(None)),
                    "llm_provider": (str, type(None)),
                    "fetch_counts_on_load": (bool, type(None)),
                    "note": (str, type(None)),
                },
                allow_unknown=True,
                description="get_context output: context info including user, org, llm_model (string), llm_provider, language. User/org managed client-side per JVNAUTOSCI-628.",
            ),
            category="read",
            description="Get current server-side context: active LLM model (string), provider, language preference, and runtime settings. NOTE: User and organisation information is managed client-side (localStorage) per JVNAUTOSCI-628 and may not be available here. Use when you need to know what model/language is configured.",
        ),
        MethodDefinition(
            name="get_client_capabilities",
            handler=_get_client_capabilities,
            input_schema=Schema(
                required={},
                optional={},
                allow_unknown=False,
                description="Return the current session's last reported client capabilities snapshot (no input parameters).",
            ),
            output_schema=Schema(
                required={
                    "success": bool,
                    "capabilities": (dict, type(None)),
                },
                optional={},
                allow_unknown=True,
                description=(
                    "Client capability snapshot as last reported by the browser (bounded, non-authoritative). "
                    "Returns capabilities=null if not available."
                ),
            ),
            category="read",
            description=(
                "Get the browser-reported client capability snapshot for the current session (speech synthesis, "
                "speech recognition, and basic audio hints). Use for debugging speech/narration behaviours without "
                "collecting high-fidelity fingerprinting data."
            ),
        ),
        MethodDefinition(
            name="chat_get_prompt_context",
            handler=_chat_get_prompt_context,
            input_schema=Schema(
                required={"namespace": str},
                optional={
                    "include_content": bool,
                    "max_chars": (int, type(None)),
                },
                allow_unknown=False,
                description=(
                    "Return the effective user-specific prompt context derived from Vontology "
                    "for the given authenticated namespace (concept ID), including which prompt "
                    "concept IDs are contributing to chat. Use for debugging what influences chat."
                ),
            ),
            output_schema=Schema(
                required={
                    "success": bool,
                    "namespace": str,
                    "prompt_concept_ids": list,
                    "prompt_concepts": list,
                    "prompt_text": str,
                    "prompt_count": int,
                },
                optional={
                    "error": str,
                    "behaviour_prompt_concept_ids": list,
                    "behaviour_prompt_concepts": list,
                    "narration_prompt_concept_ids": list,
                    "narration_prompt_concepts": list,
                    "resolved_templates": dict,
                },
                allow_unknown=False,
                description="User-specific chat prompt context for debugging and transparency.",
            ),
            category="read",
            description=(
                "Report which Vontology chat behaviour prompt concepts (including legacy "
                "`#V#von_llm_prompt`, linked via `#V#specific_to_von_user`) apply to the authenticated "
                "user namespace and optionally include their content."
            ),
        ),
        MethodDefinition(
            name="chat_introspect",
            handler=_chat_introspect,
            input_schema=Schema(
                required={"namespace": str},
                optional={
                    "organisation_concept_id": (str, type(None)),
                    "include_prompt_content": bool,
                    "include_tool_guidance_preview": bool,
                    "max_preview_chars": (int, type(None)),
                    "include_runtime_status": bool,
                },
                allow_unknown=False,
                description=(
                    "Return a compact snapshot of what influences chat for an authenticated namespace, "
                    "including active model information, user-specific prompt concept IDs, and a tool-guidance "
                    "fingerprint (hash)."
                ),
            ),
            output_schema=Schema(
                required={
                    "success": bool,
                    "namespace": str,
                    "organisation_concept_id": (str, type(None)),
                    "active_model_name": (str, type(None)),
                    "active_llm": (dict, type(None)),
                    "resolved_llm": (dict, type(None)),
                    "prompt_concept_ids": list,
                    "prompt_concepts": list,
                    "prompt_count": int,
                    "tool_guidance_hash": (str, type(None)),
                    "tool_guidance_preview": (str, type(None)),
                    "gateway_enabled": (bool, type(None)),
                    "orchestrator_max_tool_invocations": (int, type(None)),
                    "orchestrator_tool_batch_cap": (int, type(None)),
                },
                optional={"error": str},
                allow_unknown=False,
                description="Chat context introspection snapshot (safe, no secrets).",
            ),
            category="read",
            description=(
                "Introspect chat context influences for a user: model configuration, Vontology prompt concepts, "
                "and tool-guidance fingerprint. Useful for debugging and transparency."
            ),
        ),
        MethodDefinition(
            name="settings_get_public",
            handler=_settings_get_public,
            input_schema=Schema(
                required={},
                optional={
                    "user_concept_id": (str, type(None)),
                    "organisation_concept_id": (str, type(None)),
                },
                allow_unknown=False,
                description="Return non-secret settings and resolved LLM config for optional user/org scope.",
            ),
            output_schema=Schema(
                required={
                    "success": bool,
                    "settings": dict,
                },
                optional={"error": str},
                allow_unknown=True,
                description="Public settings snapshot (no secrets).",
            ),
            category="read",
            description=(
                "Return a safe subset of settings (no secrets), including the active LLM and resolved LLM when "
                "user/org IDs are provided."
            ),
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
            name="resolve_concept_by_name",
            handler=_resolve_concept_by_name,
            input_schema=_resolve_concept_by_name_input_schema(),
            output_schema=_resolve_concept_by_name_output_schema(),
            category="read",
            description=(
                "Resolve a Vontology concept deterministically from a user-provided surface form. "
                "Read-only: does not mutate concepts. Returns resolved/ambiguous/not_found with an audit trail. "
                "Supports language preferences, instance_of restriction, and optional code-string matching."
            ),
        ),
        MethodDefinition(
            name="upsert_text_relation",
            handler=_upsert_text_relation,
            input_schema=_upsert_text_relation_input_schema(),
            output_schema=_upsert_text_relation_output_schema(),
            category="write",
            description="Add or update ANY text relation (hasContent, hasDescription, hasNote, custom predicates, etc.). Use for attaching text content to concepts with flexible predicate types. More general than add_names which is specialized for hasName relations only.",
        ),
        MethodDefinition(
            name="get_text_relations",
            handler=_get_text_relations,
            input_schema=_get_text_relations_input_schema(),
            output_schema=_get_text_relations_output_schema(),
            category="read",
            description="Retrieve text relations for a concept, optionally filtered by predicate/language. Returns all text attachments (hasContent, hasDescription, hasName, etc.). Use to query what text is attached to a concept.",
        ),
        MethodDefinition(
            name="update_text_relation",
            handler=_update_text_relation,
            input_schema=_update_text_relation_input_schema(),
            output_schema=_update_text_relation_output_schema(),
            category="write",
            description="Modify the text content of an existing text relation by relation ID. Updates the text value while preserving the relation structure. Use when you need to change existing attached text.",
        ),
        MethodDefinition(
            name="delete_text_relation",
            handler=_delete_text_relation,
            input_schema=_delete_text_relation_input_schema(),
            output_schema=_delete_text_relation_output_schema(),
            category="write",
            description="Delete a specific text relation by relation ID or by predicate+text match. Optionally garbage-collects orphaned text values. Use to remove unwanted text attachments from concepts.",
        ),
        MethodDefinition(
            name="get_text_relations_summary",
            handler=_get_text_relations_summary,
            input_schema=_get_text_relations_summary_input_schema(),
            output_schema=_get_text_relations_summary_output_schema(),
            category="read",
            description="Return a lightweight summary of text relations for a concept: counts + relation IDs grouped by predicate/language (no full text bodies). Use to quickly decide what to fetch next.",
        ),
        MethodDefinition(
            name="upsert_singleton_text_relation",
            handler=_upsert_singleton_text_relation,
            input_schema=_upsert_singleton_text_relation_input_schema(),
            output_schema=_upsert_singleton_text_relation_output_schema(),
            category="write",
            description="Upsert a text relation and enforce singleton semantics for (concept, predicate, language) by replacing any other relations in the same group.",
        ),
        MethodDefinition(
            name="concept_exists",
            handler=_concept_exists,
            input_schema=_concept_exists_input_schema(),
            output_schema=_concept_exists_output_schema(),
            category="read",
            description="Minimal existence/accessibility check for a concept_id. Use before expensive fetch operations or when you need to validate user input.",
        ),
        MethodDefinition(
            name="fetch_concept_content",
            handler=_fetch_concept_content,
            input_schema=_fetch_concept_content_input_schema(),
            output_schema=None,
            category="read",
            description="Fetch rendered markdown content for a concept (content_html + md_content + raw_doc). Use reconstruct_md=false to avoid masking missing md_content.",
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
            description="Download PDF of an arXiv paper, then store it in the configured blob store (local or OpenStack Swift). The external arXiv tool writes into a local cache directory; this tool returns both the local cache file_path and a durable storage.uri. Use when user asks to download/save/fetch a paper.",
        ),
        MethodDefinition(
            name="finalise_cached_paper",
            handler=_finalise_cached_paper,
            input_schema=_finalise_cached_paper_input_schema(),
            output_schema=_finalise_cached_paper_output_schema(),
            category="write",
            timeout_sec=30.0,
            description="Take an already-cached arXiv PDF (in the local arXiv cache directory), upload it to the configured durable blob store, and register a #V#computer_file_copy instance with authoritative blob metadata text relations. Use when the PDF is already cached and you need a definitive durable URI + ontology record.",
        ),
        MethodDefinition(
            name="list_papers",
            handler=_list_papers,
            input_schema=_list_papers_input_schema(),
            output_schema=_list_papers_output_schema(),
            category="read",
            timeout_sec=15.0,
            description="List arXiv papers available in the local cache directory used by the external arXiv toolchain. This may not reflect all documents stored in the blob store. Use when user asks 'what papers do I have?' or similar.",
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
        MethodDefinition(
            name="read_file_copy",
            handler=_read_file_copy,
            input_schema=_read_file_copy_input_schema(),
            output_schema=_read_file_copy_output_schema(),
            category="read",
            timeout_sec=20.0,
            description=(
                "Read the content of an uploaded file-copy stored in the blob store. "
                "Use when the user asks to read, summarise, analyse, or extract information from "
                "a file they uploaded or an artefact stored as a #V#computer_file_copy. "
                "Requires authenticated user context and the file-copy concept_id."
            ),
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
            name="resilient_extract_url",
            handler=_resilient_extract_url,
            input_schema=_resilient_extract_url_input_schema(),
            output_schema=_resilient_extract_url_output_schema(),
            category="read",
            timeout_sec=30.0,
            description=(
                "Extract main text from a URL with deterministic fallbacks. First tries direct extraction; if the page is empty/blocked "
                "(common for JavaScript-rendered profile pages), it falls back to web search and attempts extraction from a small set of "
                "candidate URLs. Returns content plus provenance (attempted URLs and search query)."
            ),
        ),
        # Gmail MCP tools (read-only surface)
        MethodDefinition(
            name="gmail_list_messages",
            handler=_gmail_list_messages,
            input_schema=gmail_list_messages_input_schema,
            output_schema=Schema(
                required={},
                optional={},
                allow_unknown=True,
                description="Gmail API list response",
            ),
            category="read",
            timeout_sec=20.0,
            description="List Gmail messages for a profile with optional query and label filters. Read-only; relies on pre-provisioned tokens per profile.",
        ),
        MethodDefinition(
            name="gmail_get_message",
            handler=_gmail_get_message,
            input_schema=gmail_get_message_input_schema,
            output_schema=Schema(
                required={},
                optional={},
                allow_unknown=True,
                description="Gmail API message response",
            ),
            category="read",
            timeout_sec=20.0,
            description="Fetch a Gmail message for a profile. Supports Gmail API formats metadata|full|raw|minimal. Read-only; profile token required.",
        ),
        MethodDefinition(
            name="gmail_get_attachment",
            handler=_gmail_get_attachment,
            input_schema=gmail_get_attachment_input_schema,
            output_schema=Schema(
                required={},
                optional={},
                allow_unknown=True,
                description="Gmail API attachment response",
            ),
            category="read",
            timeout_sec=20.0,
            description="Fetch a Gmail attachment for a profile (base64 data). Read-only; profile token required.",
        ),
        MethodDefinition(
            name="gmail_list_labels",
            handler=_gmail_list_labels,
            input_schema=gmail_list_labels_input_schema,
            output_schema=Schema(
                required={},
                optional={},
                allow_unknown=True,
                description="Gmail API labels response",
            ),
            category="read",
            timeout_sec=15.0,
            description="List Gmail labels for a profile. Read-only; useful to discover label IDs for queries.",
        ),
        MethodDefinition(
            name="gmail_modify_labels",
            handler=_gmail_modify_labels,
            input_schema=gmail_modify_labels_input_schema,
            output_schema=Schema(
                required={},
                optional={},
                allow_unknown=True,
                description="Gmail API modify response",
            ),
            category="write",
            timeout_sec=20.0,
            description="Add/remove labels on a Gmail message. Requires allow_mutation=true and profile with gmail.modify scope.",
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
            name="search_concept_descriptions",
            handler=_search_concept_descriptions,
            input_schema=_search_concept_descriptions_input_schema(),
            output_schema=_search_concept_descriptions_output_schema(),
            category="read",
            timeout_sec=30.0,
            description=(
                "Semantic search over concept descriptions only (hasDescription text relations) for the current namespace. "
                "Use when user asks to search the knowledge base but only within concept descriptions."
            ),
        ),
        MethodDefinition(
            name="get_related_concepts",
            handler=_get_related_concepts,
            input_schema=_get_related_concepts_input_schema(),
            output_schema=_get_related_concepts_output_schema(),
            category="read",
            timeout_sec=30.0,
            description=(
                "Find concepts with similar descriptions (vector similarity) within the current namespace. "
                "Returns description chunks and metadata for related concepts."
            ),
        ),
        MethodDefinition(
            name="index_concept_text",
            handler=_index_concept_text,
            input_schema=_index_concept_text_input_schema(),
            output_schema=_index_concept_text_output_schema(),
            category="write",
            timeout_sec=60.0,
            description=(
                "Force reindex a specific concept's text relations into RAG for the current namespace. "
                "Useful after bulk edits or when RAG appears stale."
            ),
        ),
        # Jira MCP tools
        MethodDefinition(
            name="jira_search",
            handler=_jira_search,
            input_schema=_jira_search_input_schema(),
            output_schema=jira_search_output_schema,
            category="read",
            timeout_sec=20.0,
            description="Run a JQL query against Jira. Use when you need to find issues by status, assignee, project, or other fields. Requires valid ATLASSIAN_BASE_URL, ATLASSIAN_EMAIL, and ATLASSIAN_API_TOKEN in the environment. Returns the Jira search response including issues array.",
        ),
        MethodDefinition(
            name="jira_get_issue",
            handler=_jira_get_issue,
            input_schema=_jira_get_issue_input_schema(),
            output_schema=jira_get_issue_output_schema,
            category="read",
            timeout_sec=15.0,
            description="Fetch full details for a Jira issue by key (e.g., JVNAUTOSCI-123). Use when you need fields or transitions for a specific issue.",
        ),
        MethodDefinition(
            name="jira_add_comment",
            handler=_jira_add_comment,
            input_schema=_jira_add_comment_input_schema(),
            output_schema=jira_add_comment_output_schema,
            category="write",
            timeout_sec=15.0,
            description="Add a comment to a Jira issue. Use to log investigation notes or status updates. Requires issue key and comment text.",
        ),
        MethodDefinition(
            name="jira_create_issue",
            handler=_jira_create_issue,
            input_schema=_jira_create_issue_input_schema(),
            output_schema=jira_create_issue_output_schema,
            category="write",
            timeout_sec=20.0,
            description=(
                "Create a Jira issue with safety guardrails. Default dry_run=true (no mutation). "
                "Writes are allowed only for allow-listed projects (default JVNAUTOSCI). "
                "To execute, pass dry_run=false and either approved=true (per write) or execute=true with VON_INTERNAL_MCP_JIRA_EXECUTE_MODE=1."
            ),
        ),
        MethodDefinition(
            name="jira_update_issue",
            handler=_jira_update_issue,
            input_schema=_jira_update_issue_input_schema(),
            output_schema=jira_update_issue_output_schema,
            category="write",
            timeout_sec=20.0,
            description=(
                "Update a Jira issue with safety guardrails. Default dry_run=true (no mutation). "
                "Writes are blocked unless the issue belongs to an allow-listed project. "
                "To execute, pass dry_run=false and either approved=true or execute=true with VON_INTERNAL_MCP_JIRA_EXECUTE_MODE=1."
            ),
        ),
        MethodDefinition(
            name="jira_link_issue",
            handler=_jira_link_issue,
            input_schema=_jira_link_issue_input_schema(),
            output_schema=jira_link_issue_output_schema,
            category="write",
            timeout_sec=20.0,
            description=(
                "Create a Jira issue link (e.g. Relates) with safety guardrails. Default dry_run=true (no mutation). "
                "Both issue projects must be allow-listed. "
                "To execute, pass dry_run=false and either approved=true or execute=true with VON_INTERNAL_MCP_JIRA_EXECUTE_MODE=1."
            ),
        ),
        MethodDefinition(
            name="jira_transition",
            handler=_jira_transition_issue,
            input_schema=_jira_transition_input_schema(),
            output_schema=jira_transition_output_schema,
            category="write",
            timeout_sec=15.0,
            description="Transition a Jira issue using a transition ID. Use when you need to move an issue through workflow states after retrieving available transitions in Jira.",
        ),
        MethodDefinition(
            name="jira_get_myself",
            handler=_jira_get_myself,
            input_schema=_jira_get_myself_input_schema(),
            output_schema=jira_get_myself_output_schema,
            category="read",
            timeout_sec=10.0,
            description=(
                "Return the Jira user profile for the currently configured Atlassian credentials. "
                "Useful for diagnosing permission-related 404s (different accounts see different issues)."
            ),
        ),
        MethodDefinition(
            name="jira_get_auth_config",
            handler=_jira_get_auth_config,
            input_schema=_jira_get_auth_config_input_schema(),
            output_schema=jira_get_auth_config_output_schema,
            category="read",
            description=(
                "Inspect Jira auth configuration (base URL, email, whether a token is present) from environment variables. "
                "Does not contact Jira and never returns the token. Use when Jira calls return 401 and you need to confirm which account is configured."
            ),
        ),
        MethodDefinition(
            name="rag_get_status",
            handler=_rag_get_status,
            input_schema=Schema(
                required={}, optional={}, allow_unknown=True, description="No input"
            ),
            output_schema=None,
            category="read",
            description="Get RAG status: totals, eligible counts, indexed/pending/failed/skipped. Mirrors /admin/rag_status.",
        ),
        MethodDefinition(
            name="rag_list_collections",
            handler=_rag_list_collections,
            input_schema=Schema(
                required={},
                optional={"namespace": (str, type(None))},
                allow_unknown=True,
                description="List available RAG collections/sources for the current namespace",
            ),
            output_schema=None,
            category="read",
            description=(
                "List available RAG collections/sources for the current user/namespace. "
                "Use when user asks 'what is in my RAG store?' or needs to disambiguate KA sessions vs chat sessions vs vector chunks."
            ),
        ),
        MethodDefinition(
            name="rag_list_indexed",
            handler=_rag_list_indexed,
            input_schema=Schema(
                required={},
                optional={
                    "collection": (str, type(None)),
                    "limit": (int,),
                    "offset": (int,),
                    "namespace": (str, type(None)),
                },
                allow_unknown=True,
                description="List indexed sessions with optional namespace filter",
            ),
            output_schema=None,
            category="read",
            description="List all RAG-indexed interaction sessions (KA sessions) for the current user. Returns total count and session metadata. Use this to answer 'how many KA sessions are indexed'. Namespace filtered automatically.",
        ),
        MethodDefinition(
            name="rag_get_item",
            handler=_rag_get_item,
            input_schema=Schema(
                required={"session_id": str},
                optional={
                    "namespace": (str, type(None)),
                    "collection": (str, type(None)),
                },
                allow_unknown=True,
                description="Fetch one indexed session with optional namespace filter",
            ),
            output_schema=None,
            category="read",
            description="Get one indexed item (session) with a safe text preview. Respects namespace isolation.",
        ),
        MethodDefinition(
            name="rag_sync_text_relations",
            handler=_rag_sync_text_relations,
            input_schema=Schema(
                required={},
                optional={
                    "namespace": (str, type(None)),
                    "predicates": (list,),
                    "languages": (list,),
                    "limit": (int,),
                    "batch_size": (int,),
                },
                allow_unknown=True,
                description=(
                    "Index Vontology text relations (text_relations + text_values) into the vector-store for a namespace."
                ),
            ),
            output_schema=None,
            category="write",
            timeout_sec=60.0,
            description=(
                "Upsert Vontology text relations into the RAG vector-store so they are discoverable via search_knowledge_base. "
                "Requires an explicit namespace."
            ),
        ),
    ]

    for definition in definitions:
        catalogue.register(definition)

    return catalogue
