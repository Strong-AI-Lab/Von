"""Default method catalogue for the internal MCP gateway.

SCHEMA DESIGN GUIDELINES
========================
When defining input schemas for new tools, follow these conventions to ensure
LLM compatibility:

1. **Include `namespace` as an optional parameter** on user-facing tools, even if
   the handler ignores it. LLMs learn parameter patterns across tools and will
   attempt to pass `namespace` if they see it accepted elsewhere. Use:
       "namespace": (str, type(None)),

2. **Prefer `allow_unknown=True`** for input schemas unless you have a specific
   reason to reject unexpected fields. Strict schemas (`allow_unknown=False`)
   will reject calls when LLMs hallucinate extra parameters.

3. **Output schemas can be strict** (`allow_unknown=False`) since they validate
   what *we* return, not what the LLM sends.

4. **Document ignored parameters** with a comment so future maintainers understand
   why they're accepted but unused.

See JVNAUTOSCI-1044 for an example of namespace rejection breaking tool calls.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime
from typing import Any, List, Mapping, Sequence

from .dynamic_tool_loader import load_dynamic_method_definitions
from .gateway import MethodCatalogue, MethodDefinition
from .schemas import Schema, make_error_response
from .workflow_surface_capabilities import (
    build_workflow_surface_capability_matrix,
)
from src.backend.services.prompt_template_service import PromptTemplateService

logger = logging.getLogger(__name__)

_JIRA_ISSUE_KEY_PATTERN = re.compile(
    r"\b([A-Z][A-Z0-9]{1,24})-(\d+)\b",
    re.IGNORECASE,
)
_DEFAULT_JIRA_BASE_URL = "https://naoinstitute.atlassian.net"


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _get_internal_mcp_chat_orchestrator_cls():
    """Lazy-import orchestrator class to avoid heavy import side effects."""
    from .orchestrator import InternalMCPChatOrchestrator

    return InternalMCPChatOrchestrator


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
    from ...services.relationship_write_service import detect_vacuous_typing

    concept_id = kwargs.get("concept_id")
    if not concept_id:
        raise ValueError("concept_id is required")

    concept = get_concept_by_concept_id(concept_id=concept_id)

    # Use shared enrichment logic (migrate-on-read + fetch names from text relations)
    if not concept:
        return concept

    concept = enrich_concept_with_text_relations(concept)

    # Detect vacuous typing (soft warning for agents to repair)
    vacuous_warning = detect_vacuous_typing(concept)
    if vacuous_warning:
        concept["_vacuous_typing_warning"] = vacuous_warning

    include_relations_arg1 = bool(kwargs.get("include_relations_arg1"))
    include_relations_any_arg = bool(kwargs.get("include_relations_any_arg"))
    include_text_relations_arg1 = kwargs.get("include_text_relations_arg1", False)
    predicate_filter = kwargs.get("predicate_filter")
    limit = kwargs.get("limit")
    offset = kwargs.get("offset")
    include_concept_preview = kwargs.get("include_concept_preview", True)
    include_uncertain = bool(kwargs.get("include_uncertain", False))
    uncertainty_mode = kwargs.get("uncertainty_mode")
    uncertainty_statuses = kwargs.get("uncertainty_statuses")

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
            include_uncertain=include_uncertain,
            uncertainty_mode=uncertainty_mode,
            uncertainty_statuses=uncertainty_statuses,
        )
        concept["relations"] = relations_payload

    return concept


def _find_relations_with_argument(**kwargs):
    from ...services.concept_relation_service import find_relations_with_argument

    concept_id = kwargs.get("concept_id")
    if concept_id is None or not str(concept_id).strip():
        raise ValueError("concept_id is required")

    return find_relations_with_argument(
        concept_id=str(concept_id),
        argument_index=kwargs.get("argument_index"),
        predicate_filter=kwargs.get("predicate_filter"),
        relation_kind=kwargs.get("relation_kind"),
        scope=kwargs.get("scope"),
        include_text_snippets=bool(kwargs.get("include_text_snippets", False)),
        include_concept_preview=bool(kwargs.get("include_concept_preview", True)),
        limit=kwargs.get("limit"),
        offset=kwargs.get("offset"),
        sort_by=kwargs.get("sort_by"),
        include_uncertain=bool(kwargs.get("include_uncertain", False)),
        uncertainty_mode=kwargs.get("uncertainty_mode"),
        uncertainty_statuses=kwargs.get("uncertainty_statuses"),
    )


def _get_context(**kwargs):
    from ...services.settings_service import (
        resolve_llm_setting,
        get_preferred_language,
        get_setting,
    )
    from datetime import datetime, timezone

    # Get user/org context from session for resolved LLM setting
    user_concept_id = None
    org_concept_id = None
    try:
        from flask import session, has_request_context

        if has_request_context():
            user_concept_id = session.get("user_concept_id")
            org_concept_id = session.get("organisation_concept_id")
    except Exception:
        pass

    # Get resolved LLM setting (user > org precedence, no global fallback)
    model_setting = resolve_llm_setting(
        user_concept_id=user_concept_id, org_concept_id=org_concept_id
    )

    context = {
        "user": None,
        "organisation": None,
        "llm_model": (
            model_setting.get("model") if isinstance(model_setting, dict) else None
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
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: arxiv_id",
            details={"missing": ["arxiv_id"]},
            suggestions=[
                "Provide the arXiv ID (e.g., '2301.12345' or 'arxiv:2301.12345')"
            ],
        )

    async def _async_metadata():
        try:
            proxy = await get_arxiv_proxy()
            return await proxy.get_paper_metadata(arxiv_id=arxiv_id)
        except ArxivProxyError as e:
            return make_error_response(
                "arxiv_proxy_error",
                str(e),
                details={"arxiv_id": arxiv_id, "exception_type": "ArxivProxyError"},
            )
        except Exception as e:
            return make_error_response(
                "exception",
                f"Unexpected error: {e}",
                details={"arxiv_id": arxiv_id, "exception_type": type(e).__name__},
            )

    return _run_async_compat(_async_metadata)


_CREATE_CONCEPTS_SCOPE_DEFAULT = "user_org_default"
_CREATE_CONCEPTS_SCOPE_ORGANISATION_GENERAL = "organisation_general"
_CREATE_CONCEPTS_SCOPE_GLOBAL_GENERAL = "global_general"
_CREATE_CONCEPTS_SCOPE_MODE_ALIASES: dict[str, str] = {
    "default": _CREATE_CONCEPTS_SCOPE_DEFAULT,
    "user_org_default": _CREATE_CONCEPTS_SCOPE_DEFAULT,
    "user_org": _CREATE_CONCEPTS_SCOPE_DEFAULT,
    "private": _CREATE_CONCEPTS_SCOPE_DEFAULT,
    "organisation_general": _CREATE_CONCEPTS_SCOPE_ORGANISATION_GENERAL,
    "organization_general": _CREATE_CONCEPTS_SCOPE_ORGANISATION_GENERAL,
    "org_general": _CREATE_CONCEPTS_SCOPE_ORGANISATION_GENERAL,
    "global_general": _CREATE_CONCEPTS_SCOPE_GLOBAL_GENERAL,
    "global": _CREATE_CONCEPTS_SCOPE_GLOBAL_GENERAL,
    "public": _CREATE_CONCEPTS_SCOPE_GLOBAL_GENERAL,
}


def _normalise_create_concepts_scope_mode(raw_value: Any) -> str | None:
    if not isinstance(raw_value, str):
        return None
    cleaned = raw_value.strip().lower().replace("-", "_")
    if not cleaned:
        return None
    return _CREATE_CONCEPTS_SCOPE_MODE_ALIASES.get(cleaned)


def _create_concepts(**kwargs):
    from ...vontology.utils_vontology import create_vontology_concept
    from ...vontology.code_concepts_registry import PREDICATE_TYPE_ID
    from ...services.create_concepts_duplicate_guard_service import (
        build_duplicate_prevented_create_concepts_result,
        find_existing_concept_for_create_concepts,
    )
    from ...services.create_concepts_parent_resolution_service import (
        resolve_parent_for_create_concepts,
    )

    parent_id: str | None = kwargs.get("parent_id")
    concepts = kwargs.get("concepts", [])
    raw_namespace = kwargs.get("namespace")
    namespace = (
        raw_namespace.strip()
        if isinstance(raw_namespace, str) and raw_namespace.strip()
        else None
    )
    allow_duplicate_instances_raw = kwargs.get("allow_duplicate_instances", False)
    allow_duplicate_instances = (
        allow_duplicate_instances_raw
        if isinstance(allow_duplicate_instances_raw, bool)
        else str(allow_duplicate_instances_raw).strip().lower()
        in {"1", "true", "yes", "on"}
    )
    raw_scope_mode = kwargs.get("scope_mode")
    if raw_scope_mode is None:
        raw_scope_mode = kwargs.get("visibility_scope_mode")
    scope_mode = _normalise_create_concepts_scope_mode(raw_scope_mode)
    if (
        raw_scope_mode is not None
        and isinstance(raw_scope_mode, str)
        and raw_scope_mode.strip()
        and scope_mode is None
    ):
        return make_error_response(
            "invalid_parameter",
            f"Invalid scope_mode '{raw_scope_mode}'.",
            details={
                "scope_mode": raw_scope_mode,
                "supported_scope_modes": [
                    _CREATE_CONCEPTS_SCOPE_DEFAULT,
                    _CREATE_CONCEPTS_SCOPE_ORGANISATION_GENERAL,
                    _CREATE_CONCEPTS_SCOPE_GLOBAL_GENERAL,
                ],
            },
            suggestions=[
                "Use scope_mode='user_org_default' for authenticated default scoping",
                "Use scope_mode='organisation_general' for organisation-scoped shared concepts",
                "Use scope_mode='global_general' for broadly visible concepts",
            ],
        )

    from ...services.workflow_event_integration_service import resolve_event_actor_context

    actor_user_id, actor_org_id = resolve_event_actor_context(
        user_id=kwargs.get("created_by_concept_id"),
        org_id=kwargs.get("organisation_concept_id") or kwargs.get("org_id"),
        namespace=namespace,
    )

    if scope_mode == _CREATE_CONCEPTS_SCOPE_ORGANISATION_GENERAL and not actor_org_id:
        return make_error_response(
            "missing_organisation_context",
            "organisation_general scope requires an organisation context.",
            details={
                "scope_mode": scope_mode,
                "namespace": namespace,
                "created_by_concept_id": actor_user_id,
                "organisation_concept_id": actor_org_id,
            },
            suggestions=[
                "Provide namespace in #V#user@org form",
                "Or include organisation_concept_id in the tool payload",
                "Or use scope_mode='user_org_default' or 'global_general'",
            ],
        )

    if not parent_id:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: parent_id",
            details={"missing": ["parent_id"]},
            suggestions=[
                "Provide a parent type concept ID, e.g., parent_id='#V#person' or '#V#physical_object'"
            ],
        )

    # Block #V#thing as parent type (JVNAUTOSCI-1072).
    # #V#thing is the universal top type - everything is implicitly a thing.
    # Explicit relationships to it provide no semantic value.
    BLOCKED_PARENT_TYPES = {"#V#thing"}
    if parent_id in BLOCKED_PARENT_TYPES:
        # Analyse what's being created to give better guidance
        concept_names = [c.get("name", "") for c in concepts if isinstance(c, dict)]
        creating_multiple = len(concepts) > 1
        kind_requested = "instance"
        if concepts and isinstance(concepts[0], dict):
            kind_requested = (concepts[0].get("kind") or "type").strip().lower()

        # Build contextual suggestions
        suggestions = [
            "1. FIRST look for an existing parent type that is a semantically close "
            "generalisation of what you're creating by querying search_concepts with a "
            "domain/category-specific query.",
        ]

        if creating_multiple or kind_requested == "instance":
            suggestions.append(
                "2. If no suitable parent exists, CREATE A NEW PARENT TYPE FIRST as a subtype of an "
                "appropriate root parent (for example #V#event for things that happen in time, "
                "#V#physical_object for tangible items, #V#abstract_object for ideas/concepts, "
                "#V#information_object for documents/recordings). "
                "The new parent should be a semantically close generalisation of the child."
            )
            suggestions.append(
                "3. Then create your instances as instances of that specific type, not the broad category"
            )
            suggestions.append(
                "Example: For Otter recordings, first create type '#V#otter_recording_session' "
                "as subtype of #V#event, then create instances of #V#otter_recording_session"
            )
        else:
            suggestions.append(
                "2. Consider semantically close parent types: #V#physical_object, #V#abstract_object, "
                "#V#event, #V#process, #V#information_object, #V#living_organism. "
                "If none fits, create one that does."
            )

        return make_error_response(
            "blocked_parent_type",
            f"Cannot use '{parent_id}' as parent type. Every concept needs a semantically close parent. "
            "If no parent exists, create a specific parent type first. "
            "Using the universal top type is not allowed.",
            details={
                "blocked_parent_id": parent_id,
                "concepts_count": len(concepts),
                "kind_requested": kind_requested,
                "sample_names": concept_names[:3] if concept_names else [],
            },
            suggestions=suggestions,
        )

    parent_resolution = resolve_parent_for_create_concepts(parent_id)
    if not parent_resolution.success:
        canonical_parent = parent_resolution.canonical_parent_id
        related_ids = [
            cid
            for cid in [
                canonical_parent,
                *parent_resolution.fallback_candidates_checked,
            ]
            if cid
        ]
        suggestions = [
            "Create the parent concept first",
            "Search for similar concepts using search_concepts",
        ]
        if parent_resolution.fallback_candidates_checked:
            suggestions.append(
                "For workflow concepts, prefer an existing workflow supertype "
                f"({', '.join(parent_resolution.fallback_candidates_checked)})"
            )
        return make_error_response(
            "parent_not_found",
            f"Parent concept '{canonical_parent}' not found. Create it first or check the ID.",
            details={
                "canonical_parent_id": canonical_parent,
                "original_parent_id": parent_id,
                "fallback_candidates_checked": list(
                    parent_resolution.fallback_candidates_checked
                ),
            },
            suggestions=suggestions,
            related_concept_ids=related_ids,
        )
    assert parent_resolution.resolved_parent_id is not None
    validated_parent_id: str = parent_resolution.resolved_parent_id
    if not concepts or not isinstance(concepts, list):
        return make_error_response(
            "missing_parameter",
            "Missing or invalid 'concepts' array",
            details={"missing": ["concepts"]},
            suggestions=[
                "Provide an array of concept objects, e.g., concepts=[{name: 'MyType'}]"
            ],
        )

    results = []
    for concept_data in concepts:
        if not isinstance(concept_data, dict):
            results.append({"error": "Concept must be an object", "data": concept_data})
            continue

        name = concept_data.get("name")
        kind = (concept_data.get("kind") or "type").strip().lower()

        if not name:
            results.append(
                {"error": "Concept missing required 'name' field", "data": concept_data}
            )
            continue

        # Map kind to create_as_instance parameter
        if kind == "individual":
            kind = "instance"

        if kind == "predicate":
            create_as_instance = True
            parent_id_for_concept = PREDICATE_TYPE_ID
        else:
            create_as_instance = kind == "instance"
            parent_id_for_concept = validated_parent_id

        duplicate_match = find_existing_concept_for_create_concepts(
            concept_name=str(name),
            kind=kind,
            parent_id_for_concept=parent_id_for_concept,
            preferred_language="en-NZ",
            allow_duplicate_instances=allow_duplicate_instances,
        )
        if duplicate_match is not None:
            result = build_duplicate_prevented_create_concepts_result(
                requested_name=str(name),
                requested_kind=kind,
                existing_concept_id=duplicate_match.existing_concept_id,
                guard_scope=duplicate_match.guard_scope,
                match_source=duplicate_match.match_source,
            )
            result["concept_id"] = duplicate_match.existing_concept_id
            results.append(result)
            continue

        result = create_vontology_concept(
            parent_id=parent_id_for_concept,
            new_concept_name=name,
            create_as_instance=create_as_instance,
            description=concept_data.get("description"),
            notes=concept_data.get("notes"),
            created_by_concept_id=actor_user_id,
            organisation_concept_id=actor_org_id,
            event_namespace=namespace,
            visibility_scope_mode=scope_mode,
        )
        # Enrich result with the requested name for traceability and surface
        # the canonical created concept_id at a stable top-level key so UI
        # summaries can reliably name what was created.
        result["requested_name"] = name
        result["requested_kind"] = kind
        concept_id_value = result.get("concept_id")
        if not isinstance(concept_id_value, str) or not concept_id_value.strip():
            nested_concept = result.get("concept")
            if isinstance(nested_concept, dict):
                nested_id = nested_concept.get("concept_id")
                if isinstance(nested_id, str) and nested_id.strip():
                    concept_id_value = nested_id
        if not isinstance(concept_id_value, str) or not concept_id_value.strip():
            canonical_id = result.get("canonical_concept_id")
            if isinstance(canonical_id, str) and canonical_id.strip():
                concept_id_value = canonical_id
        if not isinstance(concept_id_value, str) or not concept_id_value.strip():
            existing_id = result.get("existing_concept_id")
            if isinstance(existing_id, str) and existing_id.strip():
                concept_id_value = existing_id
        if isinstance(concept_id_value, str) and concept_id_value.strip():
            result["concept_id"] = concept_id_value.strip()
        results.append(result)

    # Count different outcome types for summary
    successful = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
    already_exists = sum(
        1
        for r in results
        if isinstance(r, dict) and r.get("error_code") == "already_exists"
    )
    created_concept_ids = [
        str(r.get("concept_id")).strip()
        for r in results
        if isinstance(r, dict)
        and r.get("success")
        and isinstance(r.get("concept_id"), str)
        and str(r.get("concept_id")).strip()
    ]
    applied_scope_modes = sorted(
        {
            str(
                (((r.get("concept") or {}).get("creation_visibility") or {}).get(
                    "effective_scope_mode"
                ))
            ).strip()
            for r in results
            if isinstance(r, dict)
            and isinstance(r.get("concept"), dict)
            and isinstance((r.get("concept") or {}).get("creation_visibility"), dict)
            and str(
                (((r.get("concept") or {}).get("creation_visibility") or {}).get(
                    "effective_scope_mode"
                ))
            ).strip()
        }
    )

    return {
        "results": results,
        "total": len(concepts),
        "successful": successful,
        "already_existed": already_exists,
        "failed": len(concepts) - successful - already_exists,
        "created_concept_ids": created_concept_ids,
        "parent_id_used": validated_parent_id,  # Canonicalised parent ID that was actually used
        "parent_resolution": parent_resolution.to_dict(),
        "scope_selection": {
            "requested_scope_mode": scope_mode or _CREATE_CONCEPTS_SCOPE_DEFAULT,
            "scope_mode_source": (
                "request.scope_mode"
                if isinstance(raw_scope_mode, str) and raw_scope_mode.strip()
                else "default.user_org_default"
            ),
            "created_by_concept_id": actor_user_id,
            "organisation_concept_id": actor_org_id,
            "effective_scope_modes": applied_scope_modes,
            "namespace": namespace,
        },
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
    provenance = kwargs.get("provenance")
    namespace = kwargs.get("namespace")

    if not concept_id:
        return make_error_response(
            "missing_parameter",
            "Missing 'concept_id' parameter",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID to attach text to"],
        )
    if not predicate:
        return make_error_response(
            "missing_parameter",
            "Missing 'predicate' parameter",
            details={"missing": ["predicate"]},
            suggestions=[
                "Provide a predicate like 'hasContent', 'hasDescription', or a custom predicate"
            ],
        )
    if not text:
        return make_error_response(
            "missing_parameter",
            "Missing 'text' parameter",
            details={"missing": ["text"]},
            suggestions=["Provide the text content to attach"],
        )

    try:
        result = upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate=predicate,
            text=text,
            lang=language,
            provenance=provenance if isinstance(provenance, dict) else None,
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
        return make_error_response(
            "exception",
            f"Failed to upsert text relation: {exc}",
            details={"exception_type": type(exc).__name__},
        )


def _get_text_relations(**kwargs):
    from ...services.text_value_service import get_texts_for_concept

    concept_id = kwargs.get("concept_id")
    predicate = kwargs.get("predicate")
    language = kwargs.get("language")
    limit = kwargs.get("limit", 50)

    if not concept_id:
        return make_error_response(
            "missing_parameter",
            "Missing 'concept_id' parameter",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID to retrieve text relations for"],
        )

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
        return make_error_response(
            "exception",
            f"Failed to get text relations: {exc}",
            details={"exception_type": type(exc).__name__},
        )


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
        return make_error_response(
            "missing_parameter",
            "Missing 'concept_id' parameter",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID that owns the text relation"],
        )
    if not relation_id:
        return make_error_response(
            "missing_parameter",
            "Missing 'relation_id' parameter",
            details={"missing": ["relation_id"]},
            suggestions=["Use get_text_relations to find the relation_id to update"],
        )
    if not new_text:
        return make_error_response(
            "missing_parameter",
            "Missing 'new_text' parameter",
            details={"missing": ["new_text"]},
            suggestions=["Provide the new text content"],
        )

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
        return make_error_response(
            "exception",
            f"Failed to update text relation: {exc}",
            details={"exception_type": type(exc).__name__},
        )


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
        return make_error_response(
            "missing_parameter",
            "Missing 'concept_id' parameter",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID that owns the text relation"],
        )

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
            return make_error_response(
                "missing_parameter",
                "Must provide either relation_id or both predicate and text",
                details={"missing": ["relation_id", "predicate+text"]},
                suggestions=[
                    "Provide relation_id for direct deletion, or both predicate and text for matched deletion"
                ],
            )
    except Exception as exc:
        return make_error_response(
            "exception",
            f"Failed to delete text relation: {exc}",
            details={"exception_type": type(exc).__name__},
        )


def _get_text_relations_summary(**kwargs):
    from ...services.text_value_service import get_text_relations_summary

    concept_id = kwargs.get("concept_id")
    predicates = kwargs.get("predicates")
    languages = kwargs.get("languages")
    max_relation_ids_per_group = kwargs.get("max_relation_ids_per_group", 25)

    if not concept_id:
        return make_error_response(
            "missing_parameter",
            "Missing 'concept_id' parameter",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID to get text relations summary for"],
        )

    try:
        return get_text_relations_summary(
            concept_id,
            predicates=predicates,
            languages=languages,
            max_relation_ids_per_group=max_relation_ids_per_group,
        )
    except Exception as exc:
        return make_error_response(
            "exception",
            f"Failed to summarise text relations: {exc}",
            details={"exception_type": type(exc).__name__},
        )


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
        return make_error_response(
            "missing_parameter",
            "Missing 'concept_id' parameter",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID to attach text to"],
        )
    if not predicate:
        return make_error_response(
            "missing_parameter",
            "Missing 'predicate' parameter",
            details={"missing": ["predicate"]},
            suggestions=["Provide a predicate like 'hasContent' or 'hasDescription'"],
        )
    if not text:
        return make_error_response(
            "missing_parameter",
            "Missing 'text' parameter",
            details={"missing": ["text"]},
            suggestions=["Provide the text content to attach"],
        )

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
        return make_error_response(
            "exception",
            f"Failed to upsert singleton text relation: {exc}",
            details={"exception_type": type(exc).__name__},
        )


def _concept_exists(**kwargs):
    from ...db.repositories.concepts_repository import ConceptsRepository
    from ...security.access_control import can_access_concept
    from ...vontology.code_concepts_registry import is_code_concept_id

    concept_id = kwargs.get("concept_id")
    if not concept_id:
        return make_error_response(
            "missing_parameter",
            "Missing 'concept_id' parameter",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID to check existence for"],
        )

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
        return make_error_response(
            "exception",
            f"Failed to check concept existence: {exc}",
            details={"exception_type": type(exc).__name__},
        )


def _fetch_concept_content(**kwargs):
    from ...vontology.utils_vontology import get_vontology_node_content

    concept_id = kwargs.get("concept_id")
    reconstruct_md = kwargs.get("reconstruct_md", True)
    if not concept_id:
        return make_error_response(
            "missing_parameter",
            "Missing 'concept_id' parameter",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID to fetch content for"],
        )

    try:
        payload = get_vontology_node_content(
            concept_id, reconstruct_md=bool(reconstruct_md)
        )
        payload["success"] = "error" not in payload
        return payload
    except Exception as exc:
        return make_error_response(
            "exception",
            f"Failed to fetch concept content: {exc}",
            details={"exception_type": type(exc).__name__},
        )


def _add_names_to_concept(**kwargs):
    from ...services.text_value_service import upsert_text_for_concept
    from ...db.repositories.concepts_repository import ConceptsRepository

    concept_id = kwargs.get("concept_id")
    names = kwargs.get("names")

    if not concept_id:
        return make_error_response(
            "missing_parameter",
            "Missing 'concept_id' parameter",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID to add names to"],
        )
    if not names or not isinstance(names, list) or len(names) == 0:
        return make_error_response(
            "missing_parameter",
            "Missing or invalid 'names' array",
            details={"missing": ["names"]},
            suggestions=[
                "Provide an array of names, e.g., names=['Name1', 'Name2'] or names=[{name: 'Name', language: 'fr'}]"
            ],
        )

    # Verify concept exists
    concept = ConceptsRepository.find_one({"concept_id": concept_id})
    if not concept:
        return make_error_response(
            "concept_not_found",
            f"Concept '{concept_id}' not found",
            details={"concept_id": concept_id},
            suggestions=[
                "Check the concept ID for typos",
                "Create the concept first using create_concepts",
            ],
            related_concept_ids=[concept_id],
        )

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

    source_id = kwargs.get("source_id")
    predicate = kwargs.get("predicate")
    target = kwargs.get("target")

    if not source_id:
        return make_error_response(
            "missing_parameter",
            "Missing 'source_id' parameter",
            details={"missing": ["source_id"]},
            suggestions=[
                "Provide the source concept ID, e.g., source_id='#V#my_concept'"
            ],
        )
    if not predicate:
        return make_error_response(
            "missing_parameter",
            "Missing 'predicate' parameter",
            details={"missing": ["predicate"]},
            suggestions=[
                "Provide a predicate like 'instance_of', 'typeOf', or a concept ID like '#V#hasAffiliation'"
            ],
        )
    if not target:
        return make_error_response(
            "missing_parameter",
            "Missing 'target' parameter",
            details={"missing": ["target"]},
            suggestions=["Provide the target concept ID or text value"],
        )

    if source_id == target:
        return make_error_response(
            "relationship_self_reference",
            "Source and target cannot be the same",
            details={"source_id": source_id, "target": target},
            suggestions=["Use different concept IDs for source and target"],
        )

    try:
        repo = ConceptsRepository

        from ...services.relationship_write_service import (
            add_relationship,
            normalise_structural_predicate,
        )

        # Check if source exists
        src = repo.find_one({"concept_id": source_id})
        if not src:
            return make_error_response(
                "source_concept_not_found",
                f"Source concept '{source_id}' not found",
                details={"role": "source", "concept_id": source_id},
                suggestions=[
                    "Create the source concept first using create_concepts",
                    "Check the concept ID for typos",
                ],
                related_concept_ids=[source_id],
            )

        # Common text predicates are frequently provided either as plain predicate IDs
        # (e.g. 'hasContent') or V-prefixed IDs (e.g. '#V#hasContent'). Treat these
        # as text relations without requiring a predicate concept to exist.
        predicate_str = (
            predicate.strip() if isinstance(predicate, str) else str(predicate)
        )
        predicate_str = normalise_structural_predicate(predicate_str)
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

        # Concept-to-concept relationships use the single authoritative pathway.
        result = add_relationship(
            source_id=source_id,
            predicate=predicate_str,
            target=target,
            repo=repo,
        )

        if not result.get("success"):
            error_code = result.get("error") or "relationship_add_failed"
            return make_error_response(
                str(error_code),
                str(error_code),
                details={
                    "source_id": source_id,
                    "predicate": predicate_str,
                    "target": target,
                    "details": result,
                },
                related_concept_ids=(
                    [source_id, target] if target.startswith("#V#") else [source_id]
                ),
            )

        predicate_out = result.get("predicate") or predicate_str
        target_out = result.get("target_id") or target
        response: dict[str, Any] = {
            "success": True,
            "relationship_type": "concept_relation",
            "source_id": source_id,
            "predicate": predicate_out,
            "predicate_input": predicate,
            "target": target_out,
            "added": bool(
                result.get("forward_modified")
                if "forward_modified" in result
                else result.get("modified")
            ),
        }
        if "inverse_predicate" in result or "inverse_modified" in result:
            response["inverse"] = {
                "predicate": result.get("inverse_predicate"),
                "added": result.get("inverse_modified"),
            }
        # Propagate warning from service layer (e.g. vacuous typing, JVNAUTOSCI-1010)
        if "warning" in result:
            response["warning"] = result["warning"]
        return response

    except Exception as e:
        return make_error_response(
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
    """Remove one concept-to-concept relationship via the canonical service path."""
    from ...services.relationship_removal_service import remove_relationship

    source_id = kwargs.get("source_id")
    predicate = kwargs.get("predicate")
    target = kwargs.get("target")
    relation_id = kwargs.get("relation_id")

    if not relation_id:
        missing: list[str] = []
        if not source_id:
            missing.append("source_id")
        if not predicate:
            missing.append("predicate")
        if not target:
            missing.append("target")
        if missing:
            return make_error_response(
                "missing_parameter",
                "Missing required parameters for remove_relationship",
                details={"missing": missing},
                suggestions=[
                    "Provide relation_id OR provide source_id, predicate, and target",
                ],
            )

    try:
        return remove_relationship(
            relation_id=relation_id,
            source_id=source_id,
            predicate=predicate,
            target=target,
            mode=kwargs.get("mode"),
            cascade=kwargs.get("cascade"),
            dry_run=kwargs.get("dry_run", False),
            confirmed=kwargs.get("confirmed", False),
            operator_override=kwargs.get("operator_override", False),
            reason=kwargs.get("reason"),
            request_id=kwargs.get("request_id"),
        )
    except Exception as e:
        return make_error_response(
            "exception",
            f"Exception: {str(e)}",
            details={
                "source_id": source_id,
                "predicate": predicate,
                "target": target,
                "relation_id": relation_id,
                "exception_type": type(e).__name__,
            },
        )


def _upsert_uncertain_relationship_assertion(**kwargs):
    from ...services.uncertain_relationship_service import (
        upsert_uncertain_relationship_assertion,
    )

    source_id = kwargs.get("source_id")
    predicate = kwargs.get("predicate")
    target = kwargs.get("target")
    confidence_score = kwargs.get("confidence_score")

    missing: list[str] = []
    if not source_id:
        missing.append("source_id")
    if not predicate:
        missing.append("predicate")
    if not target:
        missing.append("target")
    if confidence_score is None:
        missing.append("confidence_score")
    if missing:
        return make_error_response(
            "missing_parameter",
            "Missing required parameters for upsert_uncertain_relationship_assertion",
            details={"missing": missing},
        )

    source_id_text = str(source_id)
    predicate_text = str(predicate)
    target_text = str(target)
    if not isinstance(confidence_score, (int, float, str)):
        return make_error_response(
            "invalid_parameter",
            "confidence_score must be numeric",
            details={"confidence_score": confidence_score},
        )
    confidence_value = float(confidence_score)

    try:
        return upsert_uncertain_relationship_assertion(
            source_id=source_id_text,
            predicate=predicate_text,
            target=target_text,
            confidence_score=confidence_value,
            provenance=kwargs.get("provenance"),
            status=kwargs.get("status", "proposed"),
            assertion_id=kwargs.get("assertion_id"),
            evidence_count=kwargs.get("evidence_count"),
        )
    except Exception as e:
        return make_error_response(
            "exception",
            f"Exception: {str(e)}",
            details={"exception_type": type(e).__name__},
        )


def _list_uncertain_relationship_assertions(**kwargs):
    from ...services.uncertain_relationship_service import (
        list_uncertain_relationship_assertions,
    )

    source_id = kwargs.get("source_id")
    if not source_id:
        return make_error_response(
            "missing_parameter",
            "Missing 'source_id' parameter",
            details={"missing": ["source_id"]},
        )

    try:
        rows = list_uncertain_relationship_assertions(
            source_id=source_id,
            predicate=kwargs.get("predicate"),
            statuses=kwargs.get("statuses"),
            include_legacy=bool(kwargs.get("include_legacy", True)),
        )
        return {
            "success": True,
            "source_id": source_id,
            "count": len(rows),
            "assertions": rows,
        }
    except Exception as e:
        return make_error_response(
            "exception",
            f"Exception: {str(e)}",
            details={"exception_type": type(e).__name__},
        )


def _promote_uncertain_relationship_assertion(**kwargs):
    from ...services.uncertain_relationship_service import (
        promote_uncertain_relationship_assertion,
    )

    source_id = kwargs.get("source_id")
    assertion_id = kwargs.get("assertion_id")
    missing: list[str] = []
    if not source_id:
        missing.append("source_id")
    if not assertion_id:
        missing.append("assertion_id")
    if missing:
        return make_error_response(
            "missing_parameter",
            "Missing required parameters for promote_uncertain_relationship_assertion",
            details={"missing": missing},
        )

    source_id_text = str(source_id)
    assertion_id_text = str(assertion_id)

    try:
        return promote_uncertain_relationship_assertion(
            source_id=source_id_text,
            assertion_id=assertion_id_text,
            operator=str(kwargs.get("operator") or "mcp"),
        )
    except Exception as e:
        return make_error_response(
            "exception",
            f"Exception: {str(e)}",
            details={"exception_type": type(e).__name__},
        )


def _reject_uncertain_relationship_assertion(**kwargs):
    from ...services.uncertain_relationship_service import (
        reject_uncertain_relationship_assertion,
    )

    source_id = kwargs.get("source_id")
    assertion_id = kwargs.get("assertion_id")
    reason = kwargs.get("reason")
    missing: list[str] = []
    if not source_id:
        missing.append("source_id")
    if not assertion_id:
        missing.append("assertion_id")
    if not reason:
        missing.append("reason")
    if missing:
        return make_error_response(
            "missing_parameter",
            "Missing required parameters for reject_uncertain_relationship_assertion",
            details={"missing": missing},
        )

    source_id_text = str(source_id)
    assertion_id_text = str(assertion_id)
    reason_text = str(reason)

    try:
        return reject_uncertain_relationship_assertion(
            source_id=source_id_text,
            assertion_id=assertion_id_text,
            reason=reason_text,
            operator=str(kwargs.get("operator") or "mcp"),
        )
    except Exception as e:
        return make_error_response(
            "exception",
            f"Exception: {str(e)}",
            details={"exception_type": type(e).__name__},
        )


def _migrate_legacy_hypothesized_relations(**kwargs):
    from ...services.uncertain_relationship_service import (
        migrate_legacy_hypothesized_relations,
    )

    source_id = kwargs.get("source_id")
    if not source_id:
        return make_error_response(
            "missing_parameter",
            "Missing 'source_id' parameter",
            details={"missing": ["source_id"]},
        )

    try:
        return migrate_legacy_hypothesized_relations(
            source_id=source_id,
            predicate=kwargs.get("predicate"),
            dry_run=bool(kwargs.get("dry_run", True)),
        )
    except Exception as e:
        return make_error_response(
            "exception",
            f"Exception: {str(e)}",
            details={"exception_type": type(e).__name__},
        )


def _preview_remove_relationship(**kwargs):
    from ...services.relationship_removal_service import preview_remove_relationship

    try:
        return preview_remove_relationship(
            relation_id=kwargs.get("relation_id"),
            source_id=kwargs.get("source_id"),
            predicate=kwargs.get("predicate"),
            target=kwargs.get("target"),
            request_id=kwargs.get("request_id"),
        )
    except Exception as e:
        return make_error_response(
            "exception",
            f"Exception: {str(e)}",
            details={"exception_type": type(e).__name__},
        )


def _remove_relationships_bulk(**kwargs):
    from ...services.relationship_removal_service import remove_relationships_bulk

    try:
        return remove_relationships_bulk(
            relation_ids=kwargs.get("relation_ids"),
            relations=kwargs.get("relations"),
            filter=kwargs.get("filter"),
            mode=kwargs.get("mode"),
            cascade=kwargs.get("cascade"),
            dry_run=kwargs.get("dry_run", False),
            confirmed=kwargs.get("confirmed", False),
            operator_override=kwargs.get("operator_override", False),
            reason=kwargs.get("reason"),
            request_id=kwargs.get("request_id"),
            stop_on_error=kwargs.get("stop_on_error", False),
        )
    except Exception as e:
        return make_error_response(
            "exception",
            f"Exception: {str(e)}",
            details={"exception_type": type(e).__name__},
        )


def _undo_relationship_removal(**kwargs):
    from ...services.relationship_removal_service import undo_relationship_removal

    undo_token = kwargs.get("undo_token")
    if not undo_token:
        return make_error_response(
            "missing_parameter",
            "Missing 'undo_token' parameter",
            details={"missing": ["undo_token"]},
            suggestions=["Provide the undo_token returned by remove_relationship(s)_bulk"],
        )

    try:
        return undo_relationship_removal(
            undo_token=undo_token,
            request_id=kwargs.get("request_id"),
            confirmed=kwargs.get("confirmed", True),
        )
    except Exception as e:
        return make_error_response(
            "exception",
            f"Exception: {str(e)}",
            details={"exception_type": type(e).__name__},
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
            return make_error_response(
                "arxiv_proxy_error",
                str(e),
                details={"exception_type": "ArxivProxyError"},
            )
        except Exception as e:
            return make_error_response(
                "exception",
                f"Unexpected error: {e}",
                details={"exception_type": type(e).__name__},
            )

    return _run_async_compat(_async_search)


def _download_paper(**kwargs):
    import asyncio
    from .arxiv_proxy_mcp import get_arxiv_proxy, ArxivProxyError

    arxiv_id = kwargs.get("arxiv_id")
    if not arxiv_id:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: arxiv_id",
            details={"missing": ["arxiv_id"]},
            suggestions=[
                "Provide the arXiv ID (e.g., '2301.12345' or 'arxiv:2301.12345')"
            ],
        )

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
            return make_error_response(
                "arxiv_proxy_error",
                str(e),
                details={"arxiv_id": arxiv_id, "exception_type": "ArxivProxyError"},
            )
        except Exception as e:
            return make_error_response(
                "exception",
                f"Unexpected error: {e}",
                details={"arxiv_id": arxiv_id, "exception_type": type(e).__name__},
            )

    return _run_async_compat(_async_download)


def _finalise_cached_paper(**kwargs):
    """Upload an already-cached arXiv PDF to the blob store and register a Computer File Copy."""

    arxiv_id = kwargs.get("arxiv_id")
    if not arxiv_id:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: arxiv_id",
            details={"missing": ["arxiv_id"]},
            suggestions=[
                "Provide the arXiv ID (e.g., '2301.12345' or 'arxiv:2301.12345')"
            ],
        )

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
        return make_error_response(
            "blob_upload_failed",
            f"Blob store upload failed: {exc}",
            details={"exception_type": "BlobUploadError", "arxiv_id": arxiv_id},
        )
    except Exception as exc:
        return make_error_response(
            "exception",
            f"Unexpected error: {exc}",
            details={"exception_type": type(exc).__name__, "arxiv_id": arxiv_id},
        )


def _list_papers(**kwargs):
    import asyncio
    from .arxiv_proxy_mcp import get_arxiv_proxy, ArxivProxyError

    async def _async_list():
        try:
            proxy = await get_arxiv_proxy()
            return await proxy.list_papers()
        except ArxivProxyError as e:
            return make_error_response(
                "arxiv_proxy_error",
                str(e),
                details={"exception_type": "ArxivProxyError"},
            )
        except Exception as e:
            return make_error_response(
                "exception",
                f"Unexpected error: {e}",
                details={"exception_type": type(e).__name__},
            )

    return _run_async_compat(_async_list)


def _read_paper(**kwargs):
    import asyncio
    from .arxiv_proxy_mcp import get_arxiv_proxy, ArxivProxyError

    arxiv_id = kwargs.get("arxiv_id")
    if not arxiv_id:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: arxiv_id",
            details={"missing": ["arxiv_id"]},
            suggestions=[
                "Provide the arXiv ID (e.g., '2301.12345' or 'arxiv:2301.12345')"
            ],
        )

    async def _async_read():
        try:
            proxy = await get_arxiv_proxy()
            return await proxy.read_paper(arxiv_id=arxiv_id)
        except ArxivProxyError as e:
            return make_error_response(
                "arxiv_proxy_error",
                str(e),
                details={"arxiv_id": arxiv_id, "exception_type": "ArxivProxyError"},
            )
        except Exception as e:
            return make_error_response(
                "exception",
                f"Unexpected error: {e}",
                details={"arxiv_id": arxiv_id, "exception_type": type(e).__name__},
            )

    return _run_async_compat(_async_read)


def _normalise_namespace_override(namespace: Any) -> str | None:
    if not isinstance(namespace, str):
        return None
    cleaned = namespace.strip()
    return cleaned or None


def _namespace_actor_overrides_from_namespace(
    namespace: str | None,
) -> tuple[str | None, str | None]:
    """Resolve optional user/org auth overrides from namespace payload."""

    namespace_clean = _normalise_namespace_override(namespace)
    if namespace_clean is None:
        return None, None

    user_part: str | None
    org_part: str | None
    if "@" in namespace_clean:
        user_raw, org_raw = namespace_clean.split("@", 1)
        user_part = user_raw.strip() or None
        org_part = org_raw.strip() or None
    elif "/" in namespace_clean:
        user_raw, org_raw = namespace_clean.split("/", 1)
        user_part = user_raw.strip() or None
        org_part = org_raw.strip() or None
    else:
        user_part = namespace_clean
        org_part = None

    if org_part and not org_part.startswith("#V#"):
        org_part = f"#V#{org_part}"
    return user_part, org_part


@contextmanager
def _with_namespace_actor_override(namespace: str | None):
    """Apply best-effort auth context override for namespace-driven tool calls."""

    from ...security.access_control import (
        override_current_organisation,
        override_current_user,
    )

    user_part, org_part = _namespace_actor_overrides_from_namespace(namespace)
    if user_part or org_part:
        with override_current_user(user_part), override_current_organisation(org_part):
            yield
        return
    yield


# Blob/file-copy retrieval
def _read_file_copy(**kwargs):
    import os
    from ...security.access_control import get_effective_user_concept_id
    from ...services.computer_file_copy_service import fetch_file_copy_bytes

    concept_id = kwargs.get("concept_id") or kwargs.get("file_copy_concept_id")
    if not isinstance(concept_id, str) or not concept_id.strip():
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: concept_id",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID of the file copy to read"],
        )

    max_bytes = kwargs.get("max_bytes")
    if max_bytes is None:
        max_bytes = 5_000_000
    else:
        try:
            max_bytes = int(max_bytes)
        except (TypeError, ValueError):
            return make_error_response(
                "invalid_parameter",
                "Invalid max_bytes: must be an integer",
                details={"max_bytes": max_bytes},
                suggestions=["Provide max_bytes as an integer (e.g., 5000000)"],
            )
        if max_bytes <= 0:
            return make_error_response(
                "invalid_parameter",
                "max_bytes must be positive",
                details={"max_bytes": max_bytes},
                suggestions=["Provide a positive integer for max_bytes"],
            )

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

    namespace = _normalise_namespace_override(kwargs.get("namespace"))

    def _run_read():
        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return make_error_response(
                "authentication_required",
                "User authentication required to read file copies",
                suggestions=["Ensure user context is set before calling this tool"],
            )

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
                        page_text: str = str(page.get_text("text"))
                        extracted_pages.append(page_text)
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
                        return make_error_response(
                            "invalid_encoding",
                            f"Invalid encoding: {encoding}",
                            details={"encoding": encoding},
                            suggestions=[
                                "Use a valid encoding like 'utf-8', 'latin-1', or 'ascii'"
                            ],
                        )
                    payload["text"] = text
                    payload["encoding"] = str(encoding)
            else:
                try:
                    content_type = getattr(info, "content_type", None)
                    original_name = getattr(info, "original_filename", None)

                    # Detect Office document types
                    is_docx = False
                    is_pptx = False
                    is_xlsx = False
                    is_odt = False
                    is_ods = False
                    is_odp = False
                    is_rtf = False
                    is_eml = False
                    is_msg = False
                    is_html = False
                    is_markdown = False
                    is_csv = False
                    is_latex = False
                    is_image = False

                    ct_lower = (
                        content_type.lower() if isinstance(content_type, str) else ""
                    )
                    name_lower = (
                        original_name.lower() if isinstance(original_name, str) else ""
                    )

                    # DOCX detection
                    if (
                        "application/vnd.openxmlformats-officedocument.wordprocessingml"
                        in ct_lower
                    ):
                        is_docx = True
                    elif name_lower.endswith(".docx"):
                        is_docx = True

                    # PPTX detection
                    if (
                        "application/vnd.openxmlformats-officedocument.presentationml"
                        in ct_lower
                    ):
                        is_pptx = True
                    elif name_lower.endswith(".pptx"):
                        is_pptx = True

                    # XLSX detection
                    if (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml"
                        in ct_lower
                    ):
                        is_xlsx = True
                    elif name_lower.endswith(".xlsx"):
                        is_xlsx = True

                    # ODT detection (OpenDocument Text)
                    if "application/vnd.oasis.opendocument.text" in ct_lower:
                        is_odt = True
                    elif name_lower.endswith(".odt"):
                        is_odt = True

                    # ODS detection (OpenDocument Spreadsheet)
                    if "application/vnd.oasis.opendocument.spreadsheet" in ct_lower:
                        is_ods = True
                    elif name_lower.endswith(".ods"):
                        is_ods = True

                    # ODP detection (OpenDocument Presentation)
                    if "application/vnd.oasis.opendocument.presentation" in ct_lower:
                        is_odp = True
                    elif name_lower.endswith(".odp"):
                        is_odp = True

                    # RTF detection
                    if ct_lower in ("application/rtf", "text/rtf"):
                        is_rtf = True
                    elif name_lower.endswith(".rtf"):
                        is_rtf = True

                    # EML detection (email)
                    if ct_lower == "message/rfc822":
                        is_eml = True
                    elif name_lower.endswith(".eml"):
                        is_eml = True

                    # MSG detection (Outlook email)
                    if ct_lower == "application/vnd.ms-outlook":
                        is_msg = True
                    elif name_lower.endswith(".msg"):
                        is_msg = True

                    # HTML detection
                    if ct_lower in ("text/html", "application/xhtml+xml"):
                        is_html = True
                    elif name_lower.endswith((".html", ".htm", ".xhtml")):
                        is_html = True

                    # Markdown detection
                    if ct_lower in ("text/markdown", "text/x-markdown"):
                        is_markdown = True
                    elif name_lower.endswith((".md", ".markdown")):
                        is_markdown = True

                    # CSV/TSV detection
                    if ct_lower in ("text/csv", "text/tab-separated-values"):
                        is_csv = True
                    elif name_lower.endswith((".csv", ".tsv")):
                        is_csv = True

                    # LaTeX detection
                    if ct_lower in ("application/x-latex", "application/x-tex"):
                        is_latex = True
                    elif name_lower.endswith((".tex", ".latex")):
                        is_latex = True

                    # Image detection
                    if ct_lower.startswith("image/"):
                        is_image = True
                    elif name_lower.endswith(
                        (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif")
                    ):
                        is_image = True

                    # DOCX extraction
                    if is_docx:
                        text = None
                        extraction_method = None
                        extraction_error = None
                        try:
                            import io
                            from docx import Document  # type: ignore[import-not-found]

                            doc = Document(io.BytesIO(bytes(data_bytes)))
                            paragraphs: list[str] = []
                            for para in doc.paragraphs:
                                if para.text.strip():
                                    paragraphs.append(para.text)
                            # Also extract text from tables
                            for table in doc.tables:
                                for row in table.rows:
                                    row_text = "\t".join(
                                        cell.text.strip() for cell in row.cells
                                    )
                                    if row_text.strip():
                                        paragraphs.append(row_text)
                            text = "\n".join(paragraphs).strip()
                            extraction_method = "python_docx"
                        except ModuleNotFoundError as exc:
                            extraction_method = "python_docx_missing"
                            extraction_error = str(exc)
                        except Exception as exc:
                            extraction_method = "python_docx_failed"
                            extraction_error = str(exc)

                        if text:
                            payload["text"] = text
                            payload["encoding"] = "utf-8"
                            payload["text_extraction"] = extraction_method
                        else:
                            if extraction_method:
                                payload["text_extraction"] = extraction_method
                            if extraction_error:
                                payload["text_extraction_error"] = extraction_error
                            text = bytes(data_bytes).decode(
                                str(encoding), errors="replace"
                            )
                            payload["text"] = text
                            payload["encoding"] = str(encoding)

                    # PPTX extraction
                    elif is_pptx:
                        text = None
                        extraction_method = None
                        extraction_error = None
                        try:
                            import io
                            from pptx import Presentation  # type: ignore[import-not-found]

                            prs = Presentation(io.BytesIO(bytes(data_bytes)))
                            slides_text: list[str] = []
                            for slide_num, slide in enumerate(prs.slides, 1):
                                slide_parts: list[str] = [f"--- Slide {slide_num} ---"]
                                for shape in slide.shapes:
                                    shape_text = getattr(shape, "text", None)  # type: ignore[attr-defined]
                                    if shape_text and shape_text.strip():
                                        slide_parts.append(shape_text)
                                    # Extract text from tables in slides
                                    shape_table = getattr(shape, "table", None)  # type: ignore[attr-defined]
                                    if shape_table is not None:
                                        for row in shape_table.rows:
                                            row_text = "\t".join(
                                                cell.text.strip() for cell in row.cells
                                            )
                                            if row_text.strip():
                                                slide_parts.append(row_text)
                                # Extract speaker notes
                                if (
                                    slide.has_notes_slide
                                    and slide.notes_slide.notes_text_frame
                                ):
                                    notes = (
                                        slide.notes_slide.notes_text_frame.text.strip()
                                    )
                                    if notes:
                                        slide_parts.append(f"[Speaker Notes: {notes}]")
                                slides_text.append("\n".join(slide_parts))
                            text = "\n\n".join(slides_text).strip()
                            extraction_method = "python_pptx"
                        except ModuleNotFoundError as exc:
                            extraction_method = "python_pptx_missing"
                            extraction_error = str(exc)
                        except Exception as exc:
                            extraction_method = "python_pptx_failed"
                            extraction_error = str(exc)

                        if text:
                            payload["text"] = text
                            payload["encoding"] = "utf-8"
                            payload["text_extraction"] = extraction_method
                        else:
                            if extraction_method:
                                payload["text_extraction"] = extraction_method
                            if extraction_error:
                                payload["text_extraction_error"] = extraction_error
                            text = bytes(data_bytes).decode(
                                str(encoding), errors="replace"
                            )
                            payload["text"] = text
                            payload["encoding"] = str(encoding)

                    # XLSX extraction
                    elif is_xlsx:
                        text = None
                        extraction_method = None
                        extraction_error = None
                        try:
                            import io
                            from openpyxl import load_workbook  # type: ignore[import-not-found]

                            wb = load_workbook(
                                io.BytesIO(bytes(data_bytes)),
                                read_only=True,
                                data_only=True,
                            )
                            sheets_text: list[str] = []
                            for sheet_name in wb.sheetnames:
                                sheet = wb[sheet_name]
                                sheet_parts: list[str] = [
                                    f"--- Sheet: {sheet_name} ---"
                                ]
                                for row in sheet.iter_rows(values_only=True):
                                    row_values = [
                                        str(cell) if cell is not None else ""
                                        for cell in row
                                    ]
                                    if any(v.strip() for v in row_values):
                                        sheet_parts.append("\t".join(row_values))
                                if len(sheet_parts) > 1:  # Has data beyond header
                                    sheets_text.append("\n".join(sheet_parts))
                            wb.close()
                            text = "\n\n".join(sheets_text).strip()
                            extraction_method = "openpyxl"
                        except ModuleNotFoundError as exc:
                            extraction_method = "openpyxl_missing"
                            extraction_error = str(exc)
                        except Exception as exc:
                            extraction_method = "openpyxl_failed"
                            extraction_error = str(exc)

                        if text:
                            payload["text"] = text
                            payload["encoding"] = "utf-8"
                            payload["text_extraction"] = extraction_method
                        else:
                            if extraction_method:
                                payload["text_extraction"] = extraction_method
                            if extraction_error:
                                payload["text_extraction_error"] = extraction_error
                            text = bytes(data_bytes).decode(
                                str(encoding), errors="replace"
                            )
                            payload["text"] = text
                            payload["encoding"] = str(encoding)

                    # ODT extraction (OpenDocument Text)
                    elif is_odt:
                        text = None
                        extraction_method = None
                        extraction_error = None
                        try:
                            import io
                            from odf import text as odf_text  # type: ignore[import-not-found]
                            from odf.opendocument import load as odf_load  # type: ignore[import-not-found]

                            doc = odf_load(io.BytesIO(bytes(data_bytes)))
                            paragraphs: list[str] = []
                            for para in doc.getElementsByType(odf_text.P):
                                p_text = "".join(
                                    node.data
                                    for node in para.childNodes
                                    if hasattr(node, "data")
                                )
                                if p_text.strip():
                                    paragraphs.append(p_text)
                            text = "\n".join(paragraphs).strip()
                            extraction_method = "odfpy"
                        except ModuleNotFoundError as exc:
                            extraction_method = "odfpy_missing"
                            extraction_error = str(exc)
                        except Exception as exc:
                            extraction_method = "odfpy_failed"
                            extraction_error = str(exc)

                        if text:
                            payload["text"] = text
                            payload["encoding"] = "utf-8"
                            payload["text_extraction"] = extraction_method
                        else:
                            if extraction_method:
                                payload["text_extraction"] = extraction_method
                            if extraction_error:
                                payload["text_extraction_error"] = extraction_error
                            text = bytes(data_bytes).decode(
                                str(encoding), errors="replace"
                            )
                            payload["text"] = text
                            payload["encoding"] = str(encoding)

                    # ODS extraction (OpenDocument Spreadsheet)
                    elif is_ods:
                        text = None
                        extraction_method = None
                        extraction_error = None
                        try:
                            import io
                            from odf.opendocument import load as odf_load  # type: ignore[import-not-found]
                            from odf import table as odf_table  # type: ignore[import-not-found]

                            doc = odf_load(io.BytesIO(bytes(data_bytes)))
                            sheets_text: list[str] = []
                            for sheet in doc.getElementsByType(odf_table.Table):
                                sheet_name = sheet.getAttribute("name") or "Sheet"
                                sheet_parts: list[str] = [
                                    f"--- Sheet: {sheet_name} ---"
                                ]
                                for row in sheet.getElementsByType(odf_table.TableRow):
                                    row_values: list[str] = []
                                    for cell in row.getElementsByType(
                                        odf_table.TableCell
                                    ):
                                        cell_text = "".join(
                                            node.data
                                            for p in cell.childNodes
                                            if hasattr(p, "childNodes")
                                            for node in p.childNodes
                                            if hasattr(node, "data")
                                        )
                                        row_values.append(cell_text)
                                    if any(v.strip() for v in row_values):
                                        sheet_parts.append("\t".join(row_values))
                                if len(sheet_parts) > 1:
                                    sheets_text.append("\n".join(sheet_parts))
                            text = "\n\n".join(sheets_text).strip()
                            extraction_method = "odfpy"
                        except ModuleNotFoundError as exc:
                            extraction_method = "odfpy_missing"
                            extraction_error = str(exc)
                        except Exception as exc:
                            extraction_method = "odfpy_failed"
                            extraction_error = str(exc)

                        if text:
                            payload["text"] = text
                            payload["encoding"] = "utf-8"
                            payload["text_extraction"] = extraction_method
                        else:
                            if extraction_method:
                                payload["text_extraction"] = extraction_method
                            if extraction_error:
                                payload["text_extraction_error"] = extraction_error
                            text = bytes(data_bytes).decode(
                                str(encoding), errors="replace"
                            )
                            payload["text"] = text
                            payload["encoding"] = str(encoding)

                    # ODP extraction (OpenDocument Presentation)
                    elif is_odp:
                        text = None
                        extraction_method = None
                        extraction_error = None
                        try:
                            import io
                            from odf.opendocument import load as odf_load  # type: ignore[import-not-found]
                            from odf import draw as odf_draw  # type: ignore[import-not-found]
                            from odf import text as odf_text  # type: ignore[import-not-found]

                            doc = odf_load(io.BytesIO(bytes(data_bytes)))
                            slides_text: list[str] = []
                            for slide_num, page in enumerate(
                                doc.getElementsByType(odf_draw.Page), 1
                            ):
                                slide_parts: list[str] = [f"--- Slide {slide_num} ---"]
                                for frame in page.getElementsByType(odf_draw.Frame):
                                    for textbox in frame.getElementsByType(
                                        odf_draw.TextBox
                                    ):
                                        for para in textbox.getElementsByType(
                                            odf_text.P
                                        ):
                                            p_text = "".join(
                                                node.data
                                                for node in para.childNodes
                                                if hasattr(node, "data")
                                            )
                                            if p_text.strip():
                                                slide_parts.append(p_text)
                                slides_text.append("\n".join(slide_parts))
                            text = "\n\n".join(slides_text).strip()
                            extraction_method = "odfpy"
                        except ModuleNotFoundError as exc:
                            extraction_method = "odfpy_missing"
                            extraction_error = str(exc)
                        except Exception as exc:
                            extraction_method = "odfpy_failed"
                            extraction_error = str(exc)

                        if text:
                            payload["text"] = text
                            payload["encoding"] = "utf-8"
                            payload["text_extraction"] = extraction_method
                        else:
                            if extraction_method:
                                payload["text_extraction"] = extraction_method
                            if extraction_error:
                                payload["text_extraction_error"] = extraction_error
                            text = bytes(data_bytes).decode(
                                str(encoding), errors="replace"
                            )
                            payload["text"] = text
                            payload["encoding"] = str(encoding)

                    # RTF extraction
                    elif is_rtf:
                        text = None
                        extraction_method = None
                        extraction_error = None
                        try:
                            from striprtf.striprtf import rtf_to_text  # type: ignore[import-not-found]

                            rtf_content = bytes(data_bytes).decode(
                                "utf-8", errors="replace"
                            )
                            text = rtf_to_text(rtf_content).strip()
                            extraction_method = "striprtf"
                        except ModuleNotFoundError as exc:
                            extraction_method = "striprtf_missing"
                            extraction_error = str(exc)
                        except Exception as exc:
                            extraction_method = "striprtf_failed"
                            extraction_error = str(exc)

                        if text:
                            payload["text"] = text
                            payload["encoding"] = "utf-8"
                            payload["text_extraction"] = extraction_method
                        else:
                            if extraction_method:
                                payload["text_extraction"] = extraction_method
                            if extraction_error:
                                payload["text_extraction_error"] = extraction_error
                            text = bytes(data_bytes).decode(
                                str(encoding), errors="replace"
                            )
                            payload["text"] = text
                            payload["encoding"] = str(encoding)

                    # EML extraction (email)
                    elif is_eml:
                        text = None
                        extraction_method = None
                        extraction_error = None
                        try:
                            import email
                            from email.policy import default as email_policy

                            msg = email.message_from_bytes(
                                bytes(data_bytes), policy=email_policy
                            )
                            parts: list[str] = []
                            # Headers
                            for header in ("From", "To", "Subject", "Date"):
                                val = msg.get(header)
                                if val:
                                    parts.append(f"{header}: {val}")
                            parts.append("")  # Blank line after headers
                            # Body
                            if msg.is_multipart():
                                for part in msg.walk():
                                    ctype = part.get_content_type()
                                    if ctype == "text/plain":
                                        body = part.get_content()
                                        if isinstance(body, str):
                                            parts.append(body)
                            else:
                                body = msg.get_content()
                                if isinstance(body, str):
                                    parts.append(body)
                            text = "\n".join(parts).strip()
                            extraction_method = "email_stdlib"
                        except Exception as exc:
                            extraction_method = "email_stdlib_failed"
                            extraction_error = str(exc)

                        if text:
                            payload["text"] = text
                            payload["encoding"] = "utf-8"
                            payload["text_extraction"] = extraction_method
                        else:
                            if extraction_method:
                                payload["text_extraction"] = extraction_method
                            if extraction_error:
                                payload["text_extraction_error"] = extraction_error
                            text = bytes(data_bytes).decode(
                                str(encoding), errors="replace"
                            )
                            payload["text"] = text
                            payload["encoding"] = str(encoding)

                    # MSG extraction (Outlook email)
                    elif is_msg:
                        text = None
                        extraction_method = None
                        extraction_error = None
                        try:
                            import io
                            import extract_msg  # type: ignore[import-not-found]

                            msg = extract_msg.Message(io.BytesIO(bytes(data_bytes)))
                            parts: list[str] = []
                            if msg.sender:
                                parts.append(f"From: {msg.sender}")
                            if msg.to:
                                parts.append(f"To: {msg.to}")
                            if msg.subject:
                                parts.append(f"Subject: {msg.subject}")
                            if msg.date:
                                parts.append(f"Date: {msg.date}")
                            parts.append("")
                            if msg.body:
                                parts.append(msg.body)
                            text = "\n".join(parts).strip()
                            extraction_method = "extract_msg"
                            msg.close()
                        except ModuleNotFoundError as exc:
                            extraction_method = "extract_msg_missing"
                            extraction_error = str(exc)
                        except Exception as exc:
                            extraction_method = "extract_msg_failed"
                            extraction_error = str(exc)

                        if text:
                            payload["text"] = text
                            payload["encoding"] = "utf-8"
                            payload["text_extraction"] = extraction_method
                        else:
                            if extraction_method:
                                payload["text_extraction"] = extraction_method
                            if extraction_error:
                                payload["text_extraction_error"] = extraction_error
                            text = bytes(data_bytes).decode(
                                str(encoding), errors="replace"
                            )
                            payload["text"] = text
                            payload["encoding"] = str(encoding)

                    # HTML extraction
                    elif is_html:
                        text = None
                        extraction_method = None
                        extraction_error = None
                        try:
                            from bs4 import BeautifulSoup  # type: ignore[import-not-found]

                            html_content = bytes(data_bytes).decode(
                                "utf-8", errors="replace"
                            )
                            soup = BeautifulSoup(html_content, "html.parser")
                            # Remove script and style elements
                            for script in soup(["script", "style"]):
                                script.decompose()
                            text = soup.get_text(separator="\n", strip=True)
                            extraction_method = "beautifulsoup"
                        except ModuleNotFoundError as exc:
                            extraction_method = "beautifulsoup_missing"
                            extraction_error = str(exc)
                        except Exception as exc:
                            extraction_method = "beautifulsoup_failed"
                            extraction_error = str(exc)

                        if text:
                            payload["text"] = text
                            payload["encoding"] = "utf-8"
                            payload["text_extraction"] = extraction_method
                        else:
                            if extraction_method:
                                payload["text_extraction"] = extraction_method
                            if extraction_error:
                                payload["text_extraction_error"] = extraction_error
                            text = bytes(data_bytes).decode(
                                str(encoding), errors="replace"
                            )
                            payload["text"] = text
                            payload["encoding"] = str(encoding)

                    # Markdown extraction (pass through as text)
                    elif is_markdown:
                        text = bytes(data_bytes).decode("utf-8", errors="replace")
                        payload["text"] = text
                        payload["encoding"] = "utf-8"
                        payload["text_extraction"] = "markdown_passthrough"

                    # CSV/TSV extraction
                    elif is_csv:
                        text = None
                        extraction_method = None
                        extraction_error = None
                        try:
                            import csv
                            import io

                            csv_content = bytes(data_bytes).decode(
                                "utf-8", errors="replace"
                            )
                            # Detect delimiter
                            dialect = csv.Sniffer().sniff(csv_content[:4096])
                            reader = csv.reader(io.StringIO(csv_content), dialect)
                            rows: list[str] = []
                            for row in reader:
                                rows.append("\t".join(row))
                            text = "\n".join(rows).strip()
                            extraction_method = "csv_stdlib"
                        except Exception as exc:
                            extraction_method = "csv_stdlib_failed"
                            extraction_error = str(exc)

                        if text:
                            payload["text"] = text
                            payload["encoding"] = "utf-8"
                            payload["text_extraction"] = extraction_method
                        else:
                            if extraction_method:
                                payload["text_extraction"] = extraction_method
                            if extraction_error:
                                payload["text_extraction_error"] = extraction_error
                            text = bytes(data_bytes).decode(
                                str(encoding), errors="replace"
                            )
                            payload["text"] = text
                            payload["encoding"] = str(encoding)

                    # LaTeX extraction (pass through as text)
                    elif is_latex:
                        text = bytes(data_bytes).decode("utf-8", errors="replace")
                        payload["text"] = text
                        payload["encoding"] = "utf-8"
                        payload["text_extraction"] = "latex_passthrough"

                    # Image OCR extraction
                    elif is_image:
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

                    # Fallback: generic text decoding
                    else:
                        text = bytes(data_bytes).decode(str(encoding), errors="replace")
                        payload["text"] = text
                        payload["encoding"] = str(encoding)
                except LookupError:
                    return make_error_response(
                        "invalid_encoding",
                        f"Invalid encoding: {encoding}",
                        details={"encoding": encoding},
                        suggestions=[
                            "Use a valid encoding like 'utf-8', 'latin-1', or 'ascii'"
                        ],
                    )
        else:
            import base64

            payload["bytes_base64"] = base64.b64encode(bytes(data_bytes)).decode(
                "ascii"
            )
            payload["bytes_base64_encoding"] = "base64"

        return payload

    with _with_namespace_actor_override(namespace):
        return _run_read()


def _interpret_file_copy(**kwargs):
    import json

    from ...services.arxiv_paper_link_service import (
        extract_arxiv_id_candidates,
        materialise_scholarly_representation_for_arxiv_file_copy,
    )
    from ...services.computer_file_copy_service import fetch_file_copy_bytes
    from ...services.file_copy_interpretation_service import (
        build_document_interpretation,
        build_image_interpretation,
        extract_pdf_diagram_organisation_candidates,
        infer_uploaded_file_subtype,
        is_image_file,
        is_pdf_file,
    )
    from ...services.rag_text_relation_change_hook_service import (
        maybe_sync_concept_text_relations_to_rag,
    )
    from ...services.relationship_write_service import add_relationship
    from ...services.text_value_service import upsert_singleton_text_relation

    concept_id = kwargs.get("concept_id") or kwargs.get("file_copy_concept_id")
    if not isinstance(concept_id, str) or not concept_id.strip():
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: concept_id",
            details={"missing": ["concept_id"]},
            suggestions=[
                "Provide concept_id for a blob-backed #V#computer_file_copy instance"
            ],
        )
    concept_id = concept_id.strip()

    max_bytes = kwargs.get("max_bytes")
    if max_bytes is None:
        max_bytes = 10_000_000
    else:
        try:
            max_bytes = int(max_bytes)
        except (TypeError, ValueError):
            return make_error_response(
                "invalid_parameter",
                "Invalid max_bytes: must be an integer",
                details={"max_bytes": max_bytes},
            )
        if max_bytes <= 0:
            return make_error_response(
                "invalid_parameter",
                "max_bytes must be positive",
                details={"max_bytes": max_bytes},
            )

    max_persist_content_chars_raw = kwargs.get("max_persist_content_chars")
    if max_persist_content_chars_raw is None:
        max_persist_content_chars = 20_000
    else:
        try:
            max_persist_content_chars = int(max_persist_content_chars_raw)
        except (TypeError, ValueError):
            return make_error_response(
                "invalid_parameter",
                "Invalid max_persist_content_chars: must be an integer",
                details={"max_persist_content_chars": max_persist_content_chars_raw},
            )
        if max_persist_content_chars < 0:
            return make_error_response(
                "invalid_parameter",
                "max_persist_content_chars must be zero or positive",
                details={"max_persist_content_chars": max_persist_content_chars},
            )

    allow_large = bool(kwargs.get("allow_large", False))
    persist = bool(kwargs.get("persist", True))
    persist_description = bool(kwargs.get("persist_description", True))
    persist_content = bool(kwargs.get("persist_content", True))
    persist_interpretation_json = bool(kwargs.get("persist_interpretation_json", True))
    include_semantic_description = bool(kwargs.get("include_semantic_description", True))
    include_pdf_diagram_analysis = bool(kwargs.get("include_pdf_diagram_analysis", True))
    include_text = bool(kwargs.get("include_text", False))
    max_diagram_pages_raw = kwargs.get("max_diagram_pages")
    if max_diagram_pages_raw is None:
        max_diagram_pages = 8
    else:
        try:
            max_diagram_pages = int(max_diagram_pages_raw)
        except (TypeError, ValueError):
            return make_error_response(
                "invalid_parameter",
                "Invalid max_diagram_pages: must be an integer",
                details={"max_diagram_pages": max_diagram_pages_raw},
            )
        if max_diagram_pages <= 0:
            return make_error_response(
                "invalid_parameter",
                "max_diagram_pages must be positive",
                details={"max_diagram_pages": max_diagram_pages},
            )

    max_diagram_candidates_raw = kwargs.get("max_diagram_candidates")
    if max_diagram_candidates_raw is None:
        max_diagram_candidates = 40
    else:
        try:
            max_diagram_candidates = int(max_diagram_candidates_raw)
        except (TypeError, ValueError):
            return make_error_response(
                "invalid_parameter",
                "Invalid max_diagram_candidates: must be an integer",
                details={"max_diagram_candidates": max_diagram_candidates_raw},
            )
        if max_diagram_candidates <= 0:
            return make_error_response(
                "invalid_parameter",
                "max_diagram_candidates must be positive",
                details={"max_diagram_candidates": max_diagram_candidates},
            )
    model_override = (
        kwargs.get("vision_model")
        if isinstance(kwargs.get("vision_model"), str)
        else None
    )
    prompt_override = (
        kwargs.get("semantic_prompt")
        if isinstance(kwargs.get("semantic_prompt"), str)
        else None
    )

    namespace = _normalise_namespace_override(kwargs.get("namespace"))
    ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
    if bool(ns_report.get("namespace_mismatch")):
        return {
            "success": False,
            "error": "namespace_mismatch",
            "message": (
                "Explicit namespace conflicts with derived user/org namespace; "
                "interpretation was not executed."
            ),
            **ns_report,
        }
    effective_namespace = namespace
    if effective_namespace is None:
        derived_ns = ns_report.get("namespace")
        if isinstance(derived_ns, str) and derived_ns.strip():
            effective_namespace = derived_ns.strip()

    read_payload = _read_file_copy(
        concept_id=concept_id,
        max_bytes=max_bytes,
        allow_large=allow_large,
        as_text=True,
        namespace=effective_namespace,
    )
    if not isinstance(read_payload, dict):
        return make_error_response(
            "read_file_copy_failed",
            "Unexpected read_file_copy response shape",
            details={"response_type": type(read_payload).__name__},
        )
    if read_payload.get("success") is not True:
        return {
            "success": False,
            "error": read_payload.get("error") or read_payload.get("error_code"),
            "message": read_payload.get("message")
            or "Failed to read file-copy bytes for interpretation",
            "concept_id": concept_id,
            "namespace": effective_namespace,
            **ns_report,
            "read_result": read_payload,
        }

    content_type = read_payload.get("content_type")
    original_filename = read_payload.get("original_filename")
    extracted_text_raw = read_payload.get("text")
    extracted_text = (
        extracted_text_raw if isinstance(extracted_text_raw, str) else None
    )
    file_kind = (
        "image"
        if is_image_file(
            content_type=content_type if isinstance(content_type, str) else None,
            filename=(
                original_filename if isinstance(original_filename, str) else None
            ),
        )
        else "document"
    )
    subtype_detection = infer_uploaded_file_subtype(
        content_type=content_type if isinstance(content_type, str) else None,
        original_filename=(
            original_filename if isinstance(original_filename, str) else None
        ),
    )
    subtype_type_concept_id = (
        subtype_detection.get("type_concept_id")
        if isinstance(subtype_detection, dict)
        else None
    )

    interpretation: dict[str, Any]
    diagram_analysis: dict[str, Any] | None = None
    image_fetch_error: str | None = None
    if file_kind == "image":
        with _with_namespace_actor_override(effective_namespace):
            image_fetch = fetch_file_copy_bytes(
                file_copy_concept_id=concept_id,
                max_bytes=max_bytes,
                allow_large=allow_large,
            )
        if not isinstance(image_fetch, dict) or image_fetch.get("success") is not True:
            image_fetch_error = str(
                image_fetch.get("error") if isinstance(image_fetch, dict) else "unknown"
            )
            interpretation = {
                "kind": "image",
                "description": (
                    "Image uploaded but byte-level visual interpretation could not be completed."
                ),
                "content_text": extracted_text,
                "content_length": len(extracted_text) if extracted_text else 0,
                "subject_tags": ["image"],
                "image_fetch_error": image_fetch_error,
            }
        else:
            image_bytes = image_fetch.get("data") or b""
            interpretation = build_image_interpretation(
                data_bytes=bytes(image_bytes),
                content_type=content_type if isinstance(content_type, str) else None,
                original_filename=(
                    original_filename if isinstance(original_filename, str) else None
                ),
                include_semantic_description=include_semantic_description,
                model_override=model_override,
                prompt_override=prompt_override,
            )
            interpreted_content = interpretation.get("content_text")
            if (
                isinstance(interpreted_content, str)
                and interpreted_content.strip()
                and (
                    not isinstance(extracted_text, str)
                    or len(interpreted_content.strip()) > len(extracted_text.strip())
                )
            ):
                extracted_text = interpreted_content.strip()
    else:
        interpretation = build_document_interpretation(
            extracted_text=extracted_text,
            content_type=content_type if isinstance(content_type, str) else None,
            original_filename=(
                original_filename if isinstance(original_filename, str) else None
            ),
        )
        if include_pdf_diagram_analysis and is_pdf_file(
            content_type=content_type if isinstance(content_type, str) else None,
            filename=original_filename if isinstance(original_filename, str) else None,
        ):
            with _with_namespace_actor_override(effective_namespace):
                pdf_fetch = fetch_file_copy_bytes(
                    file_copy_concept_id=concept_id,
                    max_bytes=max_bytes,
                    allow_large=allow_large,
                )
            if isinstance(pdf_fetch, dict) and pdf_fetch.get("success") is True:
                pdf_bytes = pdf_fetch.get("data") or b""
                diagram_analysis = extract_pdf_diagram_organisation_candidates(
                    data_bytes=bytes(pdf_bytes),
                    content_type=(
                        content_type if isinstance(content_type, str) else None
                    ),
                    original_filename=(
                        original_filename
                        if isinstance(original_filename, str)
                        else None
                    ),
                    prose_text=extracted_text,
                    max_pages=max_diagram_pages,
                    max_candidates=max_diagram_candidates,
                )
            else:
                diagram_analysis = {
                    "available": False,
                    "reason": "pdf_byte_fetch_failed",
                    "errors": [
                        str(
                            pdf_fetch.get("error")
                            if isinstance(pdf_fetch, dict)
                            else "unknown"
                        )
                    ],
                    "requires_human_confirmation": True,
                    "prose_organisations": [],
                    "diagram_organisations": [],
                    "diagram_only_organisations": [],
                    "diagram_relationship_candidates": [],
                    "page_summaries": [],
                }

            if isinstance(diagram_analysis, dict):
                interpretation["diagram_analysis"] = diagram_analysis
                interpretation["candidate_assertions_require_confirmation"] = True
                raw_diagram_only = diagram_analysis.get("diagram_only_organisations")
                diagram_only = (
                    list(raw_diagram_only) if isinstance(raw_diagram_only, list) else []
                )
                if diagram_only:
                    tags = interpretation.get("subject_tags")
                    if not isinstance(tags, list):
                        tags = []
                    if "diagram_organisation_candidates" not in tags:
                        tags.append("diagram_organisation_candidates")
                    interpretation["subject_tags"] = tags

                    names: list[str] = []
                    for item in diagram_only[:3]:
                        if isinstance(item, Mapping):
                            name = item.get("name")
                            if isinstance(name, str) and name.strip():
                                names.append(name.strip())
                    if names:
                        names_text = ", ".join(names)
                        existing_description = interpretation.get("description")
                        if (
                            isinstance(existing_description, str)
                            and existing_description.strip()
                        ):
                            interpretation["description"] = (
                                f"{existing_description.strip()} "
                                f"Diagram-derived organisation candidates: {names_text}."
                            )
                        else:
                            interpretation["description"] = (
                                f"Diagram-derived organisation candidates: {names_text}."
                            )

    description_text = interpretation.get("description")
    description = description_text if isinstance(description_text, str) else None
    content_for_persist = extracted_text if isinstance(extracted_text, str) else None
    content_truncated = False
    if (
        isinstance(content_for_persist, str)
        and max_persist_content_chars > 0
        and len(content_for_persist) > max_persist_content_chars
    ):
        content_for_persist = content_for_persist[:max_persist_content_chars]
        content_truncated = True
    if max_persist_content_chars == 0:
        content_for_persist = None

    persisted_relations: list[dict[str, Any]] = []
    persisted_structural_relations: list[dict[str, Any]] = []
    persist_errors: list[dict[str, Any]] = []
    arxiv_id_candidates: list[str] = []
    selected_arxiv_id: str | None = None
    scholarly_representation: dict[str, Any] = {
        "attempted": False,
        "verified": False,
        "reason": "not_applicable",
    }
    subtype_assertion: dict[str, Any] | None = None
    subtype_assertion_outcome = "not_attempted"
    if persist:
        if isinstance(subtype_type_concept_id, str) and subtype_type_concept_id.strip():
            with _with_namespace_actor_override(effective_namespace):
                subtype_assertion = add_relationship(
                    source_id=concept_id,
                    predicate="is_an_instance_of",
                    target=subtype_type_concept_id.strip(),
                )
            if not isinstance(subtype_assertion, dict):
                subtype_assertion = {
                    "success": False,
                    "error": "unexpected_subtype_assertion_response_shape",
                    "response_type": type(subtype_assertion).__name__,
                }
            if subtype_assertion.get("success") is True:
                subtype_assertion_outcome = (
                    "subtype_added"
                    if bool(subtype_assertion.get("forward_modified"))
                    else "subtype_already_present"
                )
                persisted_structural_relations.append(
                    {
                        "predicate": "is_an_instance_of",
                        "target_id": subtype_type_concept_id.strip(),
                        "modified": bool(subtype_assertion.get("forward_modified")),
                    }
                )
            else:
                subtype_assertion_outcome = "subtype_assertion_failed"
                persist_errors.append(
                    {
                        "predicate": "is_an_instance_of",
                        "target_id": subtype_type_concept_id.strip(),
                        "error": subtype_assertion.get("error")
                        or "subtype_assertion_failed",
                        "details": subtype_assertion,
                    }
                )
        else:
            subtype_assertion_outcome = "subtype_not_determinable"

        writes: list[tuple[str, str | None]] = []
        if persist_description and isinstance(description, str) and description.strip():
            writes.append(("hasDescription", description.strip()))
        if (
            persist_content
            and isinstance(content_for_persist, str)
            and content_for_persist.strip()
        ):
            writes.append(("hasContent", content_for_persist.strip()))
        if persist_interpretation_json:
            writes.append(
                (
                    "#V#has_file_copy_interpretation_json",
                    json.dumps(interpretation, ensure_ascii=False),
                )
            )

        with _with_namespace_actor_override(effective_namespace):
            for predicate, text_value in writes:
                if not isinstance(text_value, str) or not text_value.strip():
                    continue
                try:
                    relation = upsert_singleton_text_relation(
                        subject_concept_id=concept_id,
                        predicate=predicate,
                        text=text_value,
                        lang="en-NZ",
                        garbage_collect=True,
                        context={
                            "source": "interpret_file_copy",
                            "file_kind": file_kind,
                        },
                    )
                    relation_id = (
                        relation.get("relation_id")
                        if isinstance(relation, dict)
                        else None
                    )
                    persisted_relations.append(
                        {
                            "predicate": predicate,
                            "relation_id": relation_id,
                            "text_length": len(text_value),
                        }
                    )
                    maybe_sync_concept_text_relations_to_rag(
                        namespace=effective_namespace,
                        concept_id=concept_id,
                        predicate=predicate,
                    )
                except Exception as exc:
                    persist_errors.append(
                        {
                            "predicate": predicate,
                            "error": str(exc),
                        }
                    )

        if file_kind == "document":
            arxiv_id_candidates = extract_arxiv_id_candidates(
                kwargs.get("arxiv_id"),
                kwargs.get("source_identifier"),
                original_filename,
                read_payload.get("blob"),
                extracted_text,
                content_for_persist,
            )
            selected_arxiv_id = arxiv_id_candidates[0] if arxiv_id_candidates else None
            if isinstance(selected_arxiv_id, str) and selected_arxiv_id.strip():
                scholarly_representation = {
                    "attempted": True,
                    "verified": False,
                    "arxiv_id": selected_arxiv_id,
                    "reason": "metadata_resolution_pending",
                }

                metadata_payload = _get_paper_metadata(arxiv_id=selected_arxiv_id)
                metadata_error: str | None = None
                metadata_record: dict[str, Any] | None = None
                if isinstance(metadata_payload, dict):
                    if metadata_payload.get("success") is False:
                        metadata_error = str(
                            metadata_payload.get("error")
                            or metadata_payload.get("message")
                            or "metadata_fetch_failed"
                        )
                    elif isinstance(metadata_payload.get("paper"), Mapping):
                        metadata_record = dict(metadata_payload.get("paper") or {})
                    elif isinstance(metadata_payload.get("result"), Mapping):
                        metadata_record = dict(metadata_payload.get("result") or {})
                    else:
                        metadata_record = dict(metadata_payload)
                else:
                    metadata_error = f"unexpected_metadata_response:{type(metadata_payload).__name__}"

                user_concept_id, _organisation_concept_id = _resolve_rag_actor_scope_ids(
                    ns_report
                )
                if not isinstance(user_concept_id, str) or not user_concept_id.strip():
                    scholarly_representation = {
                        "attempted": True,
                        "verified": False,
                        "arxiv_id": selected_arxiv_id,
                        "reason": "missing_user_context_for_arxiv_representation",
                    }
                elif metadata_error:
                    scholarly_representation = {
                        "attempted": True,
                        "verified": False,
                        "arxiv_id": selected_arxiv_id,
                        "reason": metadata_error,
                        "metadata_error": metadata_error,
                    }
                else:
                    with _with_namespace_actor_override(effective_namespace):
                        scholarly_representation = (
                            materialise_scholarly_representation_for_arxiv_file_copy(
                                user_concept_id=user_concept_id.strip(),
                                arxiv_id=selected_arxiv_id,
                                file_copy_concept_id=concept_id,
                                metadata=metadata_record,
                                logger=logger,
                            )
                        )
                    scholarly_representation["attempted"] = True
                    scholarly_representation["metadata_source"] = "get_paper_metadata"
                    scholarly_representation["metadata_available"] = bool(metadata_record)

                if not bool(scholarly_representation.get("verified")):
                    persist_errors.append(
                        {
                            "predicate": "#V#scholarly_representation_verification",
                            "error": "scholarly_representation_not_verified",
                            "details": scholarly_representation,
                        }
                    )
    else:
        subtype_assertion_outcome = "persist_disabled"
        scholarly_representation = {
            "attempted": False,
            "verified": False,
            "reason": "persist_disabled",
        }

    content_text = extracted_text if isinstance(extracted_text, str) else None
    text_preview = None
    if isinstance(content_text, str):
        text_preview = (
            content_text[:4000] + "..."
            if len(content_text) > 4000
            else content_text
        )

    diagram_only_count = 0
    diagram_candidate_count = 0
    diagram_relationship_count = 0
    if isinstance(diagram_analysis, Mapping):
        raw_only = diagram_analysis.get("diagram_only_organisations")
        raw_candidates = diagram_analysis.get("diagram_organisations")
        raw_relationships = diagram_analysis.get("diagram_relationship_candidates")
        diagram_only_count = len(raw_only) if isinstance(raw_only, list) else 0
        diagram_candidate_count = len(raw_candidates) if isinstance(raw_candidates, list) else 0
        diagram_relationship_count = (
            len(raw_relationships) if isinstance(raw_relationships, list) else 0
        )

    return {
        "success": len(persist_errors) == 0,
        "concept_id": concept_id,
        "file_kind": file_kind,
        "subtype_type_concept_id": (
            subtype_type_concept_id
            if isinstance(subtype_type_concept_id, str) and subtype_type_concept_id.strip()
            else None
        ),
        "description": description,
        "content_type": content_type,
        "original_filename": original_filename,
        "text_length": len(content_text) if isinstance(content_text, str) else 0,
        "text_preview": text_preview,
        "text": content_text if include_text else None,
        "content_truncated_for_persist": content_truncated,
        "interpretation": interpretation,
        "diagram_analysis": diagram_analysis,
        "persisted": persist and len(persist_errors) == 0,
        "persisted_relations": persisted_relations,
        "persisted_structural_relations": persisted_structural_relations,
        "persist_errors": persist_errors,
        "diagnostics": {
            "subtype_detection": subtype_detection,
            "subtype_assertion_outcome": subtype_assertion_outcome,
            "subtype_assertion": subtype_assertion,
            "arxiv_id_candidates": arxiv_id_candidates,
            "selected_arxiv_id": selected_arxiv_id,
            "diagram_analysis": {
                "enabled": include_pdf_diagram_analysis,
                "available": (
                    bool(diagram_analysis.get("available"))
                    if isinstance(diagram_analysis, Mapping)
                    else False
                ),
                "diagram_only_count": diagram_only_count,
                "diagram_candidate_count": diagram_candidate_count,
                "diagram_relationship_count": diagram_relationship_count,
            },
        },
        "scholarly_representation": scholarly_representation,
        "namespace": effective_namespace,
        **ns_report,
        "read_result": {
            "text_extraction": read_payload.get("text_extraction"),
            "text_extraction_error": read_payload.get("text_extraction_error"),
            "size_bytes": read_payload.get("size_bytes"),
            "byte_length": read_payload.get("byte_length"),
            "blob": read_payload.get("blob"),
        },
        "image_fetch_error": image_fetch_error,
    }


def _index_file_copy(**kwargs):
    from ...services.rag_service import RAGBackendUnavailable, get_rag_service

    concept_id = kwargs.get("concept_id") or kwargs.get("file_copy_concept_id")
    if not isinstance(concept_id, str) or not concept_id.strip():
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: concept_id",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID of a #V#computer_file_copy instance"],
        )
    concept_id = concept_id.strip()

    ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
    ns_error = _rag_namespace_resolution_error(ns_report)
    if ns_error is not None:
        return ns_error
    ns = ns_report.get("namespace")
    if not isinstance(ns, str) or not ns.strip():
        return make_error_response(
            "namespace_required",
            "File-copy indexing requires authenticated user context (namespace)",
            details={"namespace_report": ns_report},
        )
    ns = ns.strip()

    max_bytes = kwargs.get("max_bytes")
    if max_bytes is None:
        max_bytes = 10_000_000
    allow_large = bool(kwargs.get("allow_large", False))

    read_payload = _read_file_copy(
        concept_id=concept_id,
        max_bytes=max_bytes,
        allow_large=allow_large,
        as_text=True,
        namespace=ns,
    )
    if not isinstance(read_payload, dict):
        return make_error_response(
            "read_file_copy_failed",
            "Unexpected read_file_copy response shape",
            details={"response_type": type(read_payload).__name__},
        )
    if read_payload.get("success") is not True:
        return {
            "success": False,
            "error": read_payload.get("error") or read_payload.get("error_code"),
            "message": read_payload.get("message")
            or "Failed to read file-copy bytes for indexing",
            "concept_id": concept_id,
            "namespace": ns,
            **ns_report,
            "read_result": read_payload,
        }

    text_payload = read_payload.get("text")
    text = text_payload if isinstance(text_payload, str) else ""
    if not text.strip():
        return {
            "success": False,
            "error": "unsupported_or_empty_content",
            "message": (
                "File-copy content could not be extracted into indexable text."
            ),
            "concept_id": concept_id,
            "namespace": ns,
            **ns_report,
            "read_result": read_payload,
        }

    try:
        from ...services.computer_file_copy_service import build_file_copy_artifact_record

        artifact_record = build_file_copy_artifact_record(file_copy_concept_id=concept_id)
    except Exception:
        artifact_record = None

    doc_id_raw = kwargs.get("document_id")
    if isinstance(doc_id_raw, str) and doc_id_raw.strip():
        doc_id = doc_id_raw.strip()
    else:
        doc_id = f"file_copy:{concept_id}"

    metadata: dict[str, Any] = {
        "source": "file_copy_blob",
        "concept_id": concept_id,
        "namespace": ns,
        "namespace_source": ns_report.get("namespace_source"),
        "content_type": read_payload.get("content_type"),
        "original_filename": read_payload.get("original_filename"),
        "size_bytes": read_payload.get("size_bytes"),
        "blob": read_payload.get("blob"),
    }
    if isinstance(artifact_record, dict):
        metadata["artifact_record"] = artifact_record
        provenance = artifact_record.get("provenance")
        if isinstance(provenance, dict):
            metadata["artifact_provenance"] = provenance

    doc = {
        "id": doc_id,
        "text": text,
        "metadata": metadata,
    }

    try:
        rag = get_rag_service()
    except RAGBackendUnavailable as exc:
        return make_error_response(
            "rag_backend_unavailable",
            f"RAG backend unavailable: {exc}",
            details={"exception_type": "RAGBackendUnavailable"},
        )

    try:
        indexed_count, failed_count = rag.upsert_documents([doc], namespace=ns)
    except Exception as exc:
        return make_error_response(
            "rag_upsert_failed",
            f"RAG upsert failed: {exc}",
            details={
                "exception_type": type(exc).__name__,
                "concept_id": concept_id,
                "document_id": doc_id,
                "namespace": ns,
            },
            suggestions=[
                "Verify RAG backend configuration and embedding model availability",
                "Retry with a smaller max_bytes if the file is very large",
            ],
        )

    payload = {
        "success": indexed_count > 0 and failed_count == 0,
        "concept_id": concept_id,
        "document_id": doc_id,
        "indexed_count": int(indexed_count or 0),
        "failed_count": int(failed_count or 0),
        "namespace": ns,
        **ns_report,
        "text_length": len(text),
        "content_type": read_payload.get("content_type"),
        "original_filename": read_payload.get("original_filename"),
    }
    if isinstance(artifact_record, dict):
        payload["artifact_record"] = artifact_record
    return payload


def _import_local_file_copy(**kwargs):
    from pathlib import Path
    from ...security.access_control import get_effective_user_concept_id
    from ...services.computer_file_copy_service import import_local_file_copy

    local_path = kwargs.get("local_path")
    if not isinstance(local_path, str) or not local_path.strip():
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: local_path",
            details={"missing": ["local_path"]},
            suggestions=[
                "Provide a local filesystem path within the current workspace"
            ],
        )
    local_path = local_path.strip()

    ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
    ns_error = _rag_namespace_resolution_error(ns_report)
    if ns_error is not None:
        return ns_error
    ns = ns_report.get("namespace")
    if not isinstance(ns, str) or not ns.strip():
        return make_error_response(
            "namespace_required",
            "Local file import requires authenticated user context (namespace)",
            details={"namespace_report": ns_report},
        )
    ns = ns.strip()

    type_concept_id_raw = kwargs.get("type_concept_id")
    type_concept_id = (
        type_concept_id_raw.strip()
        if isinstance(type_concept_id_raw, str) and type_concept_id_raw.strip()
        else "#V#computer_file_copy"
    )
    source_system_raw = kwargs.get("source_system")
    source_system = (
        source_system_raw.strip()
        if isinstance(source_system_raw, str) and source_system_raw.strip()
        else "filesystem_import"
    )
    source_identifier = kwargs.get("source_identifier")
    source_uri = kwargs.get("source_uri")
    source_identifier = (
        source_identifier.strip()
        if isinstance(source_identifier, str) and source_identifier.strip()
        else None
    )
    source_uri = (
        source_uri.strip()
        if isinstance(source_uri, str) and source_uri.strip()
        else None
    )

    workspace_root = Path(__file__).resolve().parents[4]

    def _run_import():
        user_concept_id = get_effective_user_concept_id()
        if not isinstance(user_concept_id, str) or not user_concept_id.strip():
            return make_error_response(
                "authentication_required",
                "User authentication required to import local files",
                suggestions=["Ensure user context is set before calling this tool"],
            )

        result = import_local_file_copy(
            local_path=local_path,
            user_concept_id=user_concept_id.strip(),
            type_concept_id=type_concept_id,
            allowed_root=workspace_root,
            source_system=source_system,
            source_identifier=source_identifier,
            source_uri=source_uri,
        )
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("namespace", ns)
            result.update(ns_report)
            result["allowed_root"] = str(workspace_root)
        return result

    with _with_namespace_actor_override(ns):
        return _run_import()


def _list_recent_screenshots(**kwargs):
    import base64
    import mimetypes
    import os
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
    keyword_hints = ("screenshot", "screen shot", "snip", "capture")

    def _coerce_int(
        value: Any,
        *,
        default: int,
        minimum: int,
        maximum: int,
        field_name: str,
    ) -> int:
        if value is None:
            return default
        try:
            parsed = int(value)
        except Exception:
            raise ValueError(f"Invalid {field_name}: expected integer")
        if parsed < minimum:
            parsed = minimum
        if parsed > maximum:
            parsed = maximum
        return parsed

    try:
        limit = _coerce_int(
            kwargs.get("limit"),
            default=12,
            minimum=1,
            maximum=100,
            field_name="limit",
        )
        max_scan_files = _coerce_int(
            kwargs.get("max_scan_files"),
            default=300,
            minimum=20,
            maximum=3000,
            field_name="max_scan_files",
        )
        max_base64_bytes = _coerce_int(
            kwargs.get("max_base64_bytes"),
            default=5 * 1024 * 1024,
            minimum=1024,
            maximum=50 * 1024 * 1024,
            field_name="max_base64_bytes",
        )
        lookback_hours_raw = kwargs.get("lookback_hours")
        if lookback_hours_raw is None:
            lookback_hours = 24.0
        else:
            lookback_hours = float(lookback_hours_raw)
            if lookback_hours <= 0:
                lookback_hours = 24.0
            lookback_hours = min(lookback_hours, 24.0 * 30.0)
    except ValueError as exc:
        return make_error_response(
            "invalid_parameter",
            str(exc),
            details={"exception_type": "ValueError"},
        )

    include_base64 = bool(kwargs.get("include_base64", False))
    match_clipboard = bool(kwargs.get("match_clipboard", True))

    paths_raw = kwargs.get("paths")
    explicit_paths: list[Path] = []
    if paths_raw is not None:
        if not isinstance(paths_raw, list):
            return make_error_response(
                "invalid_parameter",
                "paths must be a list of directory strings when provided",
                details={"parameter": "paths"},
            )
        for raw in paths_raw:
            if isinstance(raw, str) and raw.strip():
                explicit_paths.append(Path(raw.strip()))

    home_dir = Path.home()
    env_paths_raw = os.getenv("VON_INTERNAL_MCP_SCREENSHOT_PATHS", "")
    env_paths: list[Path] = []
    if isinstance(env_paths_raw, str) and env_paths_raw.strip():
        for token in re.split(r"[;\n\r]+", env_paths_raw):
            token = token.strip()
            if token:
                env_paths.append(Path(token))

    if explicit_paths:
        candidate_dirs = explicit_paths
    else:
        candidate_dirs = [
            home_dir / "Pictures" / "Screenshots",
            home_dir / "OneDrive" / "Pictures" / "Screenshots",
            home_dir / "Desktop",
            *env_paths,
        ]

    # Preserve first occurrence and keep deterministic ordering.
    seen_dir_keys: set[str] = set()
    ordered_dirs: list[Path] = []
    for directory in candidate_dirs:
        try:
            key = str(directory.expanduser().resolve(strict=False)).lower()
        except Exception:
            key = str(directory).lower()
        if key in seen_dir_keys:
            continue
        seen_dir_keys.add(key)
        ordered_dirs.append(directory)

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=lookback_hours)

    candidate_items: list[dict[str, Any]] = []
    scanned_files_count = 0
    seen_paths: set[str] = set()

    def _is_screenshot_like_name(path_obj: Path) -> bool:
        name_lower = path_obj.name.lower()
        return any(hint in name_lower for hint in keyword_hints)

    for raw_dir in ordered_dirs:
        directory = raw_dir.expanduser()
        if not directory.exists() or not directory.is_dir():
            continue
        try:
            entries = list(directory.iterdir())
        except Exception:
            continue

        for entry in entries:
            if scanned_files_count >= max_scan_files:
                break
            if not entry.is_file():
                continue
            ext = entry.suffix.lower()
            if ext not in image_exts:
                continue

            if not explicit_paths:
                parent_lower = entry.parent.name.lower()
                if parent_lower != "screenshots" and not _is_screenshot_like_name(entry):
                    continue

            try:
                stat = entry.stat()
            except Exception:
                continue
            modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if modified_at < cutoff:
                continue

            try:
                path_key = str(entry.resolve(strict=False)).lower()
            except Exception:
                path_key = str(entry).lower()
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)

            scanned_files_count += 1
            candidate_items.append(
                {
                    "path": str(entry),
                    "filename": entry.name,
                    "size_bytes": int(stat.st_size),
                    "modified_at_dt": modified_at,
                    "modified_at": modified_at.isoformat(),
                    "mime_type": mimetypes.guess_type(entry.name)[0]
                    or "application/octet-stream",
                    "width": None,
                    "height": None,
                    "hash64_hex": None,
                    "clipboard_distance": None,
                    "base64_omitted_reason": None,
                }
            )
        if scanned_files_count >= max_scan_files:
            break

    # Newest first before optional clipboard re-ranking.
    candidate_items.sort(
        key=lambda item: item.get("modified_at_dt") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    pil_available = False
    pil_import_error: str | None = None
    Image = None
    ImageGrab = None

    if match_clipboard or include_base64:
        try:
            from PIL import Image as _PILImage  # type: ignore[import-not-found]
            from PIL import ImageGrab as _PILImageGrab  # type: ignore[import-not-found]

            pil_available = True
            Image = _PILImage
            ImageGrab = _PILImageGrab
        except Exception as exc:
            pil_available = False
            pil_import_error = str(exc)

    def _average_hash_hex(image_obj: Any) -> tuple[str, int]:
        # 8x8 luminance average-hash: compact and fast enough for screenshot matching.
        grayscale = image_obj.convert("L").resize((8, 8))
        pixels = list(grayscale.getdata())
        avg = sum(int(px) for px in pixels) / 64.0
        bits = 0
        for idx, pixel in enumerate(pixels):
            if int(pixel) >= avg:
                bits |= 1 << idx
        return f"{bits:016x}", bits

    clipboard_info: dict[str, Any] = {
        "attempted": bool(match_clipboard),
        "available": False,
        "source": None,
        "width": None,
        "height": None,
        "hash64_hex": None,
        "error": None,
    }
    clipboard_hash_bits: int | None = None

    if match_clipboard:
        if not pil_available or ImageGrab is None:
            clipboard_info["error"] = (
                f"Pillow unavailable: {pil_import_error}"
                if pil_import_error
                else "Pillow unavailable"
            )
        else:
            try:
                clipboard_payload = ImageGrab.grabclipboard()
                clipboard_image = None
                clipboard_source = None
                if hasattr(clipboard_payload, "size") and hasattr(
                    clipboard_payload, "convert"
                ):
                    clipboard_image = clipboard_payload
                    clipboard_source = "clipboard_image"
                elif isinstance(clipboard_payload, list):
                    for path_value in clipboard_payload:
                        if not isinstance(path_value, str):
                            continue
                        path_obj = Path(path_value)
                        if path_obj.suffix.lower() not in image_exts:
                            continue
                        try:
                            if Image is not None:
                                with Image.open(path_obj) as img:
                                    clipboard_image = img.copy()
                                    clipboard_source = "clipboard_file_list"
                                    break
                        except Exception:
                            continue

                if clipboard_image is not None:
                    hash_hex, hash_bits = _average_hash_hex(clipboard_image)
                    clipboard_hash_bits = hash_bits
                    width, height = getattr(clipboard_image, "size", (None, None))
                    clipboard_info.update(
                        {
                            "available": True,
                            "source": clipboard_source,
                            "hash64_hex": hash_hex,
                            "width": int(width) if isinstance(width, int) else None,
                            "height": int(height) if isinstance(height, int) else None,
                        }
                    )
                else:
                    clipboard_info["error"] = "No image data available in clipboard"
            except Exception as exc:
                clipboard_info["error"] = str(exc)

    if clipboard_hash_bits is not None and pil_available and Image is not None:
        for item in candidate_items:
            file_path = Path(item["path"])
            try:
                with Image.open(file_path) as img:
                    width, height = img.size
                    hash_hex, hash_bits = _average_hash_hex(img)
                    item["width"] = int(width)
                    item["height"] = int(height)
                    item["hash64_hex"] = hash_hex
                    item["clipboard_distance"] = int(
                        (hash_bits ^ clipboard_hash_bits).bit_count()
                    )
            except Exception:
                continue

        candidate_items.sort(
            key=lambda item: (
                item["clipboard_distance"]
                if isinstance(item.get("clipboard_distance"), int)
                else 10**9,
                -int(item["modified_at_dt"].timestamp()),
            )
        )

    selected = candidate_items[:limit]

    if include_base64:
        for item in selected:
            file_path = Path(item["path"])
            size_bytes = int(item.get("size_bytes") or 0)
            if size_bytes > max_base64_bytes:
                item["base64_omitted_reason"] = (
                    f"File size {size_bytes} exceeds max_base64_bytes {max_base64_bytes}"
                )
                continue
            try:
                payload = file_path.read_bytes()
                item["content_base64"] = base64.b64encode(payload).decode("ascii")
            except Exception as exc:
                item["base64_omitted_reason"] = f"Failed to read file: {exc}"

    for item in selected:
        item.pop("modified_at_dt", None)

    return {
        "success": True,
        "items": selected,
        "count": len(selected),
        "scan_summary": {
            "candidate_directories": [str(path.expanduser()) for path in ordered_dirs],
            "lookback_hours": lookback_hours,
            "scanned_files": scanned_files_count,
            "matching_files": len(candidate_items),
            "max_scan_files": max_scan_files,
        },
        "clipboard": clipboard_info,
    }


def _get_predicate_extent(**kwargs):
    from ...server.routes.predicate_routes import get_predicate_extent_data

    concept_id = kwargs.get("concept_id") or kwargs.get("predicate_concept_id")
    if not isinstance(concept_id, str) or not concept_id.strip():
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: concept_id",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID of the predicate to query"],
        )

    def _coerce_int(value, field, *, default=None, minimum=None, maximum=None):
        if value is None:
            return default
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid {field}")
        if minimum is not None and value < minimum:
            raise ValueError(f"{field} must be >= {minimum}")
        if maximum is not None and value > maximum:
            value = maximum
        return value

    try:
        limit = _coerce_int(
            kwargs.get("limit"), "limit", default=100, minimum=1, maximum=1000
        )
        offset = _coerce_int(kwargs.get("offset"), "offset", default=0, minimum=0)
        sample_size = _coerce_int(
            kwargs.get("sample_size"),
            "sample_size",
            default=None,
            minimum=1,
            maximum=1000,
        )
        sample_seed = _coerce_int(
            kwargs.get("sample_seed"), "sample_seed", default=None
        )
    except ValueError as exc:
        return make_error_response(
            "invalid_parameter", str(exc), details={"exception_type": "ValueError"}
        )

    sort_by = kwargs.get("sort_by") or "created_at"
    sort_order = kwargs.get("sort_order") or "desc"
    subject_type = kwargs.get("subject_type") or None
    object_type = kwargs.get("object_type") or None
    source_filter = kwargs.get("source") or "all"

    payload = get_predicate_extent_data(
        concept_id=concept_id.strip(),
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_order=sort_order,
        subject_type=subject_type,
        object_type=object_type,
        source_filter=source_filter,
        sample_size=sample_size,
        sample_seed=sample_seed,
    )

    if payload.get("error"):
        return make_error_response(
            "get_salient_relations_error",
            payload.get("error", "Unknown error from get_predicate_extent_data"),
            details={"payload": payload},
        )

    return {"success": True, **payload}


# Search MCP handlers
def _search_web(**kwargs):
    import asyncio
    from .search_proxy_mcp import get_search_proxy, SearchProxyError

    query = kwargs.get("query")
    if not query:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: query",
            details={"missing": ["query"]},
            suggestions=["Provide a search query string"],
        )

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
            details = e.details.to_dict() if e.details else {}
            details["exception_type"] = "SearchProxyError"
            return make_error_response(
                details.get("error_type", "search_proxy_error"),
                str(e),
                details=details,
                suggestions=details.get("suggestions", []),
            )
        except Exception as e:
            return make_error_response(
                "exception",
                f"Unexpected error: {e}",
                details={"exception_type": type(e).__name__},
            )

    return _run_async_compat(_async_search)


def _context_search(**kwargs):
    import asyncio
    from .search_proxy_mcp import get_search_proxy, SearchProxyError

    query = kwargs.get("query")
    context = kwargs.get("context")

    if not query:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: query",
            details={"missing": ["query"]},
            suggestions=["Provide a search query string"],
        )
    if not context:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: context",
            details={"missing": ["context"]},
            suggestions=["Provide background context for the search"],
        )

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
            details = e.details.to_dict() if e.details else {}
            details["exception_type"] = "SearchProxyError"
            return make_error_response(
                details.get("error_type", "search_proxy_error"),
                str(e),
                details=details,
                suggestions=details.get("suggestions", []),
            )
        except Exception as e:
            return make_error_response(
                "exception",
                f"Unexpected error: {e}",
                details={"exception_type": type(e).__name__},
            )

    return _run_async_compat(_async_context_search)


def _qna_search(**kwargs):
    import asyncio
    from .search_proxy_mcp import get_search_proxy, SearchProxyError

    query = kwargs.get("query")
    if not query:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: query",
            details={"missing": ["query"]},
            suggestions=["Provide a question or search query"],
        )

    async def _async_qna_search():
        try:
            proxy = await get_search_proxy()
            return await proxy.qna_search(
                query=query,
                max_results=kwargs.get("max_results", 5),
                search_depth=kwargs.get("search_depth", "advanced"),
            )
        except SearchProxyError as e:
            details = e.details.to_dict() if e.details else {}
            details["exception_type"] = "SearchProxyError"
            return make_error_response(
                details.get("error_type", "search_proxy_error"),
                str(e),
                details=details,
                suggestions=details.get("suggestions", []),
            )
        except Exception as e:
            return make_error_response(
                "exception",
                f"Unexpected error: {e}",
                details={"exception_type": type(e).__name__},
            )

    return _run_async_compat(_async_qna_search)


def _extract_url(**kwargs):
    import asyncio
    from .search_proxy_mcp import get_search_proxy, SearchProxyError

    url = kwargs.get("url")
    if not url:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: url",
            details={"missing": ["url"]},
            suggestions=["Provide a URL to extract content from"],
        )

    async def _async_extract():
        try:
            proxy = await get_search_proxy()
            return await proxy.extract(url=url)
        except SearchProxyError as e:
            details = e.details.to_dict() if e.details else {}
            details["exception_type"] = "SearchProxyError"
            details["url"] = url
            return make_error_response(
                details.get("error_type", "search_proxy_error"),
                str(e),
                details=details,
                suggestions=details.get("suggestions", []),
            )
        except Exception as e:
            return make_error_response(
                "exception",
                f"Unexpected error: {e}",
                details={"exception_type": type(e).__name__, "url": url},
            )

    return _run_async_compat(_async_extract)


def _search_proxy_diagnostics(**kwargs):
    """Get diagnostics and health status for the Tavily search proxy.

    Use this to debug search/extraction failures, check Tavily API connectivity,
    and review recent call history.
    """
    from .search_proxy_mcp import get_search_proxy, SearchProxyError

    include_health_check = kwargs.get("include_health_check", False)

    async def _async_diagnostics():
        try:
            proxy = await get_search_proxy()
            diagnostics = proxy.get_diagnostics()

            if include_health_check:
                health = await proxy.check_health()
                diagnostics["health_check"] = health

            return diagnostics
        except SearchProxyError as e:
            details = e.details.to_dict() if e.details else {}
            details["exception_type"] = "SearchProxyError"
            return {
                "error": str(e),
                "details": details,
                "proxy_initialised": False,
            }
        except Exception as e:
            return {
                "error": f"Failed to get diagnostics: {e}",
                "exception_type": type(e).__name__,
                "proxy_initialised": False,
            }

    return _run_async_compat(_async_diagnostics)


def _linkedin_proxy_error_response(exc: Exception) -> dict[str, Any]:
    return make_error_response(
        "linkedin_proxy_error",
        str(exc),
        details={"exception_type": type(exc).__name__},
        suggestions=[
            "Check LinkedIn MCP command/path configuration",
            "Check LinkedIn MCP Python dependencies and data-root access",
        ],
    )


def _linkedin_list_exports(**kwargs):
    from .linkedin_proxy_mcp import get_linkedin_proxy, LinkedInProxyError

    refresh = bool(kwargs.get("refresh", False))

    async def _async_list_exports():
        proxy = await get_linkedin_proxy()
        return await proxy.list_exports(refresh=refresh)

    try:
        return _run_async_compat(_async_list_exports)
    except LinkedInProxyError as exc:
        return _linkedin_proxy_error_response(exc)
    except Exception as exc:
        return _linkedin_proxy_error_response(exc)


def _linkedin_list_files(**kwargs):
    from .linkedin_proxy_mcp import get_linkedin_proxy, LinkedInProxyError

    export_name = kwargs.get("export_name")
    if not export_name:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: export_name",
            details={"missing": ["export_name"]},
            suggestions=["Call linkedin_list_exports first to discover export names"],
        )

    async def _async_list_files():
        proxy = await get_linkedin_proxy()
        return await proxy.list_files(export_name=str(export_name))

    try:
        return _run_async_compat(_async_list_files)
    except LinkedInProxyError as exc:
        return _linkedin_proxy_error_response(exc)
    except Exception as exc:
        return _linkedin_proxy_error_response(exc)


def _linkedin_get_profile(**kwargs):
    from .linkedin_proxy_mcp import get_linkedin_proxy, LinkedInProxyError

    export_name = kwargs.get("export_name")
    if not export_name:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: export_name",
            details={"missing": ["export_name"]},
            suggestions=["Call linkedin_list_exports first to discover export names"],
        )

    async def _async_get_profile():
        proxy = await get_linkedin_proxy()
        return await proxy.get_profile(export_name=str(export_name))

    try:
        return _run_async_compat(_async_get_profile)
    except LinkedInProxyError as exc:
        return _linkedin_proxy_error_response(exc)
    except Exception as exc:
        return _linkedin_proxy_error_response(exc)


def _linkedin_get_csv_data(**kwargs):
    from .linkedin_proxy_mcp import get_linkedin_proxy, LinkedInProxyError

    export_name = kwargs.get("export_name")
    file_name = kwargs.get("file_name")
    if not export_name or not file_name:
        missing = []
        if not export_name:
            missing.append("export_name")
        if not file_name:
            missing.append("file_name")
        return make_error_response(
            "missing_parameter",
            f"Missing required parameter(s): {', '.join(missing)}",
            details={"missing": missing},
            suggestions=[
                "Provide export_name and file_name",
                "Call linkedin_list_files to discover available CSV files",
            ],
        )

    try:
        limit = int(kwargs.get("limit", 10))
    except Exception:
        return make_error_response(
            "invalid_parameter",
            "limit must be an integer",
            details={"parameter": "limit"},
        )
    limit = max(1, min(200, limit))

    async def _async_get_csv_data():
        proxy = await get_linkedin_proxy()
        return await proxy.get_csv_data(
            export_name=str(export_name),
            file_name=str(file_name),
            limit=limit,
        )

    try:
        return _run_async_compat(_async_get_csv_data)
    except LinkedInProxyError as exc:
        return _linkedin_proxy_error_response(exc)
    except Exception as exc:
        return _linkedin_proxy_error_response(exc)


def _linkedin_get_company_stats(**kwargs):
    from .linkedin_proxy_mcp import get_linkedin_proxy, LinkedInProxyError

    export_name = kwargs.get("export_name")
    if not export_name:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: export_name",
            details={"missing": ["export_name"]},
            suggestions=["Call linkedin_list_exports first to discover export names"],
        )

    try:
        top_n = int(kwargs.get("top_n", 10))
    except Exception:
        return make_error_response(
            "invalid_parameter",
            "top_n must be an integer",
            details={"parameter": "top_n"},
        )
    top_n = max(1, min(200, top_n))

    async def _async_get_company_stats():
        proxy = await get_linkedin_proxy()
        return await proxy.get_company_stats(export_name=str(export_name), top_n=top_n)

    try:
        return _run_async_compat(_async_get_company_stats)
    except LinkedInProxyError as exc:
        return _linkedin_proxy_error_response(exc)
    except Exception as exc:
        return _linkedin_proxy_error_response(exc)


def _linkedin_get_messages(**kwargs):
    from .linkedin_proxy_mcp import get_linkedin_proxy, LinkedInProxyError

    export_name = kwargs.get("export_name")
    if not export_name:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: export_name",
            details={"missing": ["export_name"]},
            suggestions=["Call linkedin_list_exports first to discover export names"],
        )

    query = kwargs.get("query")
    query_text = str(query) if query is not None else ""

    async def _async_get_messages():
        proxy = await get_linkedin_proxy()
        return await proxy.get_messages(
            export_name=str(export_name),
            query=query_text,
        )

    try:
        return _run_async_compat(_async_get_messages)
    except LinkedInProxyError as exc:
        return _linkedin_proxy_error_response(exc)
    except Exception as exc:
        return _linkedin_proxy_error_response(exc)


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
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: url",
            details={"missing": ["url"]},
            suggestions=["Provide a URL to extract content from"],
        )

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
        return make_error_response(
            "missing_parameter",
            "Missing concept_id parameter",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID to delete"],
        )

    return simulate_or_delete_concept(concept_id, execute=not simulate)


def _merge_concepts(**kwargs):
    from ...services.concept_merge_service import merge_concepts

    source_id = kwargs.get("source_id")
    target_id = kwargs.get("target_id")
    simulate = kwargs.get("simulate", True)

    if not source_id or not target_id:
        missing = []
        if not source_id:
            missing.append("source_id")
        if not target_id:
            missing.append("target_id")
        return make_error_response(
            "missing_parameter",
            f"Missing {' and '.join(missing)} parameter",
            details={"missing": missing},
            suggestions=[
                "Provide both source_id (concept to merge from) and target_id (concept to merge into)"
            ],
        )

    return merge_concepts(source_id, target_id, simulate=simulate)


def _rename_concept(**kwargs):
    """Rename a concept's ID while preserving its GUID (JVNAUTOSCI-945).

    The concept's GUID remains unchanged, ensuring stable references.
    The old concept_id is preserved as a CODE alias for backwards compatibility.
    All relationship references are automatically updated.
    """
    from ...services.concept_rename_service import rename_concept

    old_id = kwargs.get("old_id") or kwargs.get("concept_id")
    new_id = kwargs.get("new_id")
    simulate = kwargs.get("simulate", True)
    preserve_alias = kwargs.get("preserve_alias", True)

    if not old_id:
        return make_error_response(
            "missing_parameter",
            "Missing old_id (or concept_id) parameter",
            details={"missing": ["old_id"]},
            suggestions=["Provide the current concept ID to rename"],
        )
    if not new_id:
        return make_error_response(
            "missing_parameter",
            "Missing new_id parameter",
            details={"missing": ["new_id"]},
            suggestions=["Provide the new concept ID to rename to"],
        )

    return rename_concept(
        old_id=old_id,
        new_id=new_id,
        simulate=simulate,
        preserve_alias=preserve_alias,
    )


def _update_concept(**kwargs):
    from ...services.concept_service import update_concept

    concept_id = kwargs.get("concept_id")
    update_data = kwargs.get("update_data")

    if not concept_id:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: concept_id",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID to update"],
        )
    if not update_data or not isinstance(update_data, dict):
        return make_error_response(
            "missing_parameter",
            "Missing or invalid 'update_data' dictionary",
            details={"missing": ["update_data"]},
            suggestions=["Provide update_data as a dictionary with fields to update"],
        )

    try:
        result = update_concept(concept_id=concept_id, update_data=update_data)
        if result:
            return {
                "success": True,
                "concept_id": concept_id,
                "updated_fields": list(update_data.keys()),
            }
        else:
            return make_error_response(
                "update_failed",
                "Update failed or concept not found",
                details={"concept_id": concept_id},
                suggestions=["Check if the concept exists using concept_exists"],
                related_concept_ids=[concept_id],
            )
    except Exception as e:
        return make_error_response(
            "exception",
            str(e),
            details={"exception_type": type(e).__name__, "concept_id": concept_id},
        )


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
            "include_uncertain": (bool, type(None)),
            "uncertainty_mode": (str, type(None)),
            "uncertainty_statuses": (list, type(None)),
        },
        allow_unknown=True,
        description=(
            "get_concept_by_concept_id input: concept_id (required), plus optional"
            " flags to include structural/text relations (include_relations_arg1,"
            " include_relations_any_arg, include_text_relations_arg1), predicate"
            " filtering, paging (limit/offset), concept preview toggling, and"
            " uncertainty retrieval controls (include_uncertain, uncertainty_mode,"
            " uncertainty_statuses)."
        ),
    )


def _concepts_create_input_schema() -> Schema:
    return Schema(
        required={
            "parent_id": str,
            "concepts": list,
        },
        optional={
            "allow_duplicate_instances": (bool,),
            "namespace": (str, type(None)),
            "scope_mode": (str, type(None)),
            "visibility_scope_mode": (str, type(None)),
            "created_by_concept_id": (str, type(None)),
            "organisation_concept_id": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "create_concepts input: parent_id (str, parent concept_id), concepts (list of {name, kind?, description?, notes?}). "
            "kind: 'instance' for individuals, 'type' for subtypes (default), 'predicate' for relationships. "
            "By default, deterministic pre-create lookup blocks duplicate instances/types/predicates; "
            "set allow_duplicate_instances=true to opt into legacy instance suffixing. "
            "Scope defaults to authenticated user+organisation visibility when context is available. "
            "Use scope_mode='organisation_general' for organisation-shared concepts, or scope_mode='global_general' "
            "for broadly visible concepts. organisation_general requires organisation context (namespace #V#user@org or organisation_concept_id). "
            "Optional namespace is accepted and propagated into event-triggered workflow launches for tenancy attribution. "
            "Unknown top-level fields are still tolerated for orchestrator-added context."
        ),
    )


def _concepts_create_output_schema() -> Schema:
    return Schema(
        required={
            "results": list,
            "total": int,
            "successful": int,
        },
        optional={
            "scope_selection": (dict,),
        },
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


def _predicate_extent_input_schema() -> Schema:
    return Schema(
        required={
            "concept_id": str,
        },
        optional={
            "limit": (int, type(None)),
            "offset": (int, type(None)),
            "sort_by": (str, type(None)),
            "sort_order": (str, type(None)),
            "subject_type": (str, type(None)),
            "object_type": (str, type(None)),
            "source": (str, type(None)),
            "sample_size": (int, type(None)),
            "sample_seed": (int, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "get_predicate_extent input: concept_id (predicate concept_id), limit (int, default 100), "
            "offset (int), sort_by (str), sort_order ('asc'|'desc'), subject_type (concept_id), "
            "object_type (concept_id), source ('text_relations'|'structured'|'all'), "
            "sample_size (int, optional random sample size), sample_seed (int, optional)."
        ),
    )


def _predicate_extent_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
            "concept_id": str,
            "extent": list,
            "total_count": int,
            "limit": int,
            "offset": int,
            "has_more": bool,
        },
        optional={
            "sampled": (bool, type(None)),
            "sample_size": (int, type(None)),
            "error": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "get_predicate_extent output: success (bool), concept_id (str), extent (list of triples), "
            "total_count (int), limit (int), offset (int), has_more (bool), sampled (bool, optional), "
            "sample_size (int, optional), error (str if failed)."
        ),
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


def _find_relations_with_argument_input_schema() -> Schema:
    return Schema(
        required={"concept_id": str},
        optional={
            "argument_index": (int, str, type(None)),
            "predicate_filter": (list, type(None)),
            "relation_kind": (str, type(None)),
            "scope": (str, type(None)),
            "include_text_snippets": (bool, type(None)),
            "include_concept_preview": (bool, type(None)),
            "limit": (int, type(None)),
            "offset": (int, type(None)),
            "sort_by": (str, type(None)),
            "include_uncertain": (bool, type(None)),
            "uncertainty_mode": (str, type(None)),
            "uncertainty_statuses": (list, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "find_relations_with_argument input: concept_id (required), argument_index "
            "(int or 'any'), predicate_filter (list of predicate IDs or substrings), "
            "relation_kind ('any'|'binary'|'text'), scope (optional), include_text_snippets "
            "(bool), include_concept_preview (bool), paging (limit/offset), sort_by, "
            "uncertainty retrieval controls (include_uncertain, uncertainty_mode,"
            " uncertainty_statuses), and optional namespace passthrough."
        ),
    )


def _find_relations_with_argument_output_schema() -> Schema:
    return Schema(
        required={
            "concept_id": str,
            "total_hits": int,
            "hits": list,
            "paging": dict,
        },
        optional={},
        allow_unknown=True,
        description=(
            "find_relations_with_argument output: concept_id, total_hits, hits[] "
            "(source_concept_id, predicate_concept_id, relation_kind, argument_indexes, "
            "target_value, optional previews/snippets, relation_metadata, access_granted, "
            "follow_up_actions, score, optional uncertainty metadata), and paging metadata."
        ),
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
        required={},
        optional={
            "relation_id": (str, type(None)),
            "source_id": (str, type(None)),
            "predicate": (str, type(None)),
            "target": (str, type(None)),
            "mode": (str, type(None)),
            "cascade": (str, type(None)),
            "dry_run": (bool, type(None)),
            "confirmed": (bool, type(None)),
            "operator_override": (bool, type(None)),
            "reason": (str, type(None)),
            "request_id": (str, type(None)),
        },
        allow_unknown=True,
        description="remove_relationship input: relation_id OR source_id+predicate+target. Optional mode/cascade/confirmation controls. Only concept-to-concept relationships are supported.",
    )


def _upsert_uncertain_relationship_assertion_input_schema() -> Schema:
    return Schema(
        required={
            "source_id": str,
            "predicate": str,
            "target": str,
            "confidence_score": (float, int),
        },
        optional={
            "status": (str, type(None)),
            "assertion_id": (str, type(None)),
            "provenance": (dict, type(None)),
            "evidence_count": (int, type(None)),
        },
        allow_unknown=True,
        description=(
            "upsert_uncertain_relationship_assertion input: source_id, predicate, target, "
            "confidence_score plus optional status/assertion_id/provenance/evidence_count."
        ),
    )


def _uncertain_relationship_operation_output_schema() -> Schema:
    return Schema(
        required={"success": bool},
        optional={
            "created": (bool, type(None)),
            "already_promoted": (bool, type(None)),
            "already_rejected": (bool, type(None)),
            "assertion": (dict, type(None)),
            "assertions": (list, type(None)),
            "count": (int, type(None)),
            "source_id": (str, type(None)),
            "dry_run": (bool, type(None)),
            "migrated_count": (int, type(None)),
            "migrated_assertion_ids": (list, type(None)),
            "write_result": (dict, type(None)),
            "error": (str, type(None)),
            "error_code": (str, type(None)),
            "error_details": (dict, type(None)),
        },
        allow_unknown=True,
        description=(
            "uncertain relationship lifecycle output: success plus lifecycle payloads "
            "(assertion/assertions/create/promote/reject/migrate metadata) and structured errors."
        ),
    )


def _list_uncertain_relationship_assertions_input_schema() -> Schema:
    return Schema(
        required={"source_id": str},
        optional={
            "predicate": (str, type(None)),
            "statuses": (list, type(None)),
            "include_legacy": (bool, type(None)),
        },
        allow_unknown=True,
        description=(
            "list_uncertain_relationship_assertions input: source_id with optional predicate/status "
            "filters and include_legacy toggle."
        ),
    )


def _promote_uncertain_relationship_assertion_input_schema() -> Schema:
    return Schema(
        required={"source_id": str, "assertion_id": str},
        optional={"operator": (str, type(None))},
        allow_unknown=True,
        description=(
            "promote_uncertain_relationship_assertion input: source_id + assertion_id, optional operator."
        ),
    )


def _reject_uncertain_relationship_assertion_input_schema() -> Schema:
    return Schema(
        required={"source_id": str, "assertion_id": str, "reason": str},
        optional={"operator": (str, type(None))},
        allow_unknown=True,
        description=(
            "reject_uncertain_relationship_assertion input: source_id + assertion_id + rejection reason."
        ),
    )


def _migrate_legacy_hypothesized_relations_input_schema() -> Schema:
    return Schema(
        required={"source_id": str},
        optional={"predicate": (str, type(None)), "dry_run": (bool, type(None))},
        allow_unknown=True,
        description=(
            "migrate_legacy_hypothesized_relations input: source_id with optional predicate filter "
            "and dry_run (default true)."
        ),
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
            "status": (str, type(None)),
            "relation_id": (str, type(None)),
            "mode_returned": (str, type(None)),
            "cascade_policy": (str, type(None)),
            "correlation_id": (str, type(None)),
            "request_id": (str, type(None)),
            "warnings": (list, type(None)),
            "undo_token": (str, type(None)),
            "audit_record_id": (str, type(None)),
            "dry_run": (bool, type(None)),
            "impact": (dict, type(None)),
            "candidates": (list, type(None)),
            "candidate_count": (int, type(None)),
        },
        allow_unknown=True,
        description="remove_relationship output: success (bool), source_id/predicate/target, removed/already_absent, status, relation_id, mode/cascade details, warnings, audit metadata, undo_token, and structured errors.",
    )


def _preview_remove_relationship_input_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "relation_id": (str, type(None)),
            "source_id": (str, type(None)),
            "predicate": (str, type(None)),
            "target": (str, type(None)),
            "request_id": (str, type(None)),
        },
        allow_unknown=True,
        description="preview_remove_relationship input: relation_id OR source_id+predicate with optional target to preview impact before deletion.",
    )


def _preview_remove_relationship_output_schema() -> Schema:
    return Schema(
        required={"success": bool},
        optional={
            "status": (str, type(None)),
            "source_id": (str, type(None)),
            "predicate": (str, type(None)),
            "target": (str, type(None)),
            "relation_id": (str, type(None)),
            "correlation_id": (str, type(None)),
            "request_id": (str, type(None)),
            "dry_run": (bool, type(None)),
            "impact": (dict, type(None)),
            "warnings": (list, type(None)),
            "candidates": (list, type(None)),
            "candidate_count": (int, type(None)),
            "error": (str, type(None)),
            "error_code": (str, type(None)),
            "error_details": (dict, type(None)),
        },
        allow_unknown=True,
        description="preview_remove_relationship output: dry-run impact report with warnings/dependencies, plus not_found/ambiguous/error states without mutation side effects.",
    )


def _remove_relationships_bulk_input_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "relation_ids": (list, type(None)),
            "relations": (list, type(None)),
            "filter": (dict, type(None)),
            "mode": (str, type(None)),
            "cascade": (str, type(None)),
            "dry_run": (bool, type(None)),
            "confirmed": (bool, type(None)),
            "operator_override": (bool, type(None)),
            "reason": (str, type(None)),
            "request_id": (str, type(None)),
            "stop_on_error": (bool, type(None)),
        },
        allow_unknown=True,
        description="remove_relationships_bulk input: explicit relation_ids and/or triple relations and/or filter-based selector for bulk removal with deterministic reporting.",
    )


def _remove_relationships_bulk_output_schema() -> Schema:
    return Schema(
        required={"success": bool},
        optional={
            "status": (str, type(None)),
            "dry_run": (bool, type(None)),
            "mode_returned": (str, type(None)),
            "cascade_policy": (str, type(None)),
            "correlation_id": (str, type(None)),
            "request_id": (str, type(None)),
            "results": (list, type(None)),
            "summary": (dict, type(None)),
            "undo_token": (str, type(None)),
            "document_model_note": (str, type(None)),
            "error": (str, type(None)),
            "error_code": (str, type(None)),
        },
        allow_unknown=True,
        description="remove_relationships_bulk output: deterministic per-item results with aggregate summary, partial-failure reporting, and optional undo_token for soft-delete mode.",
    )


def _undo_relationship_removal_input_schema() -> Schema:
    return Schema(
        required={"undo_token": str},
        optional={
            "request_id": (str, type(None)),
            "confirmed": (bool, type(None)),
        },
        allow_unknown=True,
        description="undo_relationship_removal input: undo_token returned by remove_relationship/remove_relationships_bulk soft-delete operations.",
    )


def _undo_relationship_removal_output_schema() -> Schema:
    return Schema(
        required={"success": bool},
        optional={
            "status": (str, type(None)),
            "undo_token": (str, type(None)),
            "correlation_id": (str, type(None)),
            "request_id": (str, type(None)),
            "restored_count": (int, type(None)),
            "already_restored_count": (int, type(None)),
            "error_count": (int, type(None)),
            "errors": (list, type(None)),
            "error": (str, type(None)),
            "error_code": (str, type(None)),
            "error_details": (dict, type(None)),
        },
        allow_unknown=True,
        description="undo_relationship_removal output: restoration summary for a soft-delete undo token, including partial failure details when relevant.",
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


def _index_file_copy_input_schema() -> Schema:
    return Schema(
        required={
            "concept_id": str,
        },
        optional={
            "namespace": (str, type(None)),
            "document_id": (str, type(None)),
            "max_bytes": (int, type(None)),
            "allow_large": (bool, type(None)),
        },
        allow_unknown=True,
        description=(
            "index_file_copy input: concept_id (str for a #V#computer_file_copy instance), "
            "namespace (required user@org context), optional document_id override, optional "
            "max_bytes and allow_large controls forwarded to read_file_copy."
        ),
    )


def _index_file_copy_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "success": (bool, type(None)),
            "error": (str, type(None)),
            "message": (str, type(None)),
            "concept_id": (str, type(None)),
            "document_id": (str, type(None)),
            "indexed_count": (int, type(None)),
            "failed_count": (int, type(None)),
            "namespace": (str, type(None)),
            "text_length": (int, type(None)),
            "content_type": (str, type(None)),
            "original_filename": (str, type(None)),
            "artifact_record": (dict, type(None)),
            "read_result": (dict, type(None)),
        },
        allow_unknown=True,
        description=(
            "index_file_copy output: success flag plus indexing counts/document_id for a "
            "blob-backed file-copy ingestion into RAG."
        ),
    )


def _interpret_file_copy_input_schema() -> Schema:
    return Schema(
        required={
            "concept_id": str,
        },
        optional={
            "namespace": (str, type(None)),
            "max_bytes": (int, type(None)),
            "allow_large": (bool, type(None)),
            "persist": (bool, type(None)),
            "persist_description": (bool, type(None)),
            "persist_content": (bool, type(None)),
            "persist_interpretation_json": (bool, type(None)),
            "include_semantic_description": (bool, type(None)),
            "include_pdf_diagram_analysis": (bool, type(None)),
            "max_diagram_pages": (int, type(None)),
            "max_diagram_candidates": (int, type(None)),
            "vision_model": (str, type(None)),
            "semantic_prompt": (str, type(None)),
            "include_text": (bool, type(None)),
            "max_persist_content_chars": (int, type(None)),
        },
        allow_unknown=True,
        description=(
            "interpret_file_copy input: concept_id for a #V#computer_file_copy instance. "
            "Optionally pass namespace, max_bytes/allow_large, and persistence controls. "
            "For image files (including screenshots, faces, and building photos), this tool "
            "extracts OCR and semantic descriptions. For PDF documents, it can also perform "
            "diagram-aware organisation candidate extraction with provenance (candidate-only; "
            "human confirmation required) and persists canonical hasDescription/hasContent "
            "plus structured interpretation metadata."
        ),
    )


def _interpret_file_copy_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "success": (bool, type(None)),
            "error": (str, type(None)),
            "message": (str, type(None)),
            "concept_id": (str, type(None)),
            "file_kind": (str, type(None)),
            "subtype_type_concept_id": (str, type(None)),
            "description": (str, type(None)),
            "content_type": (str, type(None)),
            "original_filename": (str, type(None)),
            "text_length": (int, type(None)),
            "text_preview": (str, type(None)),
            "text": (str, type(None)),
            "content_truncated_for_persist": (bool, type(None)),
            "interpretation": (dict, type(None)),
            "diagram_analysis": (dict, type(None)),
            "persisted": (bool, type(None)),
            "persisted_relations": (list, type(None)),
            "persisted_structural_relations": (list, type(None)),
            "persist_errors": (list, type(None)),
            "diagnostics": (dict, type(None)),
            "namespace": (str, type(None)),
            "read_result": (dict, type(None)),
            "image_fetch_error": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "interpret_file_copy output: interpretation summary and persistence status "
            "for a blob-backed file-copy concept."
        ),
    )


def _import_local_file_copy_input_schema() -> Schema:
    return Schema(
        required={
            "local_path": str,
        },
        optional={
            "namespace": (str, type(None)),
            "type_concept_id": (str, type(None)),
            "source_system": (str, type(None)),
            "source_identifier": (str, type(None)),
            "source_uri": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "import_local_file_copy input: local_path (workspace file path) plus namespace "
            "user@org context; optional file-copy type and provenance source fields."
        ),
    )


def _import_local_file_copy_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "success": (bool, type(None)),
            "error": (str, type(None)),
            "message": (str, type(None)),
            "concept_id": (str, type(None)),
            "type_concept_id": (str, type(None)),
            "uploaded_at": (str, type(None)),
            "local_path": (str, type(None)),
            "storage": (dict, type(None)),
            "artifact_record": (dict, type(None)),
            "namespace": (str, type(None)),
            "allowed_root": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "import_local_file_copy output: blob-store registration result with created "
            "#V#computer_file_copy concept and canonical artifact_record."
        ),
    )


def _list_recent_screenshots_input_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "limit": (int, type(None)),
            "lookback_hours": (int, float, type(None)),
            "paths": (list, type(None)),
            "match_clipboard": (bool, type(None)),
            "include_base64": (bool, type(None)),
            "max_base64_bytes": (int, type(None)),
            "max_scan_files": (int, type(None)),
            # Accepted for LLM consistency; ignored by handler logic.
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "list_recent_screenshots input: optionally configure limit/lookback_hours, "
            "custom paths, clipboard matching, and base64 inclusion for attachment workflows."
        ),
    )


def _list_recent_screenshots_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
        },
        optional={
            "items": (list, type(None)),
            "count": (int, type(None)),
            "scan_summary": (dict, type(None)),
            "clipboard": (dict, type(None)),
            "error": (str, type(None)),
            "error_code": (str, type(None)),
            "error_details": (dict, type(None)),
            "suggestions": (list, type(None)),
        },
        allow_unknown=True,
        description=(
            "list_recent_screenshots output: recent screenshot candidates with metadata, "
            "optional base64 payloads, and clipboard match diagnostics."
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


def _search_proxy_diagnostics_input_schema() -> Schema:
    return Schema(
        required={},
        optional={"include_health_check": (bool,)},
        allow_unknown=True,
        description="search_proxy_diagnostics input: include_health_check (bool, default false, runs a test search to verify Tavily connectivity)",
    )


def _search_proxy_diagnostics_output_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "stats": (dict, type(None)),
            "last_call": (dict, type(None)),
            "recent_calls": (list, type(None)),
            "config": (dict, type(None)),
            "health_check": (dict, type(None)),
            "error": (str, type(None)),
            "details": (dict, type(None)),
            "proxy_initialised": (bool, type(None)),
        },
        allow_unknown=True,
        description=(
            "search_proxy_diagnostics output: stats (call_count, error_count, success_rate, avg_duration), "
            "last_call (telemetry for most recent call), recent_calls (list of recent call telemetry), "
            "config (proxy configuration), health_check (if requested: healthy, latency_ms, etc.)"
        ),
    )


def _linkedin_list_exports_input_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "refresh": (bool, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description="linkedin_list_exports input: optional refresh (bool) and namespace (accepted for orchestrator consistency).",
    )


def _linkedin_list_files_input_schema() -> Schema:
    return Schema(
        required={"export_name": str},
        optional={"namespace": (str, type(None))},
        allow_unknown=True,
        description="linkedin_list_files input: export_name (str, required), namespace (optional, ignored).",
    )


def _linkedin_get_profile_input_schema() -> Schema:
    return Schema(
        required={"export_name": str},
        optional={"namespace": (str, type(None))},
        allow_unknown=True,
        description="linkedin_get_profile input: export_name (str, required), namespace (optional, ignored).",
    )


def _linkedin_get_csv_data_input_schema() -> Schema:
    return Schema(
        required={"export_name": str, "file_name": str},
        optional={
            "limit": (int, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description="linkedin_get_csv_data input: export_name (str), file_name (str), optional limit (int, default 10), namespace (optional, ignored).",
    )


def _linkedin_get_company_stats_input_schema() -> Schema:
    return Schema(
        required={"export_name": str},
        optional={
            "top_n": (int, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description="linkedin_get_company_stats input: export_name (str), optional top_n (int, default 10), namespace (optional, ignored).",
    )


def _linkedin_get_messages_input_schema() -> Schema:
    return Schema(
        required={"export_name": str},
        optional={
            "query": (str, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description="linkedin_get_messages input: export_name (str), optional query (str), namespace (optional, ignored).",
    )


def _linkedin_generic_output_schema(
    *,
    description: str,
    optional_fields: Mapping[str, Any] | None = None,
) -> Schema:
    optional: dict[str, Any] = {
        "success": (bool, type(None)),
        "error": (str, type(None)),
        "error_code": (str, type(None)),
    }
    if isinstance(optional_fields, Mapping):
        optional.update(dict(optional_fields))
    return Schema(
        required={},
        optional=optional,
        allow_unknown=True,
        description=description,
    )


def _linkedin_list_exports_output_schema() -> Schema:
    return _linkedin_generic_output_schema(
        description="linkedin_list_exports output: exports list, data_root metadata, or error details.",
        optional_fields={
            "exports": (list, type(None)),
            "total_exports": (int, type(None)),
            "data_root": (str, type(None)),
            "data_root_exists": (bool, type(None)),
        },
    )


def _linkedin_list_files_output_schema() -> Schema:
    return _linkedin_generic_output_schema(
        description="linkedin_list_files output: files list for a chosen export, or error details.",
        optional_fields={
            "export_name": (str, type(None)),
            "files": (list, type(None)),
        },
    )


def _linkedin_get_profile_output_schema() -> Schema:
    return _linkedin_generic_output_schema(
        description="linkedin_get_profile output: profile text for an export, or error details.",
        optional_fields={
            "export_name": (str, type(None)),
            "profile": (str, type(None)),
        },
    )


def _linkedin_get_csv_data_output_schema() -> Schema:
    return _linkedin_generic_output_schema(
        description="linkedin_get_csv_data output: sampled CSV text content, or error details.",
        optional_fields={
            "export_name": (str, type(None)),
            "file_name": (str, type(None)),
            "limit": (int, type(None)),
            "data": (str, type(None)),
        },
    )


def _linkedin_get_company_stats_output_schema() -> Schema:
    return _linkedin_generic_output_schema(
        description="linkedin_get_company_stats output: company frequency mapping, or error details.",
        optional_fields={
            "export_name": (str, type(None)),
            "top_n": (int, type(None)),
            "company_stats": (dict, type(None)),
        },
    )


def _linkedin_get_messages_output_schema() -> Schema:
    return _linkedin_generic_output_schema(
        description="linkedin_get_messages output: message sample text (optionally filtered), or error details.",
        optional_fields={
            "export_name": (str, type(None)),
            "query": (str, type(None)),
            "messages": (str, type(None)),
        },
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


def _rename_concept_input_schema() -> Schema:
    return Schema(
        required={"new_id": str},
        optional={
            "old_id": (str,),
            "concept_id": (str,),
            "simulate": (bool,),
            "preserve_alias": (bool,),
        },
        allow_unknown=True,
        description=(
            "rename_concept input: new_id (required, new #V#... concept_id), "
            "old_id or concept_id (current concept_id), simulate (bool, default true), "
            "preserve_alias (bool, default true - register old ID as CODE alias)"
        ),
    )


def _rename_concept_output_schema() -> Schema:
    return Schema(
        required={"success": bool},
        optional={
            "simulate": (bool,),
            "old_id": (str,),
            "new_id": (str,),
            "operations": (list,),
            "warnings": (list,),
            "errors": (list,),
            "executed": (bool,),
        },
        allow_unknown=True,
        description="rename_concept output: reports planned or executed operations",
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
    import re

    def _normalise_concept_id(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.startswith("#v#"):
            return "#V#" + cleaned[3:]
        if cleaned.startswith("#V#"):
            return cleaned
        return f"#V#{cleaned.lstrip('#')}"

    def _namespace_slug_from_concept(value: str | None) -> str | None:
        if not isinstance(value, str):
            return None
        slug = value.strip()
        if not slug:
            return None
        if slug.startswith("#V#"):
            slug = slug[3:]
        if "@" in slug:
            slug = slug.split("@", 1)[0]
        if "+" in slug:
            slug = slug.split("+", 1)[0]
        slug = re.sub(r"[^a-z0-9]+", "_", slug.strip().lower()).strip("_")
        return slug or None

    raw_namespace = kwargs.get("namespace")
    explicit_namespace: str | None = None
    if isinstance(raw_namespace, str) and raw_namespace.strip():
        explicit_namespace = raw_namespace.strip()
        if explicit_namespace.startswith("#v#"):
            explicit_namespace = "#V#" + explicit_namespace[3:]
        elif not explicit_namespace.startswith("#V#"):
            explicit_namespace = f"#V#{explicit_namespace.lstrip('#')}"

    user_candidate = _normalise_concept_id(kwargs.get("user_concept_id"))
    if user_candidate is None:
        user_candidate = _normalise_concept_id(kwargs.get("user_id"))
    if user_candidate is None:
        user_candidate = _normalise_concept_id(kwargs.get("actor_concept_id"))
    if user_candidate is None:
        user_candidate = _normalise_concept_id(kwargs.get("created_by_concept_id"))
    if user_candidate is None:
        user_payload = kwargs.get("user")
        if isinstance(user_payload, dict):
            user_candidate = _normalise_concept_id(user_payload.get("id"))

    org_candidate = _normalise_concept_id(kwargs.get("organisation_concept_id"))
    if org_candidate is None:
        org_candidate = _normalise_concept_id(kwargs.get("org_id"))

    def _derive_namespace_from_components(
        user_component: str | None, organisation_component: str | None
    ) -> str | None:
        if user_component is None:
            return None
        user_slug = _namespace_slug_from_concept(user_component)
        org_slug = _namespace_slug_from_concept(organisation_component)
        if not user_slug:
            return None
        try:
            from ...services.namespace_service import derive_namespace

            return (
                derive_namespace(user_slug, org_slug)
                if org_slug
                else derive_namespace(user_slug)
            )
        except Exception:
            return None

    # If caller provides only namespace, derive component IDs for consistent
    # provenance and cross-flow telemetry fields.
    if explicit_namespace and (user_candidate is None or org_candidate is None):
        try:
            from ...services.namespace_service import parse_namespace

            parsed = parse_namespace(explicit_namespace)
            parsed_user = parsed.get("user_id")
            parsed_org = parsed.get("org_id")
            if user_candidate is None and isinstance(parsed_user, str):
                user_candidate = _normalise_concept_id(parsed_user)
            if org_candidate is None and isinstance(parsed_org, str):
                org_candidate = _normalise_concept_id(parsed_org)
        except Exception:
            pass

    derived_namespace: str | None = _derive_namespace_from_components(
        user_candidate, org_candidate
    )

    def _finalise_report(report: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(report)
        enriched["derived_user_concept_id"] = user_candidate
        enriched["derived_organisation_concept_id"] = org_candidate
        # Canonical aliases used by cross-flow telemetry/reporting.
        enriched["user_concept_id"] = user_candidate
        enriched["organisation_concept_id"] = org_candidate

        try:
            from ...services.namespace_isolation_diagnostics_service import (
                record_namespace_context_observation,
            )

            record_namespace_context_observation(
                flow="internal_mcp.rag.namespace_resolver",
                namespace=enriched.get("namespace"),
                namespace_source=enriched.get("namespace_source"),
                user_concept_id=user_candidate,
                organisation_concept_id=org_candidate,
                mismatch_detected=bool(enriched.get("namespace_mismatch")),
                details={
                    "namespace_resolution_note": enriched.get(
                        "namespace_resolution_note"
                    ),
                    "provided_namespace": enriched.get("provided_namespace"),
                    "derived_namespace": enriched.get("derived_namespace"),
                },
            )
        except Exception:
            pass

        if bool(enriched.get("namespace_mismatch")):
            logger.warning(
                "[NAMESPACE] RAG resolver mismatch provided=%s derived=%s user=%s org=%s",
                enriched.get("provided_namespace"),
                enriched.get("derived_namespace"),
                user_candidate,
                org_candidate,
            )
        return enriched

    if explicit_namespace and derived_namespace and explicit_namespace != derived_namespace:
        return _finalise_report(
            {
                "namespace": None,
                "namespace_source": "conflict",
                "namespace_resolution_note": "namespace_mismatch",
                "namespace_mismatch": True,
                "provided_namespace": explicit_namespace,
                "derived_namespace": derived_namespace,
            }
        )

    if explicit_namespace:
        return _finalise_report(
            {
                "namespace": explicit_namespace,
                "namespace_source": "request.namespace",
                "namespace_resolution_note": None,
                "namespace_mismatch": False,
                "provided_namespace": explicit_namespace,
                "derived_namespace": derived_namespace,
            }
        )

    if derived_namespace:
        return _finalise_report(
            {
                "namespace": derived_namespace,
                "namespace_source": "derived.user_org",
                "namespace_resolution_note": "derived_from_user_org",
                "namespace_mismatch": False,
                "provided_namespace": None,
                "derived_namespace": derived_namespace,
            }
        )

    env_ns = os.environ.get("VON_DEFAULT_NAMESPACE")
    if isinstance(env_ns, str) and env_ns.strip():
        return _finalise_report(
            {
                "namespace": env_ns.strip(),
                "namespace_source": "env.VON_DEFAULT_NAMESPACE",
                "namespace_resolution_note": "fallback",
                "namespace_mismatch": False,
                "provided_namespace": None,
                "derived_namespace": derived_namespace,
            }
        )

    return _finalise_report(
        {
            "namespace": None,
            "namespace_source": "missing",
            "namespace_resolution_note": "namespace_required",
            "namespace_mismatch": False,
            "provided_namespace": None,
            "derived_namespace": derived_namespace,
        }
    )


def _rag_namespace_resolution_error(ns_report: dict[str, Any]) -> dict[str, Any] | None:
    namespace = ns_report.get("namespace")
    if isinstance(namespace, str) and namespace.strip():
        return None

    if ns_report.get("namespace_resolution_note") == "namespace_mismatch":
        return {
            "error": "namespace_mismatch",
            "message": (
                "Explicit namespace conflicts with derived user/org namespace; "
                "request was not executed."
            ),
            **ns_report,
            "success": False,
        }

    return {
        "error": "namespace_required",
        "message": "RAG access requires authenticated user context (namespace)",
        **ns_report,
        "success": False,
    }


def _with_rag_provenance(*, payload: dict, item_kind: str, source_system: str) -> dict:
    result = dict(payload)
    user_concept_id = payload.get("user_concept_id")
    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        user_concept_id = payload.get("derived_user_concept_id")
    organisation_concept_id = payload.get("organisation_concept_id")
    if not isinstance(organisation_concept_id, str) or not organisation_concept_id.strip():
        organisation_concept_id = payload.get("derived_organisation_concept_id")

    provenance = {
        "item_kind": item_kind,
        "source_system": source_system,
        "namespace": payload.get("namespace"),
        "namespace_source": payload.get("namespace_source"),
        "user_concept_id": user_concept_id,
        "organisation_concept_id": organisation_concept_id,
    }
    existing = result.get("provenance")
    if isinstance(existing, dict):
        provenance = {**existing, **provenance}
    result["provenance"] = provenance
    return result


def _classify_turn_execution_failure_mode(item: dict[str, Any]) -> str:
    """Classify a turn execution record into a reliability failure mode."""

    decision_raw = item.get("decision")
    decision = str(decision_raw).strip().lower() if decision_raw is not None else ""

    unresolved_effect_count_raw = item.get("unresolved_effect_count")
    try:
        unresolved_effect_count = int(unresolved_effect_count_raw or 0)
    except Exception:
        unresolved_effect_count = 0

    safe_to_claim_completion = bool(item.get("safe_to_claim_completion", True))

    critic_summary_raw = item.get("critic_summary")
    critic_summary: dict[str, Any]
    if isinstance(critic_summary_raw, dict):
        critic_summary = critic_summary_raw
    else:
        critic_summary = {}
    try:
        not_verified_count = int(critic_summary.get("not_verified_count") or 0)
    except Exception:
        not_verified_count = 0
    try:
        inconclusive_count = int(critic_summary.get("inconclusive_count") or 0)
    except Exception:
        inconclusive_count = 0
    try:
        error_count = int(critic_summary.get("error_count") or 0)
    except Exception:
        error_count = 0

    completion_claim_detected = bool(item.get("completion_claim_detected", False))
    completion_claim_validated = bool(item.get("completion_claim_validated", True))

    if decision == "failed":
        return "mutation_failed_or_blocked"
    if decision == "escalation_required":
        return "mutation_not_executed"
    if decision == "partial":
        if unresolved_effect_count > 0:
            return "unresolved_required_effects"
        if (not_verified_count + inconclusive_count + error_count) > 0:
            return "postcondition_inconclusive"
        return "partial_unspecified"
    if decision == "completed":
        if not safe_to_claim_completion:
            return "false_completion_gate_state"
        if unresolved_effect_count > 0:
            return "false_completion_claim"
        if (not_verified_count + inconclusive_count + error_count) > 0:
            return "false_completion_claim"
        if completion_claim_detected and not completion_claim_validated:
            return "unvalidated_completion_claim"
        return "completed_verified"
    if completion_claim_detected and not completion_claim_validated:
        return "unvalidated_completion_claim"
    return "unknown"


def _is_likely_failure_to_act(failure_mode: str) -> bool:
    return failure_mode in {
        "mutation_failed_or_blocked",
        "mutation_not_executed",
        "unresolved_required_effects",
        "postcondition_inconclusive",
        "false_completion_gate_state",
        "false_completion_claim",
        "unvalidated_completion_claim",
        "partial_unspecified",
    }


def _derive_turn_execution_failure_recommendations(
    failure_mode_counts: dict[str, int],
) -> list[str]:
    recommendations: list[str] = []
    mutation_not_executed = int(failure_mode_counts.get("mutation_not_executed", 0))
    failed_or_blocked = int(failure_mode_counts.get("mutation_failed_or_blocked", 0))
    verification_issues = (
        int(failure_mode_counts.get("postcondition_inconclusive", 0))
        + int(failure_mode_counts.get("unresolved_required_effects", 0))
    )
    false_completion = int(failure_mode_counts.get("false_completion_claim", 0))

    if mutation_not_executed > 0:
        recommendations.append(
            "Increase selector pressure for mutation-intent turns so they route through #V#conversation_turn_execution_workflow and execute write-capable tools."
        )
    if failed_or_blocked > 0:
        recommendations.append(
            "Capture and surface write-tool failure causes (blocked/permissions/tool errors) and attach deterministic recovery steps."
        )
    if verification_issues > 0:
        recommendations.append(
            "Strengthen postcondition checks to require state re-query verification before completion is allowed."
        )
    if false_completion > 0:
        recommendations.append(
            "Tighten completion-gate invariants so completed decisions are impossible while unresolved effects or unverified checks remain."
        )
    if not recommendations:
        recommendations.append(
            "No dominant failure pattern detected in the sampled window. Expand filters or time range to gather more evidence."
        )
    return recommendations


def _turn_execution_failure_mode_priority(failure_mode: str) -> int:
    order = {
        "false_completion_claim": 0,
        "false_completion_gate_state": 1,
        "mutation_not_executed": 2,
        "mutation_failed_or_blocked": 3,
        "unresolved_required_effects": 4,
        "postcondition_inconclusive": 5,
        "unvalidated_completion_claim": 6,
        "partial_unspecified": 7,
        "completed_verified": 8,
        "unknown": 9,
    }
    return order.get(failure_mode, 99)


def _turn_execution_failure_mode_confidence(failure_mode: str) -> str:
    if failure_mode in {
        "false_completion_claim",
        "false_completion_gate_state",
        "mutation_not_executed",
        "mutation_failed_or_blocked",
    }:
        return "high"
    if failure_mode in {
        "unresolved_required_effects",
        "postcondition_inconclusive",
        "unvalidated_completion_claim",
        "partial_unspecified",
    }:
        return "medium"
    return "low"


def _turn_execution_failure_mode_expected_action(failure_mode: str) -> str:
    if failure_mode in {"mutation_not_executed", "mutation_failed_or_blocked"}:
        return (
            "Attempt and complete the required mutation tools, then verify state changes."
        )
    if failure_mode in {"unresolved_required_effects", "postcondition_inconclusive"}:
        return "Run deterministic postcondition checks and require verified satisfied status."
    if failure_mode in {"false_completion_claim", "false_completion_gate_state"}:
        return "Block completion claims until required effects and checks are fully satisfied."
    if failure_mode == "unvalidated_completion_claim":
        return "Validate completion claims against execution evidence before narrating success."
    return "Preserve deterministic execution and verification evidence for this turn."


def _turn_execution_failure_mode_observed_action(item: Mapping[str, Any]) -> str:
    decision = item.get("decision")
    decision_text = str(decision).strip() if isinstance(decision, str) else "unknown"
    unresolved = int(item.get("unresolved_effect_count") or 0)
    safe_completion = item.get("safe_to_claim_completion")
    requires_follow_up = bool(item.get("requires_follow_up"))

    return (
        f"decision={decision_text}; unresolved_effect_count={unresolved}; "
        f"safe_to_claim_completion={safe_completion}; requires_follow_up={requires_follow_up}"
    )


def _build_turn_execution_case_id(request_id: Any, index: int) -> str:
    if isinstance(request_id, str) and request_id.strip():
        cleaned_chars: list[str] = []
        for char in request_id.strip():
            if char.isalnum() or char in {"_", "-"}:
                cleaned_chars.append(char)
            else:
                cleaned_chars.append("_")
        token = "".join(cleaned_chars).strip("_")
        if token:
            return f"turn_exec_{token}"
    return f"turn_exec_case_{index:03d}"


def _normalise_turn_execution_jira_base_url(raw_value: Any) -> str:
    if isinstance(raw_value, str) and raw_value.strip():
        return raw_value.strip().rstrip("/")
    env_value = os.getenv("ATLASSIAN_BASE_URL")
    if isinstance(env_value, str) and env_value.strip():
        return env_value.strip().rstrip("/")
    return _DEFAULT_JIRA_BASE_URL


def _extract_turn_execution_jira_issue_keys_from_text(value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    keys: list[str] = []
    seen: set[str] = set()
    for match in _JIRA_ISSUE_KEY_PATTERN.finditer(value):
        project = str(match.group(1)).upper()
        issue_number = str(match.group(2))
        issue_key = f"{project}-{issue_number}"
        if issue_key in seen:
            continue
        seen.add(issue_key)
        keys.append(issue_key)
    return keys


def _extract_turn_execution_jira_issue_keys(item: Mapping[str, Any]) -> list[str]:
    candidate_values = [
        item.get("prompt_preview"),
        item.get("decision_reason"),
    ]
    keys: list[str] = []
    seen: set[str] = set()
    for candidate in candidate_values:
        for issue_key in _extract_turn_execution_jira_issue_keys_from_text(candidate):
            if issue_key in seen:
                continue
            seen.add(issue_key)
            keys.append(issue_key)
    return keys


def _build_turn_execution_case_triage(
    item: Mapping[str, Any],
    *,
    jira_base_url: str,
) -> dict[str, Any]:
    issue_keys = _extract_turn_execution_jira_issue_keys(item)
    browse_urls = [f"{jira_base_url}/browse/{issue_key}" for issue_key in issue_keys]
    request_id = (
        str(item.get("request_id")).strip()
        if isinstance(item.get("request_id"), str)
        else None
    )
    suggested_jql = (
        f'project = JVNAUTOSCI AND text ~ "\\"{request_id}\\""'
        if request_id
        else None
    )
    return {
        "jira_issue_keys": issue_keys,
        "jira_browse_urls": browse_urls,
        "suggested_jql": suggested_jql,
    }


def _build_turn_execution_replay_case(
    item: Mapping[str, Any],
    *,
    index: int,
    jira_base_url: str,
) -> dict[str, Any]:
    failure_mode = (
        str(item.get("failure_mode")).strip()
        if isinstance(item.get("failure_mode"), str)
        else "unknown"
    )
    request_id = item.get("request_id")
    return {
        "case_id": _build_turn_execution_case_id(request_id, index),
        "request_id": request_id,
        "session_id": item.get("chat_session_id"),
        "created_at_utc": item.get("created_at_utc"),
        "workflow_id": item.get("selected_workflow_id"),
        "failure_mode": failure_mode,
        "confidence": _turn_execution_failure_mode_confidence(failure_mode),
        "expected_action": _turn_execution_failure_mode_expected_action(failure_mode),
        "observed_action": _turn_execution_failure_mode_observed_action(item),
        "pass_criteria": {
            "action_attempted": True,
            "postcondition_satisfied": True,
            "no_false_success": True,
        },
        "evidence": {
            "decision": item.get("decision"),
            "decision_reason": item.get("decision_reason"),
            "requires_follow_up": item.get("requires_follow_up"),
            "safe_to_claim_completion": item.get("safe_to_claim_completion"),
            "unresolved_effect_count": item.get("unresolved_effect_count"),
            "blocking_effect_ids": (
                item.get("blocking_effect_ids")
                if isinstance(item.get("blocking_effect_ids"), list)
                else []
            ),
            "prompt_preview": item.get("prompt_preview"),
        },
        "triage": _build_turn_execution_case_triage(
            item,
            jira_base_url=jira_base_url,
        ),
    }


def _build_turn_execution_benchmark_fingerprint(
    *,
    filters: Mapping[str, Any],
    replay_cases: Sequence[Mapping[str, Any]],
) -> str:
    import hashlib
    import json

    canonical_payload = {
        "filters": dict(filters),
        "replay_cases": [
            {
                "case_id": case.get("case_id"),
                "request_id": case.get("request_id"),
                "failure_mode": case.get("failure_mode"),
                "workflow_id": case.get("workflow_id"),
            }
            for case in replay_cases
        ],
    }
    encoded = json.dumps(
        canonical_payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _build_turn_execution_triage_index(
    replay_cases: Sequence[Mapping[str, Any]],
    *,
    jira_base_url: str,
) -> dict[str, Any]:
    links_by_issue: dict[str, dict[str, Any]] = {}
    request_ids_without_issue_keys: list[str] = []

    for case in replay_cases:
        triage_raw = case.get("triage")
        triage: Mapping[str, Any] = triage_raw if isinstance(triage_raw, Mapping) else {}
        issue_keys_raw_value = triage.get("jira_issue_keys")
        issue_keys_raw = issue_keys_raw_value if isinstance(issue_keys_raw_value, list) else []
        issue_keys = [
            key.strip()
            for key in issue_keys_raw
            if isinstance(key, str) and key.strip()
        ]
        request_id = (
            str(case.get("request_id")).strip()
            if isinstance(case.get("request_id"), str)
            else ""
        )
        case_id = (
            str(case.get("case_id")).strip()
            if isinstance(case.get("case_id"), str)
            else ""
        )

        if not issue_keys and request_id:
            request_ids_without_issue_keys.append(request_id)

        for issue_key in issue_keys:
            entry = links_by_issue.setdefault(
                issue_key,
                {
                    "issue_key": issue_key,
                    "browse_url": f"{jira_base_url}/browse/{issue_key}",
                    "case_ids": [],
                    "request_ids": [],
                },
            )
            if case_id and case_id not in entry["case_ids"]:
                entry["case_ids"].append(case_id)
            if request_id and request_id not in entry["request_ids"]:
                entry["request_ids"].append(request_id)

    issue_links = sorted(
        links_by_issue.values(),
        key=lambda row: str(row.get("issue_key") or ""),
    )
    unique_request_ids_without_issue_keys = list(dict.fromkeys(request_ids_without_issue_keys))
    return {
        "jira_base_url": jira_base_url,
        "issue_link_count": len(issue_links),
        "issue_links": issue_links,
        "request_ids_without_issue_keys": unique_request_ids_without_issue_keys,
        "request_ids_without_issue_keys_count": len(
            unique_request_ids_without_issue_keys
        ),
    }


def _safe_float_or_none(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _build_turn_execution_regression_assessment(
    *,
    metrics: Mapping[str, Any],
    baseline_likely_failure_rate_pct: Any,
    baseline_false_success_rate_pct: Any,
    baseline_unresolved_follow_up_rate_pct: Any,
    regression_tolerance_pct: Any,
) -> dict[str, Any]:
    tolerance = _safe_float_or_none(regression_tolerance_pct)
    if tolerance is None or tolerance < 0:
        tolerance = 0.0

    comparisons: list[dict[str, Any]] = []
    regression_detected = False

    def _add_comparison(
        *,
        metric_name: str,
        current_key: str,
        baseline_value_raw: Any,
    ) -> None:
        nonlocal regression_detected
        baseline_value = _safe_float_or_none(baseline_value_raw)
        current_value = _safe_float_or_none(metrics.get(current_key))
        if baseline_value is None or current_value is None:
            return
        delta_pct = round(current_value - baseline_value, 2)
        regressed = delta_pct > tolerance
        if regressed:
            regression_detected = True
        comparisons.append(
            {
                "metric": metric_name,
                "baseline_pct": round(baseline_value, 2),
                "current_pct": round(current_value, 2),
                "delta_pct": delta_pct,
                "regressed": regressed,
            }
        )

    _add_comparison(
        metric_name="likely_failure_rate_pct",
        current_key="likely_failure_rate_pct",
        baseline_value_raw=baseline_likely_failure_rate_pct,
    )
    _add_comparison(
        metric_name="false_success_rate_pct",
        current_key="false_success_rate_pct",
        baseline_value_raw=baseline_false_success_rate_pct,
    )
    _add_comparison(
        metric_name="unresolved_follow_up_rate_pct",
        current_key="unresolved_follow_up_rate_pct",
        baseline_value_raw=baseline_unresolved_follow_up_rate_pct,
    )

    return {
        "baseline_provided": len(comparisons) > 0,
        "regression_tolerance_pct": round(tolerance, 2),
        "regression_detected": regression_detected,
        "comparisons": comparisons,
    }


def _derive_turn_execution_capability_gaps(
    items: Sequence[Mapping[str, Any]],
    *,
    failure_mode_counts: Mapping[str, int],
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []

    if not items:
        gaps.append(
            {
                "gap_id": "no_turn_execution_records",
                "title": "No turn execution records found in selected window",
                "evidence_count": 0,
                "severity": "high",
                "description": (
                    "Benchmark cannot evaluate failure-to-act rates because no "
                    "turn_execution_records are available for the selected namespace "
                    "and filters."
                ),
            }
        )
        return gaps

    missing_request_id_count = 0
    missing_workflow_selection_count = 0
    missing_prompt_preview_count = 0
    for item in items:
        request_id = item.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            missing_request_id_count += 1
        selected_workflow_id = item.get("selected_workflow_id")
        if not isinstance(selected_workflow_id, str) or not selected_workflow_id.strip():
            missing_workflow_selection_count += 1
        prompt_preview = item.get("prompt_preview")
        if not isinstance(prompt_preview, str) or not prompt_preview.strip():
            missing_prompt_preview_count += 1

    if missing_request_id_count > 0:
        gaps.append(
            {
                "gap_id": "missing_request_id",
                "title": "Turn execution records missing request_id",
                "evidence_count": missing_request_id_count,
                "severity": "high",
                "description": (
                    "Some turn execution records cannot be joined to replay cases because "
                    "request_id is absent."
                ),
            }
        )
    if missing_workflow_selection_count > 0:
        gaps.append(
            {
                "gap_id": "missing_workflow_selection",
                "title": "Turn execution records missing selected_workflow_id",
                "evidence_count": missing_workflow_selection_count,
                "severity": "medium",
                "description": (
                    "Workflow routing metadata is incomplete for part of the corpus, "
                    "which weakens per-workflow reliability breakdowns."
                ),
            }
        )
    if missing_prompt_preview_count > 0:
        gaps.append(
            {
                "gap_id": "missing_prompt_preview",
                "title": "Turn execution records missing prompt preview",
                "evidence_count": missing_prompt_preview_count,
                "severity": "low",
                "description": (
                    "Prompt previews are absent for some records, reducing triage readability."
                ),
            }
        )

    false_success_evidence = int(
        failure_mode_counts.get("false_completion_claim", 0)
    ) + int(failure_mode_counts.get("false_completion_gate_state", 0))
    if false_success_evidence > 0:
        gaps.append(
            {
                "gap_id": "false_success_regressions",
                "title": "False-success completion signals still present",
                "evidence_count": false_success_evidence,
                "severity": "high",
                "description": (
                    "At least one turn indicates completion was claimed while execution "
                    "evidence remained unresolved."
                ),
            }
        )

    unknown_failures = int(failure_mode_counts.get("unknown", 0))
    if unknown_failures > 0:
        gaps.append(
            {
                "gap_id": "unknown_failure_mode",
                "title": "Unclassifiable turn execution outcomes",
                "evidence_count": unknown_failures,
                "severity": "medium",
                "description": (
                    "Some records do not fit current failure-mode taxonomy and should "
                    "be reviewed for schema or classifier extension."
                ),
            }
        )

    return gaps


def _format_turn_execution_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100.0, 2)


def _build_turn_execution_benchmark_signals(
    *,
    metrics: Mapping[str, Any],
    baseline_unresolved_follow_up_rate_pct: Any,
    regression_tolerance_pct: Any,
    regression_assessment: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    def _signal_status(passed: bool | None) -> str:
        if passed is True:
            return "pass"
        if passed is False:
            return "fail"
        return "not_evaluated"

    def _add_signal(
        *,
        signal_id: str,
        dimension: str,
        title: str,
        passed: bool | None,
        details: Mapping[str, Any],
    ) -> None:
        signals.append(
            {
                "signal_id": signal_id,
                "dimension": dimension,
                "title": title,
                "status": _signal_status(passed),
                "passed": passed,
                "details": dict(details),
            }
        )

    selection_metrics_raw = metrics.get("selection_metrics")
    selection_metrics = (
        selection_metrics_raw if isinstance(selection_metrics_raw, Mapping) else {}
    )
    gate_metrics_raw = metrics.get("gate_metrics")
    gate_metrics = gate_metrics_raw if isinstance(gate_metrics_raw, Mapping) else {}
    retry_metrics_raw = metrics.get("retry_metrics")
    retry_metrics = retry_metrics_raw if isinstance(retry_metrics_raw, Mapping) else {}
    user_metrics_raw = metrics.get("user_imposition_metrics")
    user_metrics = user_metrics_raw if isinstance(user_metrics_raw, Mapping) else {}

    signals: list[dict[str, Any]] = []

    likely_failure_plain_response_count = int(
        selection_metrics.get("likely_failure_plain_response_count") or 0
    )
    _add_signal(
        signal_id="workflow_selection_prefers_tool_path",
        dimension="workflow_selection",
        title="Likely-failure turns avoid plain-response workflows",
        passed=likely_failure_plain_response_count == 0,
        details={
            "observed_plain_response_count": likely_failure_plain_response_count,
            "expected_plain_response_count": 0,
        },
    )

    false_success_count = int(gate_metrics.get("false_success_count") or 0)
    _add_signal(
        signal_id="completion_gate_false_success_guard",
        dimension="gate_outcomes",
        title="Completion gate reports zero false-success outcomes",
        passed=false_success_count == 0,
        details={
            "observed_false_success_count": false_success_count,
            "expected_false_success_count": 0,
        },
    )

    follow_up_count = int(retry_metrics.get("follow_up_count") or 0)
    follow_up_with_retry_signal_count = int(
        retry_metrics.get("follow_up_with_retry_signal_count") or 0
    )
    retry_guard_passed = (
        True
        if follow_up_count <= 0
        else follow_up_with_retry_signal_count >= follow_up_count
    )
    _add_signal(
        signal_id="retry_guardrail_signals_recorded",
        dimension="retries",
        title="Follow-up turns carry retry/stop telemetry signals",
        passed=retry_guard_passed,
        details={
            "follow_up_count": follow_up_count,
            "follow_up_with_retry_signal_count": follow_up_with_retry_signal_count,
        },
    )

    follow_up_rate_pct = _safe_float_or_none(user_metrics.get("follow_up_turn_rate_pct"))
    baseline_follow_up_rate_pct = _safe_float_or_none(
        baseline_unresolved_follow_up_rate_pct
    )
    tolerance = _safe_float_or_none(regression_tolerance_pct)
    if tolerance is None or tolerance < 0:
        tolerance = 0.0
    if follow_up_rate_pct is None or baseline_follow_up_rate_pct is None:
        user_imposition_passed = None
        max_follow_up_rate_pct = None
    else:
        max_follow_up_rate_pct = round(baseline_follow_up_rate_pct + tolerance, 2)
        user_imposition_passed = follow_up_rate_pct <= max_follow_up_rate_pct
    _add_signal(
        signal_id="user_imposition_rate_vs_baseline",
        dimension="user_imposition",
        title="Follow-up burden does not exceed baseline tolerance",
        passed=user_imposition_passed,
        details={
            "observed_follow_up_turn_rate_pct": follow_up_rate_pct,
            "max_follow_up_turn_rate_pct": max_follow_up_rate_pct,
            "baseline_follow_up_turn_rate_pct": baseline_follow_up_rate_pct,
            "regression_tolerance_pct": round(tolerance, 2),
        },
    )

    baseline_provided = bool(
        regression_assessment.get("baseline_provided", False)
        if isinstance(regression_assessment, Mapping)
        else False
    )
    if baseline_provided:
        regression_detected = bool(
            regression_assessment.get("regression_detected", False)
            if isinstance(regression_assessment, Mapping)
            else False
        )
        _add_signal(
            signal_id="overall_regression_assessment",
            dimension="benchmark",
            title="Overall benchmark metrics stay within baseline tolerance",
            passed=not regression_detected,
            details={"regression_detected": regression_detected},
        )

    summary = {
        "pass_count": sum(1 for signal in signals if signal.get("status") == "pass"),
        "fail_count": sum(1 for signal in signals if signal.get("status") == "fail"),
        "not_evaluated_count": sum(
            1 for signal in signals if signal.get("status") == "not_evaluated"
        ),
        "total_count": len(signals),
    }
    return signals, summary


def _turn_execution_build_benchmark(**kwargs):
    include_completed = bool(kwargs.get("include_completed", True))
    max_cases_raw = kwargs.get("max_cases")
    max_cases = 25
    if isinstance(max_cases_raw, int):
        max_cases = max(1, min(200, max_cases_raw))

    forwarded = dict(kwargs)
    forwarded["include_completed"] = include_completed
    result = _turn_execution_search_failures(**forwarded)
    if not isinstance(result, dict):
        return result
    if not result.get("success", False):
        return result

    raw_items = result.get("items")
    items = [dict(item) for item in raw_items] if isinstance(raw_items, list) else []
    sorted_items = sorted(
        items,
        key=lambda item: (
            _turn_execution_failure_mode_priority(
                str(item.get("failure_mode"))
                if isinstance(item.get("failure_mode"), str)
                else "unknown"
            ),
            str(item.get("request_id") or ""),
            str(item.get("created_at_utc") or ""),
        ),
    )

    likely_items = [
        item for item in sorted_items if bool(item.get("likely_failure_to_act", False))
    ]
    selected_cases = likely_items[:max_cases]

    jira_base_url = _normalise_turn_execution_jira_base_url(kwargs.get("jira_base_url"))

    replay_cases: list[dict[str, Any]] = []
    for idx, item in enumerate(selected_cases, start=1):
        replay_cases.append(
            _build_turn_execution_replay_case(
                item,
                index=idx,
                jira_base_url=jira_base_url,
            )
        )

    workflow_counts: dict[str, int] = {}
    workflow_failure_counts: dict[str, int] = {}
    for item in sorted_items:
        workflow_id = item.get("selected_workflow_id")
        workflow_key = (
            workflow_id.strip()
            if isinstance(workflow_id, str) and workflow_id.strip()
            else "unknown"
        )
        workflow_counts[workflow_key] = workflow_counts.get(workflow_key, 0) + 1
        if bool(item.get("likely_failure_to_act", False)):
            workflow_failure_counts[workflow_key] = (
                workflow_failure_counts.get(workflow_key, 0) + 1
            )

    scanned_count = len(sorted_items)
    likely_failure_count = len(likely_items)
    false_success_count = sum(
        1
        for item in sorted_items
        if item.get("failure_mode")
        in {"false_completion_claim", "false_completion_gate_state"}
    )
    unresolved_follow_up_count = sum(
        1 for item in sorted_items if bool(item.get("requires_follow_up", False))
    )
    safe_completion_count = sum(
        1 for item in sorted_items if bool(item.get("safe_to_claim_completion", False))
    )
    likely_failure_plain_response_count = sum(
        1
        for item in likely_items
        if str(item.get("selected_workflow_id") or "").strip()
        in {"#V#chat_assistant_workflow", "#V#plain_response_workflow"}
    )
    likely_failure_tool_workflow_count = sum(
        1
        for item in likely_items
        if str(item.get("selected_workflow_id") or "").strip()
        == "#V#tool_calling_workflow"
    )
    bounded_loop_stop_reasons = {
        "attempt_budget_exhausted",
        "elapsed_budget_exhausted",
        "no_progress_guard_triggered",
        "stall_latency_budget_exhausted",
    }
    follow_up_with_retry_signal_count = 0
    bounded_retry_stop_count = 0
    stall_latency_stop_count = 0
    escalation_signal_count = 0
    for item in sorted_items:
        if not bool(item.get("requires_follow_up", False)):
            continue
        loop_attempts = _coerce_int_or_none(item.get("loop_attempts")) or 0
        repeat_iteration = bool(item.get("repeat_iteration", False))
        loop_stop_reason = (
            str(item.get("loop_stop_reason")).strip()
            if isinstance(item.get("loop_stop_reason"), str)
            else ""
        )
        if repeat_iteration or loop_attempts > 0 or bool(loop_stop_reason):
            follow_up_with_retry_signal_count += 1
        if loop_stop_reason in bounded_loop_stop_reasons:
            bounded_retry_stop_count += 1
        if loop_stop_reason == "stall_latency_budget_exhausted":
            stall_latency_stop_count += 1
        if bool(item.get("escalation_signal", False)):
            escalation_signal_count += 1

    failure_mode_counts: dict[str, int] = {}
    failure_mode_counts_raw = result.get("failure_mode_counts")
    if isinstance(failure_mode_counts_raw, dict):
        for key, value in failure_mode_counts_raw.items():
            key_text = str(key).strip() or "unknown"
            try:
                failure_mode_counts[key_text] = int(value)
            except Exception:
                failure_mode_counts[key_text] = 0
    filters_payload = {
        "namespace": kwargs.get("namespace"),
        "limit": kwargs.get("limit"),
        "offset": kwargs.get("offset"),
        "decision": kwargs.get("decision"),
        "decisions": kwargs.get("decisions"),
        "workflow_id": kwargs.get("workflow_id"),
        "requires_follow_up": kwargs.get("requires_follow_up"),
        "prompt_contains": kwargs.get("prompt_contains"),
        "from_utc": kwargs.get("from_utc"),
        "to_utc": kwargs.get("to_utc"),
        "include_completed": include_completed,
        "max_cases": max_cases,
        "jira_base_url": jira_base_url,
    }
    metrics_payload = {
        "scanned_count": scanned_count,
        "likely_failure_count": likely_failure_count,
        "likely_failure_rate_pct": _format_turn_execution_rate(
            likely_failure_count, scanned_count
        ),
        "false_success_count": false_success_count,
        "false_success_rate_pct": _format_turn_execution_rate(
            false_success_count, scanned_count
        ),
        "unresolved_follow_up_count": unresolved_follow_up_count,
        "unresolved_follow_up_rate_pct": _format_turn_execution_rate(
            unresolved_follow_up_count, scanned_count
        ),
        "selection_metrics": {
            "likely_failure_tool_workflow_count": likely_failure_tool_workflow_count,
            "likely_failure_plain_response_count": likely_failure_plain_response_count,
            "likely_failure_tool_workflow_rate_pct": _format_turn_execution_rate(
                likely_failure_tool_workflow_count, likely_failure_count
            ),
        },
        "gate_metrics": {
            "false_success_count": false_success_count,
            "safe_completion_count": safe_completion_count,
            "requires_follow_up_count": unresolved_follow_up_count,
            "requires_follow_up_rate_pct": _format_turn_execution_rate(
                unresolved_follow_up_count, scanned_count
            ),
        },
        "retry_metrics": {
            "follow_up_count": unresolved_follow_up_count,
            "follow_up_with_retry_signal_count": follow_up_with_retry_signal_count,
            "follow_up_with_retry_signal_rate_pct": _format_turn_execution_rate(
                follow_up_with_retry_signal_count, unresolved_follow_up_count
            ),
            "bounded_retry_stop_count": bounded_retry_stop_count,
            "bounded_retry_stop_rate_pct": _format_turn_execution_rate(
                bounded_retry_stop_count, unresolved_follow_up_count
            ),
            "stall_latency_stop_count": stall_latency_stop_count,
        },
        "user_imposition_metrics": {
            "follow_up_turn_count": unresolved_follow_up_count,
            "follow_up_turn_rate_pct": _format_turn_execution_rate(
                unresolved_follow_up_count, scanned_count
            ),
            "escalation_signal_count": escalation_signal_count,
            "escalation_signal_rate_pct": _format_turn_execution_rate(
                escalation_signal_count, unresolved_follow_up_count
            ),
        },
        "failure_mode_counts": failure_mode_counts,
        "decision_counts": (
            result.get("decision_counts")
            if isinstance(result.get("decision_counts"), dict)
            else {}
        ),
        "workflow_counts": workflow_counts,
        "workflow_failure_counts": workflow_failure_counts,
    }
    regression_assessment = _build_turn_execution_regression_assessment(
        metrics=metrics_payload,
        baseline_likely_failure_rate_pct=kwargs.get("baseline_likely_failure_rate_pct"),
        baseline_false_success_rate_pct=kwargs.get("baseline_false_success_rate_pct"),
        baseline_unresolved_follow_up_rate_pct=kwargs.get(
            "baseline_unresolved_follow_up_rate_pct"
        ),
        regression_tolerance_pct=kwargs.get("regression_tolerance_pct"),
    )
    benchmark_signals, benchmark_signal_summary = (
        _build_turn_execution_benchmark_signals(
            metrics=metrics_payload,
            baseline_unresolved_follow_up_rate_pct=kwargs.get(
                "baseline_unresolved_follow_up_rate_pct"
            ),
            regression_tolerance_pct=kwargs.get("regression_tolerance_pct"),
            regression_assessment=regression_assessment,
        )
    )
    payload = {
        "collection": "turn_execution_records",
        "benchmark_generated_at_utc": _utc_now_iso(),
        "filters": filters_payload,
        "metrics": metrics_payload,
        "benchmark_fingerprint": _build_turn_execution_benchmark_fingerprint(
            filters=filters_payload,
            replay_cases=replay_cases,
        ),
        "seeded_cases": replay_cases,
        "replay_cases": replay_cases,
        "triage_index": _build_turn_execution_triage_index(
            replay_cases,
            jira_base_url=jira_base_url,
        ),
        "regression_assessment": regression_assessment,
        "benchmark_signals": benchmark_signals,
        "benchmark_signal_summary": benchmark_signal_summary,
        "capability_gaps": _derive_turn_execution_capability_gaps(
            sorted_items,
            failure_mode_counts=failure_mode_counts,
        ),
        "recommendations": (
            result.get("recommendations")
            if isinstance(result.get("recommendations"), list)
            else _derive_turn_execution_failure_recommendations(failure_mode_counts)
        ),
        "effective_namespace": result.get("effective_namespace"),
        "effective_namespace_source": result.get("effective_namespace_source"),
        "namespace": result.get("namespace"),
        "namespace_source": result.get("namespace_source"),
        "namespace_resolution_note": result.get("namespace_resolution_note"),
        "success": True,
    }
    return _with_rag_provenance(
        payload=payload,
        item_kind="turn_execution_benchmark_report",
        source_system="mongo.turn_execution_records",
    )


def _turn_execution_backfill_from_chat_history(**kwargs):
    from ...services.turn_execution_record_service import (
        backfill_turn_execution_records_from_chat_history,
    )

    namespace = kwargs.get("namespace")
    if not isinstance(namespace, str) or not namespace.strip():
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: namespace",
            details={"missing": ["namespace"]},
            suggestions=[
                "Provide namespace to scope backfill (for example #V#user@organisation)"
            ],
        )

    result = backfill_turn_execution_records_from_chat_history(
        namespace=namespace.strip(),
        limit_sessions=kwargs.get("limit_sessions", 500),
        dry_run=kwargs.get("dry_run", True),
        synthesise_missing_records=kwargs.get("synthesise_missing_records", True),
    )
    if not isinstance(result, dict):
        return result
    if not result.get("success", False):
        return result
    return _with_rag_provenance(
        payload=result,
        item_kind="turn_execution_backfill_report",
        source_system="mongo.turn_execution_records",
    )


def _turn_execution_namespace_coverage_report(**kwargs):
    from ...services.turn_execution_record_service import (
        build_turn_execution_namespace_coverage_report,
    )

    result = build_turn_execution_namespace_coverage_report(
        namespace=kwargs.get("namespace"),
        limit_namespaces=kwargs.get("limit_namespaces", 25),
        limit_sessions_per_namespace=kwargs.get("limit_sessions_per_namespace", 200),
        limit_projected_records_per_namespace=kwargs.get(
            "limit_projected_records_per_namespace", 10000
        ),
    )
    if not isinstance(result, dict):
        return result
    if not result.get("success", False):
        return result
    return _with_rag_provenance(
        payload=result,
        item_kind="turn_execution_namespace_coverage_report",
        source_system="mongo.turn_execution_records",
    )


def _turn_execution_list(**kwargs):
    forwarded = dict(kwargs)
    forwarded["collection"] = "turn_execution_records"
    return _rag_list_indexed(**forwarded)


def _turn_execution_get(**kwargs):
    request_id = kwargs.get("request_id")
    session_id = kwargs.get("session_id")
    target = request_id if request_id is not None else session_id
    if not isinstance(target, str) or not target.strip():
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: request_id",
            details={"missing": ["request_id"]},
            suggestions=["Provide request_id (or session_id alias)"],
        )
    forwarded = dict(kwargs)
    forwarded["collection"] = "turn_execution_records"
    forwarded["session_id"] = target.strip()
    return _rag_get_item(**forwarded)


def _turn_execution_search_failures(**kwargs):
    include_completed = bool(kwargs.get("include_completed", False))
    forwarded = dict(kwargs)
    forwarded["collection"] = "turn_execution_records"

    # Failure search defaults to follow-up-required records unless explicitly
    # overridden by caller-provided filters.
    if (
        not include_completed
        and "requires_follow_up" not in forwarded
        and "decision" not in forwarded
        and "decisions" not in forwarded
    ):
        forwarded["requires_follow_up"] = True

    result = _rag_list_indexed(**forwarded)
    if not isinstance(result, dict):
        return result
    if not result.get("success", False):
        return result

    raw_items = result.get("items")
    if not isinstance(raw_items, list):
        raw_items = []

    analysed_items: list[dict[str, Any]] = []
    failure_mode_counts: dict[str, int] = {}
    likely_failure_count = 0
    example_request_ids: list[str] = []

    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        failure_mode = _classify_turn_execution_failure_mode(item)
        likely_failure = _is_likely_failure_to_act(failure_mode)
        item["failure_mode"] = failure_mode
        item["likely_failure_to_act"] = likely_failure

        if likely_failure and isinstance(item.get("request_id"), str):
            request_id = item["request_id"].strip()
            if request_id and request_id not in example_request_ids:
                example_request_ids.append(request_id)

        if likely_failure:
            likely_failure_count += 1

        failure_mode_counts[failure_mode] = failure_mode_counts.get(failure_mode, 0) + 1
        analysed_items.append(item)

    if not include_completed:
        analysed_items = [
            item
            for item in analysed_items
            if bool(item.get("likely_failure_to_act", False))
        ]

    payload = {
        "collection": "turn_execution_records",
        "items": analysed_items,
        "returned_count": len(analysed_items),
        "total": result.get("total"),
        "limit": result.get("limit"),
        "offset": result.get("offset"),
        "decision_counts": (
            result.get("decision_counts")
            if isinstance(result.get("decision_counts"), dict)
            else {}
        ),
        "failure_mode_counts": failure_mode_counts,
        "likely_failure_count": likely_failure_count,
        "example_request_ids": example_request_ids[:10],
        "recommendations": _derive_turn_execution_failure_recommendations(
            failure_mode_counts
        ),
        "effective_namespace": result.get("effective_namespace"),
        "effective_namespace_source": result.get("effective_namespace_source"),
        "namespace": result.get("namespace"),
        "namespace_source": result.get("namespace_source"),
        "namespace_resolution_note": result.get("namespace_resolution_note"),
        "success": True,
    }
    return _with_rag_provenance(
        payload=payload,
        item_kind="turn_execution_failure_report",
        source_system="mongo.turn_execution_records",
    )


def _search_knowledge_base(**kwargs):
    from ...services.rag_service import get_rag_service, RAGBackendUnavailable

    import time

    query_text = kwargs.get("query")
    if not query_text:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: query",
            details={"missing": ["query"]},
            suggestions=["Provide a search query string"],
        )

    try:
        service = get_rag_service()  # Default backend
        ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
        ns = ns_report.get("namespace")

        # SECURITY: Require namespace for RAG search - prevents cross-user data leakage
        ns_error = _rag_namespace_resolution_error(ns_report)
        if ns_error is not None:
            if ns_error.get("error") == "namespace_required":
                ns_error["message"] = (
                    "RAG search requires authenticated user context (namespace)"
                )
            return ns_error

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
        return make_error_response(
            "rag_service_unavailable",
            f"RAG service unavailable: {e}",
            details={"exception_type": "RAGBackendUnavailable"},
            suggestions=["Ensure RAG service is running and accessible"],
        )
    except Exception as e:
        return make_error_response(
            "exception",
            f"Unexpected error: {e}",
            details={"exception_type": type(e).__name__},
        )


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
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: concept_id",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID to reindex"],
        )

    ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
    ns = ns_report.get("namespace")
    ns_error = _rag_namespace_resolution_error(ns_report)
    if ns_error is not None:
        if ns_error.get("error") == "namespace_required":
            ns_error["message"] = (
                "RAG indexing requires authenticated user context (namespace)"
            )
        return ns_error
    if not isinstance(ns, str) or not ns.strip():
        return make_error_response(
            "namespace_required",
            "RAG indexing requires authenticated user context (namespace)",
            details={"namespace_report": ns_report},
        )
    ns = ns.strip()

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


# Description generation (JVNAUTOSCI-1044)
def _generate_concept_description(**kwargs):
    """Generate a description for a concept using LLM.

    Uses the DescriptionGenerationService to create meaningful descriptions
    for concepts that lack proper hasDescription text relations.
    """
    concept_id = kwargs.get("concept_id")
    if not isinstance(concept_id, str) or not concept_id.strip():
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: concept_id",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID for which to generate a description"],
        )

    force = bool(kwargs.get("force", False))
    store = bool(kwargs.get("store", True))

    from ...services.description_generation_service import (
        generate_concept_description,
        is_placeholder_description,
    )

    result = generate_concept_description(
        concept_id=concept_id.strip(),
        force=force,
        store=store,
    )

    return result


def _generate_concept_description_input_schema() -> Schema:
    return Schema(
        required={"concept_id": str},
        optional={
            "force": (bool,),
            "store": (bool,),
            # Accept namespace for LLM consistency (ignored by handler)
            "namespace": (str, type(None)),
        },
        allow_unknown=False,
        description=(
            "generate_concept_description input: concept_id (str, required), "
            "force (bool, default false - regenerate even if description exists), "
            "store (bool, default true - persist the generated description)"
        ),
    )


def _generate_concept_description_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
            "concept_id": str,
        },
        optional={
            "description": (str, type(None)),
            "was_generated": (bool, type(None)),
            "error": (str, type(None)),
        },
        allow_unknown=False,
        description="generate_concept_description output: success, description text, was_generated flag",
    )


def _check_placeholder_description(**kwargs):
    """Check if a description text appears to be an auto-generated placeholder."""
    text = kwargs.get("text")

    from ...services.description_generation_service import is_placeholder_description

    return {
        "success": True,
        "is_placeholder": is_placeholder_description(text),
        "text_preview": (text[:100] + "...") if text and len(text) > 100 else text,
    }


def _check_placeholder_description_input_schema() -> Schema:
    return Schema(
        required={"text": (str, type(None))},
        optional={},
        allow_unknown=False,
        description="check_placeholder_description input: text (str or null) to check",
    )


def _check_placeholder_description_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
            "is_placeholder": bool,
        },
        optional={
            "text_preview": (str, type(None)),
        },
        allow_unknown=False,
        description="check_placeholder_description output: whether the text is a placeholder",
    )


def _renderer_resolve_applicability(**kwargs):
    """Resolve renderer applicability from ontology-derived metadata."""
    from ...services.renderer_applicability_service import (
        resolve_renderer_applicability_from_metadata,
    )
    from ...services.renderer_applicability_vontology_service import (
        canonical_renderer_profile_concept_ids,
        enrich_request_payload_from_concept,
        load_renderer_definitions_from_concept_ids,
    )

    renderer_definitions = kwargs.get("renderer_definitions")
    renderer_definition_concept_ids = kwargs.get("renderer_definition_concept_ids")
    request_payload = kwargs.get("request_payload")

    if not isinstance(request_payload, dict):
        return make_error_response(
            "missing_parameter",
            "request_payload is required and must be a dict",
            details={"missing": ["request_payload"]},
            suggestions=["Provide request_payload describing the object to render"],
        )
    if renderer_definitions is not None and not isinstance(renderer_definitions, list):
        return make_error_response(
            "invalid_parameter",
            "renderer_definitions must be a list when provided",
            details={"parameter": "renderer_definitions"},
            suggestions=["Provide renderer_definitions as a list of renderer metadata objects"],
        )
    if renderer_definition_concept_ids is not None and not isinstance(
        renderer_definition_concept_ids, list
    ):
        return make_error_response(
            "invalid_parameter",
            "renderer_definition_concept_ids must be a list when provided",
            details={"parameter": "renderer_definition_concept_ids"},
            suggestions=["Provide renderer_definition_concept_ids as a list of concept IDs"],
        )

    effective_renderer_definitions = list(renderer_definitions or [])
    loading_diagnostics = {"renderer_definition_source": "inline_payload_only"}
    if isinstance(renderer_definition_concept_ids, list) and renderer_definition_concept_ids:
        loaded_definitions, loading_diagnostics = load_renderer_definitions_from_concept_ids(
            renderer_definition_concept_ids
        )
        effective_renderer_definitions.extend(loaded_definitions)

    if not effective_renderer_definitions:
        canonical_profile_ids = list(canonical_renderer_profile_concept_ids())
        error_details: dict[str, Any] = {
            "missing": [
                "renderer_definitions",
                "renderer_definition_concept_ids",
            ],
            "renderer_definition_inputs": {
                "inline_count": len(renderer_definitions or []),
                "concept_id_count": len(renderer_definition_concept_ids or []),
                "effective_count": len(effective_renderer_definitions),
            },
            "renderer_definition_loading": loading_diagnostics,
        }
        if canonical_profile_ids:
            error_details["canonical_renderer_profile_concept_ids"] = canonical_profile_ids
        return make_error_response(
            "missing_parameter",
            "No renderer definitions were supplied. Provide renderer_definitions or renderer_definition_concept_ids with valid profile text.",
            details=error_details,
            suggestions=[
                "Provide renderer_definitions as inline renderer metadata",
                "Or provide renderer_definition_concept_ids where text relations contain renderer profile JSON",
                "Use upsert_renderer_profile to persist valid profile JSON for missing/malformed renderer concepts",
            ],
        )

    enriched_request_payload, request_enrichment_diagnostics = enrich_request_payload_from_concept(
        request_payload
    )

    allow_multimodal = kwargs.get("allow_multimodal", True)
    if not isinstance(allow_multimodal, bool):
        allow_multimodal = bool(allow_multimodal)

    try:
        result = resolve_renderer_applicability_from_metadata(
            renderer_definitions=effective_renderer_definitions,
            request_payload=enriched_request_payload,
            allow_multimodal=allow_multimodal,
        )
        payload = result.to_dict()
        diagnostics = payload.get("diagnostics")
        if not isinstance(diagnostics, dict):
            diagnostics = {}
        diagnostics["renderer_definition_inputs"] = {
            "inline_count": len(renderer_definitions or []),
            "concept_id_count": len(renderer_definition_concept_ids or []),
            "effective_count": len(effective_renderer_definitions),
        }
        diagnostics["renderer_definition_loading"] = loading_diagnostics
        diagnostics["request_payload_enrichment"] = request_enrichment_diagnostics
        payload["diagnostics"] = diagnostics
        return {"success": True, **payload}
    except ValueError as exc:
        return make_error_response(
            "invalid_parameter",
            str(exc),
            details={"exception_type": "ValueError"},
            suggestions=["Check renderer_definitions and request_payload field shapes"],
        )
    except Exception as exc:
        return make_error_response(
            "unexpected_error",
            f"Unexpected error while resolving renderer applicability: {exc}",
            details={"exception_type": type(exc).__name__},
        )


def _upsert_renderer_profile(**kwargs):
    """Persist renderer applicability metadata on a renderer concept."""
    from ...services.renderer_applicability_vontology_service import (
        upsert_renderer_profile,
    )

    renderer_concept_id = kwargs.get("renderer_concept_id")
    renderer_profile = kwargs.get("renderer_profile")
    predicate = kwargs.get("predicate")
    language = kwargs.get("language", "en-NZ")
    policy = kwargs.get("policy", "replace_others")
    provenance = kwargs.get("provenance")
    context = kwargs.get("context")
    garbage_collect = kwargs.get("garbage_collect")

    if not isinstance(renderer_concept_id, str) or not renderer_concept_id.strip():
        return make_error_response(
            "missing_parameter",
            "renderer_concept_id is required and must be a non-empty string",
            details={"missing": ["renderer_concept_id"]},
            suggestions=["Provide the renderer concept ID (for example #V#timeline_renderer)"],
        )
    if not isinstance(renderer_profile, dict):
        return make_error_response(
            "missing_parameter",
            "renderer_profile is required and must be a dict",
            details={"missing": ["renderer_profile"]},
            suggestions=[
                "Provide renderer_profile with modalities and applicability metadata"
            ],
        )

    try:
        return upsert_renderer_profile(
            renderer_concept_id=renderer_concept_id,
            renderer_profile=renderer_profile,
            predicate=predicate or "#V#has_renderer_profile_json",
            language=language,
            policy=policy,
            provenance=provenance if isinstance(provenance, dict) else None,
            context=context if isinstance(context, dict) else None,
            garbage_collect=(True if garbage_collect is None else bool(garbage_collect)),
        )
    except ValueError as exc:
        return make_error_response(
            "invalid_parameter",
            str(exc),
            details={"exception_type": "ValueError"},
            suggestions=[
                "Ensure renderer_profile includes at least renderer_id/modalities/object kinds",
                "Ensure renderer_concept_id references an existing concept",
            ],
        )
    except Exception as exc:
        return make_error_response(
            "unexpected_error",
            f"Unexpected error while upserting renderer profile: {exc}",
            details={"exception_type": type(exc).__name__},
        )


# =============================================================================
# Durable Workflow Instance Handlers (JVNAUTOSCI-1075)
# =============================================================================


def _workflow_list_definitions(**kwargs):
    """List available workflow definitions."""
    from ...workflows.durable.registry_factory import (
        build_durable_workflow_registry_read_only,
        get_workflow_registry_inventory_snapshot,
    )
    from ...workflows.workflow_definition_identity_service import (
        build_workflow_definition_identity,
    )
    from ...workflows.vontology_loader import (
        resolve_workflow_background_launch_policy,
        resolve_workflow_description,
    )
    from ...workflows.workflow_baseline_telemetry import (
        get_workflow_baseline_telemetry_snapshot,
    )

    limit = min(int(kwargs.get("limit", 50)), 200)

    try:
        # Diagnostics should be read-only: avoid bootstrap writes on introspection
        # pathways such as workflow_list_definitions and health checks.
        registry = build_durable_workflow_registry_read_only()
        ids = sorted(list(registry.all_workflow_ids()))

        # Enriched descriptions
        definitions = []
        for wid in ids[:limit]:
            registration = registry.get_registration(wid)
            defn = registration.definition if registration is not None else registry.get(wid)
            source = (
                str(getattr(registration, "source", "") or "").strip()
                if registration is not None
                else "unknown"
            ) or "unknown"
            description, description_source = resolve_workflow_description(
                wid,
                workflow_source=(registration.source if registration is not None else None),
                registration_purpose=(
                    registration.purpose if registration is not None else None
                ),
                definition_purpose=(getattr(defn, "purpose", "") if defn else None),
            )
            background_launch_policy = None
            background_launch_policy_source = "none"
            definition_metadata = getattr(defn, "metadata", None)
            if isinstance(definition_metadata, Mapping):
                policy_from_definition = definition_metadata.get(
                    "background_launch_policy"
                )
                if isinstance(policy_from_definition, dict):
                    background_launch_policy = dict(policy_from_definition)
                    background_launch_policy_source = str(
                        definition_metadata.get("background_launch_policy_source")
                        or "definition.metadata"
                    )
            if background_launch_policy is None:
                (
                    background_launch_policy,
                    background_launch_policy_source,
                ) = resolve_workflow_background_launch_policy(wid)
            definitions.append(
                {
                    "workflow_id": wid,
                    "description": description,
                    "description_source": description_source,
                    "initial_state": defn.initial_state if defn else "",
                    "source": source,
                    "background_launch_policy": background_launch_policy,
                    "background_launch_policy_source": background_launch_policy_source,
                    "definition_identity": build_workflow_definition_identity(
                        workflow_id=wid,
                        source=source,
                        definition=defn,
                        authoritative_definition=defn if source.lower() == "vontology" else None,
                    ),
                }
            )

        # Resolve capabilities from the authoritative internal catalogue so
        # diagnostics stay correct as tools are added/removed over time.
        capability_matrix = build_workflow_surface_capability_matrix(
            internal_method_names=build_default_catalogue().list_methods()
        )

        return {
            "success": True,
            "definitions": definitions,
            "count": len(definitions),
            "parity_inventory": get_workflow_registry_inventory_snapshot(),
            "baseline_telemetry": get_workflow_baseline_telemetry_snapshot(),
            "capability_matrix": capability_matrix,
        }
    except Exception as e:
        return make_error_response(
            "list_failed",
            f"Failed to list workflow definitions: {e}",
        )


def _workflow_mcp_health_check(**kwargs):
    """Run lightweight gateway-path health checks for core workflow MCP tools."""

    from time import perf_counter
    from .gateway import InternalMCPGateway
    from .transport import InternalMCPTransport

    include_introspection = kwargs.get("include_introspection", True)
    if not isinstance(include_introspection, bool):
        include_introspection = bool(include_introspection)

    namespace = kwargs.get("namespace")
    if not isinstance(namespace, str) or not namespace.strip():
        namespace = "#V#workflow_health_check"

    checks_to_run: list[tuple[str, dict[str, Any]]] = [
        ("workflow_list_definitions", {"limit": 5}),
        ("workflow_list_instances", {"limit": 5}),
        ("workflow_list_event_bindings", {"limit": 5}),
        ("workflow_list_schedules", {"limit": 5}),
    ]
    if include_introspection:
        checks_to_run.extend(
            [
                ("settings_get_public", {}),
                ("chat_introspect", {"namespace": namespace}),
            ]
        )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    capability_matrix = build_workflow_surface_capability_matrix(
        internal_method_names=gateway.describe_methods().keys()
    )

    checks: list[dict[str, Any]] = []
    failed_tools: list[str] = []
    for tool_name, payload in checks_to_run:
        started = perf_counter()
        try:
            result = gateway.invoke(tool_name, payload)
            response_payload = result.payload if isinstance(result.payload, dict) else {}
            ok = bool(response_payload.get("success"))
            error_code = response_payload.get("error_code")
            error = response_payload.get("error")
        except Exception as exc:
            ok = False
            error_code = "exception"
            error = str(exc)
            response_payload = {"exception_type": type(exc).__name__}

        if not ok:
            failed_tools.append(tool_name)

        checks.append(
            {
                "tool": tool_name,
                "ok": ok,
                "latency_ms": round((perf_counter() - started) * 1000.0, 2),
                "error_code": error_code,
                "error": error,
                "details": response_payload.get("error_details")
                if isinstance(response_payload, dict)
                else None,
            }
        )

    return {
        "success": len(failed_tools) == 0,
        "checked_tools": [tool_name for tool_name, _ in checks_to_run],
        "checks": checks,
        "failed_tools": failed_tools,
        "capability_matrix": capability_matrix,
    }


def _workflow_bind_event(**kwargs):
    """Create or update an event -> workflow binding."""

    from ...workflows.durable import WorkflowInstanceManager
    from ...workflows.durable.registry_factory import (
        build_durable_workflow_registry_read_only,
    )
    from ...services.workflow_event_integration_service import (
        resolve_event_actor_context,
    )

    event_type_raw = kwargs.get("event_type")
    workflow_id_raw = kwargs.get("workflow_id")
    if not isinstance(event_type_raw, str) or not event_type_raw.strip():
        return make_error_response(
            "missing_parameter",
            "event_type is required",
            details={"missing": ["event_type"]},
        )
    if not isinstance(workflow_id_raw, str) or not workflow_id_raw.strip():
        return make_error_response(
            "missing_parameter",
            "workflow_id is required",
            details={"missing": ["workflow_id"]},
        )

    event_type = event_type_raw.strip()
    workflow_id = workflow_id_raw.strip()
    input_mapping_raw = kwargs.get("input_mapping")
    enabled = kwargs.get("enabled", True)
    replace_existing = kwargs.get("replace_existing", False)
    actor = kwargs.get("actor")

    input_mapping: dict[str, str] = {}
    if isinstance(input_mapping_raw, dict):
        for key, value in input_mapping_raw.items():
            key_clean = str(key or "").strip()
            value_clean = str(value or "").strip()
            if key_clean and value_clean:
                input_mapping[key_clean] = value_clean

    if not isinstance(enabled, bool):
        enabled = bool(enabled)
    if not isinstance(replace_existing, bool):
        replace_existing = bool(replace_existing)

    actor_clean = actor.strip() if isinstance(actor, str) and actor.strip() else None
    if actor_clean is None:
        actor_id, _actor_org = resolve_event_actor_context()
        actor_clean = actor_id

    workflow_registry_known: bool | None = None
    try:
        registry = build_durable_workflow_registry_read_only()
        workflow_registry_known = workflow_id in set(registry.all_workflow_ids())
    except Exception:
        workflow_registry_known = None

    manager = WorkflowInstanceManager()
    try:
        binding, created, updated = manager.upsert_event_binding(
            event_type=event_type,
            workflow_id=workflow_id,
            input_mapping=input_mapping,
            enabled=enabled,
            actor=actor_clean,
            replace_existing=replace_existing,
        )
    except ValueError as exc:
        if str(exc) == "binding_conflict":
            return make_error_response(
                "binding_conflict",
                (
                    "Binding already exists with different configuration. "
                    "Set replace_existing=true to overwrite."
                ),
                details={
                    "event_type": event_type,
                    "workflow_id": workflow_id,
                    "replace_existing": replace_existing,
                },
            )
        return make_error_response(
            "invalid_binding",
            str(exc),
            details={"event_type": event_type, "workflow_id": workflow_id},
        )
    except Exception as exc:
        return make_error_response(
            "bind_failed",
            f"Failed to bind event to workflow: {exc}",
            details={"event_type": event_type, "workflow_id": workflow_id},
        )

    return {
        "success": True,
        "binding": binding.to_status_dict(),
        "created": created,
        "updated": updated,
        "unchanged": not created and not updated,
        "workflow_registry_known": workflow_registry_known,
    }


def _workflow_list_event_bindings(**kwargs):
    """List event -> workflow bindings (persistent + optional env fallback)."""

    from ...services.workflow_event_integration_service import list_event_workflow_bindings

    event_type = kwargs.get("event_type")
    enabled_only = kwargs.get("enabled_only", False)
    include_env_fallback = kwargs.get("include_env_fallback", True)
    try:
        limit = int(kwargs.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))

    if not isinstance(event_type, str) or not event_type.strip():
        event_type = None
    if not isinstance(enabled_only, bool):
        enabled_only = bool(enabled_only)
    if not isinstance(include_env_fallback, bool):
        include_env_fallback = bool(include_env_fallback)

    bindings = list_event_workflow_bindings(
        event_type=event_type,
        enabled_only=enabled_only,
        include_env_fallback=include_env_fallback,
        limit=limit,
    )
    return {
        "success": True,
        "bindings": bindings,
        "count": len(bindings),
    }


def _workflow_create_instance(**kwargs):
    """Create a new durable workflow instance."""
    from ...workflows.durable import WorkflowInstanceManager
    from ...workflows.durable.workflow_instance_submission_service import (
        submit_verified_workflow_instance,
    )

    workflow_id = kwargs.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        return make_error_response(
            "missing_parameter",
            "workflow_id is required",
            details={"missing": ["workflow_id"]},
        )

    user_id_raw = kwargs.get("user_id", "anonymous")
    org_id_raw = kwargs.get("org_id", "default")
    namespace_raw = kwargs.get("namespace")
    inputs_raw = kwargs.get("inputs", {})
    max_retries_raw = kwargs.get("max_retries", 3)

    user_id = (
        user_id_raw.strip()
        if isinstance(user_id_raw, str) and user_id_raw.strip()
        else "anonymous"
    )
    org_id = (
        org_id_raw.strip()
        if isinstance(org_id_raw, str) and org_id_raw.strip()
        else "default"
    )
    namespace = (
        namespace_raw.strip()
        if isinstance(namespace_raw, str) and namespace_raw.strip()
        else f"{user_id}/{org_id}"
    )
    inputs = inputs_raw if isinstance(inputs_raw, dict) else {}
    try:
        max_retries = int(max_retries_raw)
    except (TypeError, ValueError):
        max_retries = 3
    max_retries = max(0, min(max_retries, 50))

    try:
        manager = WorkflowInstanceManager()
        submission = submit_verified_workflow_instance(
            manager=manager,
            workflow_id=workflow_id.strip(),
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            inputs=inputs if isinstance(inputs, dict) else {},
            max_retries=max_retries,
        )
        return submission.to_dict()
    except Exception as e:
        return make_error_response(
            "create_failed",
            f"Failed to create workflow instance: {e}",
        )


def _workflow_list_instances(**kwargs):
    """List workflow instances with filters."""
    from ...workflows.durable import WorkflowInstanceManager, WorkflowInstanceStatus

    user_id = kwargs.get("user_id")
    org_id = kwargs.get("org_id")
    namespace_raw = kwargs.get("namespace")
    status_str = kwargs.get("status")
    workflow_id = kwargs.get("workflow_id")
    source_event_type = kwargs.get("source_event_type")
    source_event_id = kwargs.get("source_event_id")
    session_id = kwargs.get("session_id") or kwargs.get("conversation_session_id")
    request_id = kwargs.get("request_id") or kwargs.get("turn_id")
    from_utc = kwargs.get("from_utc")
    to_utc = kwargs.get("to_utc")
    namespace = (
        namespace_raw.strip()
        if isinstance(namespace_raw, str) and namespace_raw.strip()
        else None
    )
    try:
        limit = int(kwargs.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    status = None
    if status_str:
        try:
            status = WorkflowInstanceStatus(status_str.lower())
        except ValueError:
            return make_error_response(
                "invalid_status",
                f"Invalid status: {status_str}. Valid values: pending, running, completed, failed, cancelled, paused",
            )

    manager = WorkflowInstanceManager()
    instances = manager.list_instances(
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
        status=status,
        workflow_id=workflow_id,
        source_event_type=source_event_type,
        source_event_id=source_event_id,
        conversation_session_id=session_id
        if isinstance(session_id, str) and session_id.strip()
        else None,
        request_id=request_id if isinstance(request_id, str) and request_id.strip() else None,
        from_utc=from_utc if isinstance(from_utc, str) and from_utc.strip() else None,
        to_utc=to_utc if isinstance(to_utc, str) and to_utc.strip() else None,
        limit=limit,
    )

    return {
        "success": True,
        "instances": [inst.to_status_dict() for inst in instances],
        "count": len(instances),
    }


def _workflow_get_instance(**kwargs):
    """Get details of a specific workflow instance."""
    from ...workflows.durable import WorkflowInstanceManager

    instance_id = kwargs.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.strip():
        return make_error_response(
            "missing_parameter",
            "instance_id is required",
            details={"missing": ["instance_id"]},
        )

    manager = WorkflowInstanceManager()
    instance = manager.get_instance(instance_id.strip())

    if not instance:
        return make_error_response(
            "not_found",
            f"Workflow instance not found: {instance_id}",
        )

    result = instance.to_status_dict()
    result["success"] = True
    result["inputs"] = instance.inputs
    result["outputs"] = instance.outputs
    result["user_id"] = instance.user_id
    result["org_id"] = instance.org_id
    result["namespace"] = instance.namespace
    result["error_step"] = instance.error_step
    result["schedule_id"] = instance.schedule_id
    result["workflow_data"] = instance.workflow_data

    return result


def _workflow_cancel_instance(**kwargs):
    """Cancel a running or pending workflow instance."""
    from ...workflows.durable import WorkflowInstanceManager

    instance_id = kwargs.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.strip():
        return make_error_response(
            "missing_parameter",
            "instance_id is required",
            details={"missing": ["instance_id"]},
        )

    manager = WorkflowInstanceManager()
    instance = manager.get_instance(instance_id.strip())

    if not instance:
        return make_error_response(
            "not_found",
            f"Workflow instance not found: {instance_id}",
        )

    if instance.status.is_terminal():
        return make_error_response(
            "already_terminal",
            f"Instance is already in terminal status: {instance.status.value}",
        )

    success = manager.mark_cancelled(instance_id.strip())
    if success:
        return {
            "success": True,
            "instance_id": instance_id,
            "status": "cancelled",
        }
    else:
        return make_error_response("cancel_failed", "Failed to cancel instance")


def _workflow_retry_instance(**kwargs):
    """Reset a failed workflow instance for retry."""
    from ...workflows.durable import WorkflowInstanceManager, WorkflowInstanceStatus

    instance_id = kwargs.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.strip():
        return make_error_response(
            "missing_parameter",
            "instance_id is required",
            details={"missing": ["instance_id"]},
        )

    manager = WorkflowInstanceManager()
    instance = manager.get_instance(instance_id.strip())

    if not instance:
        return make_error_response(
            "not_found",
            f"Workflow instance not found: {instance_id}",
        )

    if instance.status != WorkflowInstanceStatus.FAILED:
        return make_error_response(
            "not_failed",
            f"Instance is not in failed status: {instance.status.value}",
        )

    success = manager.reset_for_retry(instance_id.strip())
    if success:
        return {
            "success": True,
            "instance_id": instance_id,
            "status": "pending",
            "retry_count": instance.retry_count,
        }
    else:
        return make_error_response(
            "retry_limit_exceeded",
            f"Maximum retries exceeded: {instance.retry_count}/{instance.max_retries}",
        )


# =============================================================================
# Durable Workflow Schedule Handlers
# =============================================================================


def _workflow_create_schedule(**kwargs):
    """Create a new workflow schedule."""
    from datetime import datetime, timezone
    from ...workflows.durable import (
        WorkflowInstanceManager,
        WorkflowSchedule,
        ScheduleType,
    )

    workflow_id = kwargs.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        return make_error_response(
            "missing_parameter",
            "workflow_id is required",
            details={"missing": ["workflow_id"]},
        )

    user_id = kwargs.get("user_id", "anonymous")
    org_id = kwargs.get("org_id", "default")
    namespace = kwargs.get("namespace", f"{user_id}/{org_id}")
    default_inputs = kwargs.get("default_inputs", {})
    description = kwargs.get("description")

    schedule_type_str = kwargs.get("schedule_type", "interval")
    try:
        schedule_type = ScheduleType(schedule_type_str.lower())
    except ValueError:
        return make_error_response(
            "invalid_schedule_type",
            f"Invalid schedule_type: {schedule_type_str}. Valid values: once, interval, cron",
        )

    schedule = None

    if schedule_type == ScheduleType.INTERVAL:
        interval_seconds = kwargs.get("interval_seconds")
        if not interval_seconds or not isinstance(interval_seconds, (int, float)):
            return make_error_response(
                "missing_parameter",
                "interval_seconds is required for interval type",
            )
        schedule = WorkflowSchedule.create_interval(
            workflow_id.strip(),
            interval_seconds=int(interval_seconds),
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            default_inputs=default_inputs if isinstance(default_inputs, dict) else {},
            description=description,
        )

    elif schedule_type == ScheduleType.CRON:
        cron_expression = kwargs.get("cron_expression")
        if not isinstance(cron_expression, str) or not cron_expression.strip():
            return make_error_response(
                "missing_parameter",
                "cron_expression is required for cron type",
            )
        schedule = WorkflowSchedule.create_cron(
            workflow_id.strip(),
            cron_expression=cron_expression.strip(),
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            default_inputs=default_inputs if isinstance(default_inputs, dict) else {},
            description=description,
        )

    elif schedule_type == ScheduleType.ONCE:
        run_at_str = kwargs.get("run_at")
        if not isinstance(run_at_str, str) or not run_at_str.strip():
            return make_error_response(
                "missing_parameter",
                "run_at is required for once type",
            )
        try:
            run_at = datetime.fromisoformat(run_at_str.replace("Z", "+00:00"))
        except ValueError:
            return make_error_response(
                "invalid_datetime",
                "Invalid run_at datetime format. Use ISO format: 2026-02-04T10:00:00Z",
            )
        schedule = WorkflowSchedule.create_once(
            workflow_id.strip(),
            run_at=run_at,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            default_inputs=default_inputs if isinstance(default_inputs, dict) else {},
            description=description,
        )

    if schedule is None:
        return make_error_response("create_failed", "Failed to create schedule")

    try:
        manager = WorkflowInstanceManager()
        schedule_id = manager.create_schedule(schedule)
        return {
            "success": True,
            "schedule_id": schedule_id,
            "schedule_type": schedule_type.value,
        }
    except Exception as e:
        return make_error_response(
            "create_failed",
            f"Failed to create workflow schedule: {e}",
        )


def _workflow_list_schedules(**kwargs):
    """List workflow schedules."""
    from ...workflows.durable import WorkflowInstanceManager

    user_id = kwargs.get("user_id")
    enabled_only = kwargs.get("enabled_only", False)
    limit = min(int(kwargs.get("limit", 50)), 200)

    manager = WorkflowInstanceManager()
    schedules = manager.list_schedules(
        user_id=user_id,
        enabled_only=bool(enabled_only),
        limit=limit,
    )

    return {
        "success": True,
        "schedules": [sched.to_status_dict() for sched in schedules],
        "count": len(schedules),
    }


def _workflow_get_schedule(**kwargs):
    """Get details of a specific workflow schedule."""
    from ...workflows.durable import WorkflowInstanceManager

    schedule_id = kwargs.get("schedule_id")
    if not isinstance(schedule_id, str) or not schedule_id.strip():
        return make_error_response(
            "missing_parameter",
            "schedule_id is required",
            details={"missing": ["schedule_id"]},
        )

    manager = WorkflowInstanceManager()
    schedule = manager.get_schedule(schedule_id.strip())

    if not schedule:
        return make_error_response(
            "not_found",
            f"Workflow schedule not found: {schedule_id}",
        )

    result = schedule.to_status_dict()
    result["success"] = True
    result["user_id"] = schedule.user_id
    result["org_id"] = schedule.org_id
    result["namespace"] = schedule.namespace
    result["default_inputs"] = schedule.default_inputs
    result["created_at"] = (
        schedule.created_at.isoformat() if schedule.created_at else None
    )
    result["updated_at"] = (
        schedule.updated_at.isoformat() if schedule.updated_at else None
    )

    return result


def _workflow_set_schedule_enabled(**kwargs):
    """Enable or disable a workflow schedule."""
    from ...workflows.durable import WorkflowInstanceManager

    schedule_id = kwargs.get("schedule_id")
    if not isinstance(schedule_id, str) or not schedule_id.strip():
        return make_error_response(
            "missing_parameter",
            "schedule_id is required",
            details={"missing": ["schedule_id"]},
        )

    enabled = kwargs.get("enabled")
    if not isinstance(enabled, bool):
        return make_error_response(
            "missing_parameter",
            "enabled (boolean) is required",
            details={"missing": ["enabled"]},
        )

    manager = WorkflowInstanceManager()
    schedule = manager.get_schedule(schedule_id.strip())

    if not schedule:
        return make_error_response(
            "not_found",
            f"Workflow schedule not found: {schedule_id}",
        )

    success = manager.set_schedule_enabled(schedule_id.strip(), enabled)
    if success:
        return {
            "success": True,
            "schedule_id": schedule_id,
            "enabled": enabled,
        }
    else:
        return make_error_response("update_failed", "Failed to update schedule")


def _workflow_delete_schedule(**kwargs):
    """Delete a workflow schedule."""
    from ...workflows.durable import WorkflowInstanceManager

    schedule_id = kwargs.get("schedule_id")
    if not isinstance(schedule_id, str) or not schedule_id.strip():
        return make_error_response(
            "missing_parameter",
            "schedule_id is required",
            details={"missing": ["schedule_id"]},
        )

    manager = WorkflowInstanceManager()
    schedule = manager.get_schedule(schedule_id.strip())

    if not schedule:
        return make_error_response(
            "not_found",
            f"Workflow schedule not found: {schedule_id}",
        )

    success = manager.delete_schedule(schedule_id.strip())
    if success:
        return {
            "success": True,
            "deleted": True,
            "schedule_id": schedule_id,
        }
    else:
        return make_error_response("delete_failed", "Failed to delete schedule")


def _workflow_trigger_schedule(**kwargs):
    """Manually trigger a workflow schedule immediately."""
    from ...workflows.durable import WorkflowInstanceManager
    from ...workflows.durable.workflow_instance_submission_service import (
        submit_verified_workflow_instance,
    )

    schedule_id = kwargs.get("schedule_id")
    if not isinstance(schedule_id, str) or not schedule_id.strip():
        return make_error_response(
            "missing_parameter",
            "schedule_id is required",
            details={"missing": ["schedule_id"]},
        )

    manager = WorkflowInstanceManager()
    schedule = manager.get_schedule(schedule_id.strip())

    if not schedule:
        return make_error_response(
            "not_found",
            f"Workflow schedule not found: {schedule_id}",
        )

    try:
        submission = submit_verified_workflow_instance(
            manager=manager,
            workflow_id=schedule.workflow_id,
            user_id=schedule.user_id,
            org_id=schedule.org_id,
            namespace=schedule.namespace,
            inputs=schedule.default_inputs,
            schedule_id=schedule.schedule_id,
        )
        payload = submission.to_dict()
        payload["schedule_id"] = schedule_id
        if submission.success:
            payload["status"] = "triggered"
        return payload
    except Exception as e:
        return make_error_response(
            "trigger_failed",
            f"Failed to trigger schedule: {e}",
        )


def _get_related_concepts(**kwargs):
    """Find concepts with similar descriptions (vector similarity)."""

    concept_id = kwargs.get("concept_id")
    if not isinstance(concept_id, str) or not concept_id.strip():
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: concept_id",
            details={"missing": ["concept_id"]},
            suggestions=["Provide the concept ID for similarity search"],
        )

    ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
    ns = ns_report.get("namespace")
    ns_error = _rag_namespace_resolution_error(ns_report)
    if ns_error is not None:
        if ns_error.get("error") == "namespace_required":
            ns_error["message"] = (
                "RAG search requires authenticated user context (namespace)"
            )
        return ns_error

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
        return make_error_response(
            "rag_service_unavailable",
            f"RAG service unavailable: {e}",
            details={"exception_type": "RAGBackendUnavailable", **ns_report},
            suggestions=["Check that the RAG service is running and configured"],
        )

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


# Renderer applicability schema helpers (JVNAUTOSCI-1140)
def _renderer_resolve_applicability_input_schema() -> Schema:
    return Schema(
        required={
            "request_payload": dict,
        },
        optional={
            "renderer_definitions": list,
            "renderer_definition_concept_ids": list,
            "allow_multimodal": bool,
            # Accepted for LLM consistency; resolver currently does not use it.
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "renderer_resolve_applicability input: request_payload (dict) and either "
            "renderer_definitions (list) or renderer_definition_concept_ids (list[str]). "
            "Optional allow_multimodal (bool, default true)."
        ),
    )


def _renderer_resolve_applicability_output_schema() -> Schema:
    return Schema(
        required={"success": bool},
        optional={
            "interpreted_object_kind": (str, type(None)),
            "interpreted_as_transient_microtheory": (bool, type(None)),
            "selected_renderers": (list, type(None)),
            "candidate_evaluations": (list, type(None)),
            "diagnostics": (dict, type(None)),
            "error": (str, type(None)),
            "error_code": (str, type(None)),
            "error_details": (dict, type(None)),
            "suggestions": (list, type(None)),
            "related_concept_ids": (list, type(None)),
        },
        allow_unknown=True,
        description=(
            "renderer_resolve_applicability output: deterministic renderer selection and fallback diagnostics."
        ),
    )


def _upsert_renderer_profile_input_schema() -> Schema:
    return Schema(
        required={
            "renderer_concept_id": str,
            "renderer_profile": dict,
        },
        optional={
            "predicate": str,
            "language": str,
            "policy": str,
            "garbage_collect": (bool, type(None)),
            "provenance": (dict, type(None)),
            "context": (dict, type(None)),
            # Accepted for LLM consistency.
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "upsert_renderer_profile input: renderer_concept_id (str), renderer_profile (dict), "
            "optional predicate/language/policy/provenance/context."
        ),
    )


def _upsert_renderer_profile_output_schema() -> Schema:
    return Schema(
        required={"success": bool},
        optional={
            "renderer_concept_id": (str, type(None)),
            "predicate": (str, type(None)),
            "language": (str, type(None)),
            "renderer_profile": (dict, type(None)),
            "text_relation": (dict, type(None)),
            "error": (str, type(None)),
            "error_code": (str, type(None)),
            "error_details": (dict, type(None)),
            "suggestions": (list, type(None)),
            "related_concept_ids": (list, type(None)),
        },
        allow_unknown=True,
        description=(
            "upsert_renderer_profile output: persisted renderer profile metadata with singleton text relation diagnostics."
        ),
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


def _jira_get_transitions_input_schema() -> Schema:
    return Schema(
        required={"issue_key": str},
        optional={},
        allow_unknown=True,
        description="jira_get_transitions input: issue_key (str, required)",
    )


def _jira_add_comment_input_schema() -> Schema:
    return Schema(
        required={"issue_key": str, "comment": str},
        optional={},
        allow_unknown=True,
        description="jira_add_comment input: issue_key (str) and comment (str) both required",
    )


def _jira_add_attachment_input_schema() -> Schema:
    return Schema(
        required={
            "issue_key": str,
            "filename": str,
            "content_base64": str,
            "mime_type": str,
        },
        optional={
            "comment": (str, type(None)),
            # Accepted for LLM consistency; ignored by handler logic.
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "jira_add_attachment input: issue_key, filename, content_base64, mime_type (all required). "
            "Optional comment and namespace."
        ),
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
            "components": (list, type(None)),
            "dry_run": (bool,),
            "approved": (bool,),
            "execute": (bool,),
            "request_id": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "jira_create_issue input: project_key, issue_type, summary (required). "
            "Optional description/parent/assignee_account_id/labels/components. "
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
            "link_type": str,
        },
        optional={
            # Preferred semantic aliases. source_issue_key maps to Jira inwardIssue,
            # target_issue_key maps to Jira outwardIssue.
            "source_issue_key": (str, type(None)),
            "target_issue_key": (str, type(None)),
            # Backward-compatible Jira-native fields.
            "inward_issue_key": (str, type(None)),
            "outward_issue_key": (str, type(None)),
            "comment": (str, type(None)),
            "dry_run": (bool,),
            "approved": (bool,),
            "execute": (bool,),
            "request_id": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "jira_link_issue input: link_type (required; e.g. 'Relates'). "
            "Preferred issue fields: source_issue_key + target_issue_key "
            "(source maps to Jira inwardIssue; target maps to Jira outwardIssue). "
            "Backward-compatible fields: inward_issue_key + outward_issue_key. "
            "Optional comment. Guardrails: dry_run (default true), approved, execute "
            "(requires VON_INTERNAL_MCP_JIRA_EXECUTE_MODE=1)."
        ),
    )


def _jira_delete_issue_link_input_schema() -> Schema:
    return Schema(
        required={
            "issue_link_id": str,
        },
        optional={
            # Optional issue keys allow allow-list checks to remain project-scoped.
            "source_issue_key": (str, type(None)),
            "target_issue_key": (str, type(None)),
            "dry_run": (bool,),
            "approved": (bool,),
            "execute": (bool,),
            "request_id": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "jira_delete_issue_link input: issue_link_id (required). "
            "At least one of source_issue_key or target_issue_key is required for project allow-list validation. "
            "Guardrails: dry_run (default true), approved, execute "
            "(requires VON_INTERNAL_MCP_JIRA_EXECUTE_MODE=1)."
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
        # Accept orchestrator context keys (for example namespace); this tool ignores them.
        allow_unknown=True,
        description="jira_get_auth_config input: no arguments",
    )


def _jira_hygiene_discover_input_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "project_key": (str, type(None)),
            "candidate_epic_keys": (list, type(None)),
            "max_issues": (int, str, type(None)),
            "max_epics": (int, str, type(None)),
            "include_cross_cutting": (bool, str, type(None)),
            "include_in_progress_candidates": (bool, str, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "jira_hygiene_discover input: optional project_key/candidate_epic_keys "
            "and paging controls (max_issues, max_epics), plus optional "
            "include_in_progress_candidates review extraction."
        ),
    )


def _jira_hygiene_propose_input_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "epic_catalogue": (list, type(None)),
            "orphan_candidates": (list, type(None)),
            "cross_cutting_candidates": (list, type(None)),
            "in_progress_candidates": (list, type(None)),
            "batch_size": (int, str, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "jira_hygiene_propose input: discovery payload fields (epic_catalogue, "
            "orphan_candidates, cross_cutting_candidates, in_progress_candidates) "
            "and optional batch_size."
        ),
    )


def _jira_hygiene_check_approval_input_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "ready_to_execute": (list, type(None)),
            "execution_mode": (str, type(None)),
            "approved": (bool, str, type(None)),
            "excluded_issue_keys": (list, type(None)),
            "overrides": (dict, type(None)),
            "batch_size": (int, str, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "jira_hygiene_check_approval input: proposal operations plus approval "
            "controls (approved, excluded_issue_keys, overrides, batch_size)."
        ),
    )


def _jira_hygiene_execute_batches_input_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "approved_operations": (list, type(None)),
            "execution_mode": (str, type(None)),
            "approved": (bool, str, type(None)),
            "batch_size": (int, str, type(None)),
            "max_retries": (int, str, type(None)),
            "retry_backoff_seconds": (int, float, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "jira_hygiene_execute_batches input: approved operations plus execution "
            "controls (batch_size, max_retries, retry_backoff_seconds)."
        ),
    )


def _jira_hygiene_emit_audit_input_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "project_key": (str, type(None)),
            "execution_mode": (str, type(None)),
            "proposal_summary": (dict, type(None)),
            "execution_summary": (dict, type(None)),
            "approved_operations": (list, type(None)),
            "needs_decision": (list, type(None)),
            "emit_epic_comments": (bool, str, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "jira_hygiene_emit_audit input: proposal/execution summaries with "
            "optional epic-level audit comment emission."
        ),
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


def _jira_add_attachment_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
        },
        optional={
            "issue_key": (str, type(None)),
            "attachment_id": (str, type(None)),
            "filename": (str, type(None)),
            "size_bytes": (int, type(None)),
            "content_type": (str, type(None)),
            "comment_added": (bool, type(None)),
            "jira_attachment": (dict, type(None)),
            "error": (str, type(None)),
            "error_code": (str, type(None)),
            "error_details": (dict, type(None)),
            "suggestions": (list, type(None)),
        },
        allow_unknown=True,
        description=(
            "jira_add_attachment output: success flag plus structured attachment metadata "
            "(issue_key, attachment_id, filename, size_bytes, content_type)."
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


def _task_generic_output_schema(action: str) -> Schema:
    return Schema(
        required={"success": bool},
        optional={
            "error": (str, type(None)),
            "error_code": (str, type(None)),
            "count": (int, type(None)),
            "total": (int, type(None)),
            "offset": (int, type(None)),
            "limit": (int, type(None)),
        },
        allow_unknown=True,
        description=(
            f"task_{action} output: task operation response with success/error fields and operation-specific payload."
        ),
    )


def _shared_conversation_generic_output_schema(action: str) -> Schema:
    return Schema(
        required={"success": bool},
        optional={
            "error": (str, type(None)),
            "error_code": (str, type(None)),
            "error_details": (dict, type(None)),
            "suggestions": (list, type(None)),
            "session": (dict, type(None)),
            "invite": (dict, type(None)),
            "invites": (list, type(None)),
            "count": (int, type(None)),
            "created": (bool, type(None)),
            "joined": (bool, type(None)),
            "created_session": (bool, type(None)),
            "is_owner": (bool, type(None)),
            "has_accepted_invite": (bool, type(None)),
            "access_mode": (str, type(None)),
            "session_id": (str, type(None)),
            "invite_id": (str, type(None)),
            "action": (str, type(None)),
            "user_concept_id": (str, type(None)),
            "actor_user_id": (str, type(None)),
            "actor_concept_id": (str, type(None)),
            "organisation_concept_id": (str, type(None)),
            "owner_user_id": (str, type(None)),
            "episode_id": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            f"shared_conversation_{action} output: shared-conversation MCP operation response "
            "with success/error fields and operation-specific payload."
        ),
    )


def _shared_conversation_create_session_input_schema() -> Schema:
    return Schema(
        required={"session_id": str},
        optional={
            "session_name": (str, type(None)),
            "user_concept_id": (str, type(None)),
            "acting_user_concept_id": (str, type(None)),
            "actor_user_id": (str, type(None)),
            "on_behalf_of_user_concept_id": (str, type(None)),
            "actor_concept_id": (str, type(None)),
            "agent_concept_id": (str, type(None)),
            "organisation_concept_id": (str, type(None)),
            "org_id": (str, type(None)),
            "role_in_org": (str, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "Create or materialise a chat session for a user in shared conversation workflows. "
            "Requires session_id; user can be provided directly or inferred from namespace."
        ),
    )


def _shared_conversation_join_session_input_schema() -> Schema:
    return Schema(
        required={"session_id": str},
        optional={
            "session_name": (str, type(None)),
            "user_concept_id": (str, type(None)),
            "acting_user_concept_id": (str, type(None)),
            "actor_user_id": (str, type(None)),
            "on_behalf_of_user_concept_id": (str, type(None)),
            "actor_concept_id": (str, type(None)),
            "agent_concept_id": (str, type(None)),
            "organisation_concept_id": (str, type(None)),
            "org_id": (str, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "Validate shared-conversation access for a user and materialise a local session record "
            "if needed for participation workflows."
        ),
    )


def _shared_conversation_invite_create_input_schema() -> Schema:
    return Schema(
        required={"session_id": str},
        optional={
            "invitee_concept_id": (str, type(None)),
            "invitee_user_id": (str, type(None)),
            "user_concept_id": (str, type(None)),
            "acting_user_concept_id": (str, type(None)),
            "actor_user_id": (str, type(None)),
            "on_behalf_of_user_concept_id": (str, type(None)),
            "actor_concept_id": (str, type(None)),
            "agent_concept_id": (str, type(None)),
            "organisation_concept_id": (str, type(None)),
            "org_id": (str, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "Create a shared-conversation invite for an invitee in the active organisation scope."
        ),
    )


def _shared_conversation_list_invites_input_schema() -> Schema:
    return Schema(
        required={},
        optional={
            "status": (str, type(None)),
            "direction": (str, type(None)),
            "session_id": (str, type(None)),
            "user_concept_id": (str, type(None)),
            "acting_user_concept_id": (str, type(None)),
            "actor_user_id": (str, type(None)),
            "on_behalf_of_user_concept_id": (str, type(None)),
            "actor_concept_id": (str, type(None)),
            "agent_concept_id": (str, type(None)),
            "organisation_concept_id": (str, type(None)),
            "org_id": (str, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "List shared-conversation invites for a user. Supports incoming/outgoing direction, "
            "status filtering, and optional session scoping."
        ),
    )


def _shared_conversation_respond_invite_input_schema() -> Schema:
    return Schema(
        required={"invite_id": str, "action": str},
        optional={
            "join_session_on_accept": (bool, type(None)),
            "session_name": (str, type(None)),
            "user_concept_id": (str, type(None)),
            "acting_user_concept_id": (str, type(None)),
            "actor_user_id": (str, type(None)),
            "on_behalf_of_user_concept_id": (str, type(None)),
            "actor_concept_id": (str, type(None)),
            "agent_concept_id": (str, type(None)),
            "organisation_concept_id": (str, type(None)),
            "org_id": (str, type(None)),
            "namespace": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "Accept or decline a shared-conversation invite for a user. "
            "Optionally materialises a session entry on accept."
        ),
    )


# RAG metadata/content MCP tools
def _rag_get_status(**kwargs):
    import requests
    import os

    try:
        ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
        ns_error = _rag_namespace_resolution_error(ns_report)
        if ns_error is not None and ns_error.get("error") == "namespace_mismatch":
            return ns_error

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
        return make_error_response(
            "http_error",
            f"HTTP {res.status_code}",
            details={"status_code": res.status_code},
        )
    except Exception as e:
        return make_error_response(
            "exception", str(e), details={"exception_type": type(e).__name__}
        )


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
        "file_copy": "file_copy_concepts",
        "file_copies": "file_copy_concepts",
        "file_copy_concept": "file_copy_concepts",
        "file_copy_concepts": "file_copy_concepts",
        "blob_store": "file_copy_concepts",
        "blob_store_file": "file_copy_concepts",
        "blob_store_files": "file_copy_concepts",
        "uploaded_file": "file_copy_concepts",
        "uploaded_files": "file_copy_concepts",
        "text_relations": "vontology_text_relations",
        "text_relation": "vontology_text_relations",
        "text": "vontology_text_relations",
        "vontology_text_relations": "vontology_text_relations",
        "turn_execution": "turn_execution_records",
        "turn_execution_record": "turn_execution_records",
        "turn_execution_records": "turn_execution_records",
        "execution_record": "turn_execution_records",
        "execution_records": "turn_execution_records",
        "turn_record": "turn_execution_records",
        "turn_records": "turn_execution_records",
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
    ns_error = _rag_namespace_resolution_error(ns_report)
    if ns_error is not None:
        return ns_error

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
            "collection": "file_copy_concepts",
            "label": "Blob-store file-copy concepts",
            "description": (
                "User-visible file-copy concepts stored in MongoDB (concepts collection) with blob metadata. "
                "Use this to inspect uploaded/object-store files discoverable by namespace-scoped retrieval."
            ),
            "list_tool": "rag_list_indexed",
            "get_tool": "rag_get_item",
            "search_tool": "search_knowledge_base",
            "list_supported": True,
            "get_supported": True,
            "search_supported": True,
            "list_supported_reason": None,
            "get_supported_reason": None,
            "item_kind": "file_copy_concept",
            "source_system": "mongo.concepts",
        },
        {
            "collection": "turn_execution_records",
            "label": "Turn execution records",
            "description": (
                "Assistant turn execution records stored in MongoDB (turn_execution_records). "
                "Includes workflow selection, required effects, postcondition checks, and completion-gate decisions. "
                "Use to analyse failed or incomplete action execution across conversations. "
                "Responses also include per-record RAG indexing-state diagnostics derived from chat_history."
            ),
            "list_tool": "rag_list_indexed",
            "get_tool": "rag_get_item",
            "search_tool": None,
            "list_supported": True,
            "get_supported": True,
            "search_supported": False,
            "list_supported_reason": None,
            "get_supported_reason": None,
            "item_kind": "turn_execution_record",
            "source_system": "mongo.turn_execution_records",
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


def _normalise_non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0


def _derive_turn_execution_rag_indexing_state_from_chat_doc(
    chat_doc: Mapping[str, Any],
) -> dict[str, Any]:
    chat_session_id_raw = chat_doc.get("session_id")
    chat_session_id = (
        chat_session_id_raw.strip()
        if isinstance(chat_session_id_raw, str) and chat_session_id_raw.strip()
        else None
    )
    history_raw = chat_doc.get("history")
    message_total = len(history_raw) if isinstance(history_raw, list) else None
    indexed_success = _normalise_non_negative_int(chat_doc.get("rag_indexed_success"))
    indexed_failed = _normalise_non_negative_int(chat_doc.get("rag_indexed_failed"))

    pending_messages: int | None = None
    if isinstance(message_total, int):
        pending_messages = message_total - indexed_success - indexed_failed
        if pending_messages < 0:
            pending_messages = 0

    status = "not_indexed"
    sync_state = "not_indexed"
    reason_code = "no_successful_indexing_attempts_recorded"
    reason = "No successful RAG indexing attempts were recorded for this chat session."
    indexed = indexed_success > 0
    fully_indexed = False

    if indexed_success > 0:
        if indexed_failed > 0:
            status = "partial"
            sync_state = "error"
            reason_code = "indexing_attempts_include_failures"
            reason = "Some messages indexed successfully but at least one indexing attempt failed."
            fully_indexed = False
        elif pending_messages is not None and pending_messages > 0:
            status = "partial"
            sync_state = "lagging"
            reason_code = "indexing_lag_detected"
            reason = "Some chat messages are still pending RAG indexing."
            fully_indexed = False
        else:
            status = "indexed"
            sync_state = "synchronised"
            reason_code = "indexed_and_synchronised"
            reason = "Chat session appears fully indexed in RAG."
            fully_indexed = True
    elif indexed_failed > 0:
        status = "indexing_failed"
        sync_state = "error"
        reason_code = "all_indexing_attempts_failed"
        reason = (
            "Indexing attempts were recorded, but none succeeded for this chat session."
        )
        indexed = False
        fully_indexed = False
    elif message_total == 0:
        status = "not_indexed"
        sync_state = "not_applicable"
        reason_code = "session_has_no_messages"
        reason = "Chat session has no messages to index."
        indexed = False
        fully_indexed = False

    return {
        "chat_session_id": chat_session_id,
        "status": status,
        "indexed": indexed,
        "fully_indexed": fully_indexed,
        "sync_state": sync_state,
        "reason_code": reason_code,
        "reason": reason,
        "messages_total": message_total,
        "messages_indexed_success": indexed_success,
        "messages_indexed_failed": indexed_failed,
        "messages_pending_indexing": pending_messages,
        "source_system": "mongo.chat_history",
    }


def _build_turn_execution_rag_indexing_state(
    *,
    chat_session_id: Any,
    state_by_chat_session_id: Mapping[str, Mapping[str, Any]],
    lookup_warning: Mapping[str, str] | None,
) -> dict[str, Any]:
    if not isinstance(chat_session_id, str) or not chat_session_id.strip():
        return {
            "chat_session_id": None,
            "status": "unknown",
            "indexed": None,
            "fully_indexed": None,
            "sync_state": "unknown",
            "reason_code": "chat_session_id_missing_on_turn_record",
            "reason": "Turn execution record does not include a chat_session_id.",
            "messages_total": None,
            "messages_indexed_success": None,
            "messages_indexed_failed": None,
            "messages_pending_indexing": None,
            "source_system": "mongo.chat_history",
        }

    session_id = chat_session_id.strip()
    if lookup_warning is not None:
        return {
            "chat_session_id": session_id,
            "status": "unknown",
            "indexed": None,
            "fully_indexed": None,
            "sync_state": "unknown",
            "reason_code": lookup_warning.get(
                "reason_code", "chat_history_lookup_unavailable"
            ),
            "reason": lookup_warning.get(
                "reason",
                "Could not read chat_history to determine RAG indexing state.",
            ),
            "messages_total": None,
            "messages_indexed_success": None,
            "messages_indexed_failed": None,
            "messages_pending_indexing": None,
            "source_system": "mongo.chat_history",
        }

    indexed_state = state_by_chat_session_id.get(session_id)
    if indexed_state is not None:
        return dict(indexed_state)

    return {
        "chat_session_id": session_id,
        "status": "not_indexed",
        "indexed": False,
        "fully_indexed": False,
        "sync_state": "unknown",
        "reason_code": "chat_history_session_not_found",
        "reason": "No matching chat_history session was found for this turn execution record.",
        "messages_total": None,
        "messages_indexed_success": None,
        "messages_indexed_failed": None,
        "messages_pending_indexing": None,
        "source_system": "mongo.chat_history",
    }


def _load_turn_execution_rag_indexing_state_map(
    *,
    db: Any,
    namespace: str,
    chat_session_ids: Sequence[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str] | None]:
    unique_chat_session_ids: list[str] = []
    seen_ids: set[str] = set()
    for raw_session_id in chat_session_ids:
        if not isinstance(raw_session_id, str):
            continue
        session_id = raw_session_id.strip()
        if not session_id:
            continue
        if session_id in seen_ids:
            continue
        seen_ids.add(session_id)
        unique_chat_session_ids.append(session_id)

    if not unique_chat_session_ids:
        return {}, None

    try:
        chat_history_coll = db["chat_history"]
    except Exception:
        return {}, {
            "reason_code": "chat_history_lookup_unavailable",
            "reason": "chat_history collection unavailable while deriving RAG indexing state.",
        }

    query = {
        "namespace": namespace,
        "session_id": {"$in": unique_chat_session_ids},
    }
    projection = {
        "session_id": 1,
        "history": 1,
        "rag_indexed_success": 1,
        "rag_indexed_failed": 1,
    }

    try:
        cursor = chat_history_coll.find(query, projection)
    except Exception as exc:
        return {}, {
            "reason_code": "chat_history_lookup_failed",
            "reason": (
                "chat_history lookup failed while deriving RAG indexing state: "
                f"{type(exc).__name__}"
            ),
        }

    state_by_chat_session_id: dict[str, dict[str, Any]] = {}
    try:
        for doc in cursor:
            if not isinstance(doc, dict):
                continue
            session_id_raw = doc.get("session_id")
            if not isinstance(session_id_raw, str) or not session_id_raw.strip():
                continue
            session_id = session_id_raw.strip()
            state_by_chat_session_id[session_id] = (
                _derive_turn_execution_rag_indexing_state_from_chat_doc(doc)
            )
    except Exception as exc:
        return {}, {
            "reason_code": "chat_history_lookup_failed",
            "reason": (
                "chat_history cursor iteration failed while deriving RAG indexing state: "
                f"{type(exc).__name__}"
            ),
        }

    return state_by_chat_session_id, None


def _summarise_turn_execution_rag_indexing_states(
    items: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        state = item.get("rag_indexing_state")
        status = None
        if isinstance(state, Mapping):
            status = state.get("status")
        status_key = status.strip() if isinstance(status, str) and status.strip() else "unknown"
        counts[status_key] = counts.get(status_key, 0) + 1
    return counts


def _resolve_rag_actor_scope_ids(
    ns_report: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """Extract user/org scope IDs from a namespace resolution report."""

    user_concept_id = ns_report.get("user_concept_id")
    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        user_concept_id = ns_report.get("derived_user_concept_id")
    if isinstance(user_concept_id, str):
        user_concept_id = user_concept_id.strip() or None
    else:
        user_concept_id = None

    organisation_concept_id = ns_report.get("organisation_concept_id")
    if not isinstance(organisation_concept_id, str) or not organisation_concept_id.strip():
        organisation_concept_id = ns_report.get("derived_organisation_concept_id")
    if isinstance(organisation_concept_id, str):
        organisation_concept_id = organisation_concept_id.strip() or None
    else:
        organisation_concept_id = None

    return user_concept_id, organisation_concept_id


def _build_rag_file_copy_visibility_query(
    *,
    ns_report: Mapping[str, Any],
    extra_filters: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    user_concept_id, organisation_concept_id = _resolve_rag_actor_scope_ids(ns_report)
    visibility_filters: list[dict[str, Any]] = []

    if user_concept_id:
        visibility_filters.append(
            {"relationships.specific_to_user": {"$in": [user_concept_id]}}
        )
        visibility_filters.append(
            {"relationships.#V#specific_to_user": {"$in": [user_concept_id]}}
        )
    if organisation_concept_id:
        visibility_filters.append(
            {"relationships.specific_to_org": {"$in": [organisation_concept_id]}}
        )

    if not visibility_filters:
        return None, "namespace_user_required"

    and_filters: list[dict[str, Any]] = [
        {"attributes.blob_key": {"$exists": True, "$nin": [None, ""]}},
        {"$or": visibility_filters},
    ]
    if extra_filters:
        for row in extra_filters:
            if isinstance(row, Mapping):
                and_filters.append(dict(row))

    return {"$and": and_filters}, None


def _coerce_int_or_none(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _build_rag_file_copy_item(
    *,
    collection: str,
    doc: Mapping[str, Any],
    namespace: str,
    namespace_source: Any,
) -> dict[str, Any]:
    attributes = doc.get("attributes")
    attrs = attributes if isinstance(attributes, Mapping) else {}
    relationships = doc.get("relationships")
    rels = relationships if isinstance(relationships, Mapping) else {}

    instance_types_raw = rels.get("is_an_instance_of")
    if isinstance(instance_types_raw, list):
        instance_types = [str(v) for v in instance_types_raw if isinstance(v, str)]
    elif isinstance(instance_types_raw, str):
        instance_types = [instance_types_raw]
    else:
        instance_types = []

    concept_id_raw = doc.get("concept_id")
    concept_id = (
        concept_id_raw
        if isinstance(concept_id_raw, str) and concept_id_raw.strip()
        else str(doc.get("_id"))
    )
    concept_id = concept_id.strip()

    original_filename = attrs.get("original_filename")
    if not isinstance(original_filename, str) or not original_filename.strip():
        fallback_name = doc.get("name")
        original_filename = (
            fallback_name.strip()
            if isinstance(fallback_name, str) and fallback_name.strip()
            else None
        )
    else:
        original_filename = original_filename.strip()

    size_bytes = _coerce_int_or_none(attrs.get("size_bytes"))
    content_type = attrs.get("content_type")
    content_type = (
        content_type.strip()
        if isinstance(content_type, str) and content_type.strip()
        else None
    )

    preview_parts: list[str] = []
    if original_filename:
        preview_parts.append(original_filename)
    if isinstance(size_bytes, int):
        preview_parts.append(f"{size_bytes} bytes")
    if content_type:
        preview_parts.append(content_type)
    preview = " | ".join(preview_parts)

    artifact_record: dict[str, Any] | None = None
    try:
        from ...services.computer_file_copy_service import build_file_copy_artifact_record

        artifact_record = build_file_copy_artifact_record(
            file_copy_concept_id=concept_id,
            concept_doc=doc,
        )
    except Exception:
        artifact_record = None

    return {
        "collection": collection,
        "session_id": concept_id,
        "concept_id": concept_id,
        "name": original_filename,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "blob": {
            "backend": attrs.get("blob_backend"),
            "key": attrs.get("blob_key"),
            "uri": attrs.get("blob_uri"),
        },
        "instance_types": instance_types,
        "updated_at": doc.get("updated_at"),
        "created_at": doc.get("created_at"),
        "namespace": namespace,
        "preview": preview[:4000],
        "preview_length": len(preview),
        "artifact_record": artifact_record,
        "item_kind": "file_copy_concept",
        "source_system": "mongo.concepts",
        "namespace_source": namespace_source,
    }


def _rag_list_indexed(**kwargs):
    from ...db.connection_manager import get_db

    db = get_db()
    if db is None:
        return make_error_response(
            "db_unavailable",
            "Database connection unavailable",
            suggestions=["Check database connectivity and configuration"],
        )
    collection_report = _resolve_rag_collection_from_kwargs(kwargs)
    collection = collection_report.get("effective_collection")
    limit = int(kwargs.get("limit", 20))
    offset = int(kwargs.get("offset", 0))

    ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
    ns = ns_report.get("namespace")

    # SECURITY: Require namespace for RAG access - prevents cross-user data leakage
    ns_error = _rag_namespace_resolution_error(ns_report)
    if ns_error is not None:
        return ns_error
    if not isinstance(ns, str) or not ns.strip():
        return make_error_response(
            "namespace_required",
            "RAG access requires authenticated user context (namespace)",
            details={"namespace_report": ns_report},
        )
    ns = ns.strip()

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

    if collection == "file_copy_concepts":
        coll = db["concepts"]
        file_copy_query, query_error = _build_rag_file_copy_visibility_query(
            ns_report=ns_report
        )
        if file_copy_query is None:
            return {
                "error": str(query_error or "namespace_user_required"),
                "message": (
                    "File-copy retrieval requires namespace-derived user or organisation context."
                ),
                "collection": collection,
                **collection_report,
                "effective_namespace": ns,
                "effective_namespace_source": ns_report.get("namespace_source"),
                **ns_report,
                "success": False,
            }
        assert file_copy_query is not None

        cursor = (
            coll.find(
                file_copy_query,
                {
                    "concept_id": 1,
                    "name": 1,
                    "attributes": 1,
                    "relationships.is_an_instance_of": 1,
                    "created_at": 1,
                    "updated_at": 1,
                },
            )
            .skip(offset)
            .limit(limit)
        )
        items: list[dict[str, Any]] = []
        for doc in cursor:
            if not isinstance(doc, dict):
                continue
            items.append(
                _build_rag_file_copy_item(
                    collection=collection,
                    doc=doc,
                    namespace=ns,
                    namespace_source=ns_report.get("namespace_source"),
                )
            )

        payload = {
            "collection": collection,
            **collection_report,
            "items": items,
            "total": coll.count_documents(file_copy_query),
            "limit": limit,
            "offset": offset,
            "effective_namespace": ns,
            "effective_namespace_source": ns_report.get("namespace_source"),
            **ns_report,
            "success": True,
        }
        return _with_rag_provenance(
            payload=payload,
            item_kind="rag_file_copy_list",
            source_system="mongo.concepts",
        )

    if collection == "turn_execution_records":
        coll = db["turn_execution_records"]

        query: dict[str, Any] = {"namespace": ns}

        decision_values: list[str] = []
        decision_single = kwargs.get("decision")
        if isinstance(decision_single, str) and decision_single.strip():
            decision_values.append(decision_single.strip())
        decisions_many = kwargs.get("decisions")
        if isinstance(decisions_many, list):
            for item in decisions_many:
                if isinstance(item, str) and item.strip():
                    decision_values.append(item.strip())
        if decision_values:
            unique_decisions: list[str] = []
            seen_decisions: set[str] = set()
            for value in decision_values:
                lowered = value.lower()
                if lowered in seen_decisions:
                    continue
                seen_decisions.add(lowered)
                unique_decisions.append(value)
            if len(unique_decisions) == 1:
                query["completion_gate.decision"] = unique_decisions[0]
            else:
                query["completion_gate.decision"] = {"$in": unique_decisions}

        workflow_id = kwargs.get("workflow_id")
        if isinstance(workflow_id, str) and workflow_id.strip():
            query["workflow_selection.selected_workflow_id"] = workflow_id.strip()

        requires_follow_up = kwargs.get("requires_follow_up")
        if isinstance(requires_follow_up, bool):
            query["completion_gate.requires_follow_up"] = requires_follow_up

        prompt_contains = kwargs.get("prompt_contains")
        if isinstance(prompt_contains, str) and prompt_contains.strip():
            query["prompt.preview"] = {
                "$regex": prompt_contains.strip(),
                "$options": "i",
            }

        from_utc = kwargs.get("from_utc")
        to_utc = kwargs.get("to_utc")
        created_range: dict[str, str] = {}
        if isinstance(from_utc, str) and from_utc.strip():
            created_range["$gte"] = from_utc.strip()
        if isinstance(to_utc, str) and to_utc.strip():
            created_range["$lte"] = to_utc.strip()
        if created_range:
            query["created_at_utc"] = created_range

        cursor = (
            coll.find(
                query,
                {
                    "request_id": 1,
                    "session_id": 1,
                    "created_at_utc": 1,
                    "namespace": 1,
                    "completion_gate": 1,
                    "required_effects": 1,
                    "workflow_selection": 1,
                    "prompt": 1,
                    "critic": 1,
                    "final_response": 1,
                },
            )
            .skip(offset)
            .limit(limit)
        )
        docs = [doc for doc in cursor if isinstance(doc, dict)]

        chat_session_ids_for_page: list[str] = []
        for doc in docs:
            chat_session_id = doc.get("session_id")
            if isinstance(chat_session_id, str) and chat_session_id.strip():
                chat_session_ids_for_page.append(chat_session_id.strip())
        rag_indexing_state_map, rag_indexing_lookup_warning = (
            _load_turn_execution_rag_indexing_state_map(
                db=db,
                namespace=ns,
                chat_session_ids=chat_session_ids_for_page,
            )
        )

        def _safe_text_optional(value: Any) -> str | None:
            if not isinstance(value, str):
                return None
            cleaned = value.strip()
            return cleaned or None

        def _safe_int_optional(value: Any) -> int | None:
            try:
                return int(value)
            except Exception:
                return None

        def _safe_bool_optional(value: Any) -> bool | None:
            if isinstance(value, bool):
                return value
            return None

        items: list[dict[str, Any]] = []
        for doc in docs:
            completion_gate_raw = doc.get("completion_gate")
            completion_gate: dict[str, Any] = (
                completion_gate_raw if isinstance(completion_gate_raw, dict) else {}
            )
            completion_gate_evidence_raw = completion_gate.get("evidence_payload")
            completion_gate_evidence: dict[str, Any] = (
                completion_gate_evidence_raw
                if isinstance(completion_gate_evidence_raw, dict)
                else {}
            )
            required_effects_raw = doc.get("required_effects")
            required_effects: list[Any] = (
                required_effects_raw if isinstance(required_effects_raw, list) else []
            )
            unresolved_effect_count = 0
            for effect in required_effects:
                if not isinstance(effect, dict):
                    continue
                status = effect.get("status")
                if isinstance(status, str) and status in {"not_executed", "not_satisfied"}:
                    unresolved_effect_count += 1

            workflow_selection_raw = doc.get("workflow_selection")
            workflow_selection: dict[str, Any] = (
                workflow_selection_raw
                if isinstance(workflow_selection_raw, dict)
                else {}
            )
            prompt_payload_raw = doc.get("prompt")
            prompt_payload: dict[str, Any] = (
                prompt_payload_raw if isinstance(prompt_payload_raw, dict) else {}
            )
            critic_payload_raw = doc.get("critic")
            critic_payload: dict[str, Any] = (
                critic_payload_raw if isinstance(critic_payload_raw, dict) else {}
            )
            critic_summary_raw = critic_payload.get("summary")
            critic_summary: dict[str, Any] = (
                critic_summary_raw if isinstance(critic_summary_raw, dict) else {}
            )
            final_response_payload_raw = doc.get("final_response")
            final_response_payload: dict[str, Any] = (
                final_response_payload_raw
                if isinstance(final_response_payload_raw, dict)
                else {}
            )
            blocking_effect_ids_raw = completion_gate.get("blocking_effect_ids")
            blocking_effect_ids: list[str] = []
            if isinstance(blocking_effect_ids_raw, list):
                for effect_id in blocking_effect_ids_raw:
                    if isinstance(effect_id, str) and effect_id.strip():
                        blocking_effect_ids.append(effect_id.strip())
            repeat_iteration = _safe_bool_optional(
                doc.get("completion_gate_repeat_iteration")
            )
            if repeat_iteration is None:
                repeat_iteration = bool(
                    completion_gate_evidence.get("repeat_iteration", False)
                )

            loop_attempts = _safe_int_optional(doc.get("completion_gate_loop_attempts"))
            if loop_attempts is None:
                loop_attempts = _safe_int_optional(
                    completion_gate_evidence.get("loop_attempts")
                )
            if loop_attempts is None:
                loop_attempts = 0

            loop_stop_reason = _safe_text_optional(
                doc.get("completion_gate_loop_stop_reason")
            ) or _safe_text_optional(completion_gate_evidence.get("repeat_stop_reason"))

            terminal_outcome = _safe_text_optional(
                doc.get("completion_gate_terminal_outcome")
            ) or _safe_text_optional(completion_gate_evidence.get("terminal_outcome"))

            escalation_signal = _safe_bool_optional(
                doc.get("completion_gate_escalation_signal")
            )
            if escalation_signal is None:
                escalation_signal = bool(
                    completion_gate_evidence.get("escalation_signal", False)
                )

            escalation_reason = _safe_text_optional(
                doc.get("completion_gate_escalation_reason")
            ) or _safe_text_optional(completion_gate_evidence.get("escalation_reason"))
            rag_indexing_state = _build_turn_execution_rag_indexing_state(
                chat_session_id=doc.get("session_id"),
                state_by_chat_session_id=rag_indexing_state_map,
                lookup_warning=rag_indexing_lookup_warning,
            )

            items.append(
                {
                    "collection": collection,
                    "session_id": doc.get("request_id"),
                    "request_id": doc.get("request_id"),
                    "chat_session_id": doc.get("session_id"),
                    "created_at_utc": doc.get("created_at_utc"),
                    "namespace": doc.get("namespace"),
                    "decision": completion_gate.get("decision"),
                    "decision_reason": completion_gate.get("decision_reason"),
                    "safe_to_claim_completion": completion_gate.get(
                        "safe_to_claim_completion"
                    ),
                    "requires_follow_up": completion_gate.get("requires_follow_up"),
                    "blocking_effect_ids": blocking_effect_ids,
                    "required_effect_count": len(required_effects),
                    "unresolved_effect_count": unresolved_effect_count,
                    "selected_workflow_id": workflow_selection.get(
                        "selected_workflow_id"
                    ),
                    "selector_verdict": workflow_selection.get("selector_verdict"),
                    "prompt_preview": prompt_payload.get("preview"),
                    "completion_claim_detected": final_response_payload.get(
                        "completion_claim_detected"
                    ),
                    "completion_claim_validated": final_response_payload.get(
                        "completion_claim_validated"
                    ),
                    "repeat_iteration": repeat_iteration,
                    "loop_attempts": loop_attempts,
                    "loop_stop_reason": loop_stop_reason,
                    "terminal_outcome": terminal_outcome,
                    "escalation_signal": escalation_signal,
                    "escalation_reason": escalation_reason,
                    "critic_summary": critic_summary,
                    "rag_indexing_state": rag_indexing_state,
                    "item_kind": "turn_execution_record",
                    "source_system": "mongo.turn_execution_records",
                    "namespace_source": ns_report.get("namespace_source"),
                }
            )

        total = coll.count_documents(query)
        decision_counts: dict[str, int] = {}
        try:
            decision_pipeline = [
                {"$match": query},
                {"$group": {"_id": "$completion_gate.decision", "count": {"$sum": 1}}},
            ]
            for row in coll.aggregate(decision_pipeline):
                key = row.get("_id")
                if key is None:
                    key = "unknown"
                key_text = str(key).strip() or "unknown"
                decision_counts[key_text] = int(row.get("count") or 0)
        except Exception:
            decision_counts = {}

        payload = {
            "collection": collection,
            **collection_report,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "decision_counts": decision_counts,
            "rag_indexing_state_counts": _summarise_turn_execution_rag_indexing_states(
                items
            ),
            "effective_namespace": ns,
            "effective_namespace_source": ns_report.get("namespace_source"),
            **ns_report,
            "success": True,
        }
        if rag_indexing_lookup_warning is not None:
            payload["rag_indexing_lookup_warning"] = rag_indexing_lookup_warning

        return _with_rag_provenance(
            payload=payload,
            item_kind="turn_execution_record_list",
            source_system="mongo.turn_execution_records",
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
        return make_error_response(
            "db_unavailable",
            "Database connection unavailable",
            suggestions=["Check database connectivity and configuration"],
        )
    collection_report = _resolve_rag_collection_from_kwargs(kwargs)
    collection = collection_report.get("effective_collection")
    session_id = kwargs.get("session_id")
    if not session_id:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: session_id",
            details={"missing": ["session_id"]},
            suggestions=["Provide the session ID to query"],
        )

    ns_report = _resolve_rag_namespace_from_kwargs(kwargs)
    ns = ns_report.get("namespace")

    # SECURITY: Require namespace for RAG access - prevents cross-user data leakage
    ns_error = _rag_namespace_resolution_error(ns_report)
    if ns_error is not None:
        return ns_error
    if not isinstance(ns, str) or not ns.strip():
        return make_error_response(
            "namespace_required",
            "RAG access requires authenticated user context (namespace)",
            details={"namespace_report": ns_report},
        )
    ns = ns.strip()

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
            return make_error_response(
                "not_found",
                f"Session {session_id} not found",
                details={"session_id": session_id, "namespace": ns},
                suggestions=["Check the session ID and namespace"],
            )

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
            return make_error_response(
                "not_found",
                f"Chat history session {session_id} not found",
                details={"session_id": session_id, "namespace": ns},
                suggestions=["Check the session ID and namespace"],
            )

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

    if collection == "file_copy_concepts":
        coll = db["concepts"]
        file_copy_query, query_error = _build_rag_file_copy_visibility_query(
            ns_report=ns_report,
            extra_filters=[{"concept_id": str(session_id).strip()}],
        )
        if file_copy_query is None:
            return {
                "error": str(query_error or "namespace_user_required"),
                "message": (
                    "File-copy retrieval requires namespace-derived user or organisation context."
                ),
                "collection": collection,
                **collection_report,
                "effective_namespace": ns,
                "effective_namespace_source": ns_report.get("namespace_source"),
                **ns_report,
                "success": False,
            }
        assert file_copy_query is not None

        doc = coll.find_one(
            file_copy_query,
            {
                "concept_id": 1,
                "name": 1,
                "attributes": 1,
                "relationships.is_an_instance_of": 1,
                "created_at": 1,
                "updated_at": 1,
            },
        )
        if not isinstance(doc, dict):
            return make_error_response(
                "not_found",
                f"File-copy concept {session_id} not found",
                details={"session_id": session_id, "namespace": ns},
                suggestions=["Check the concept_id and namespace"],
            )

        item = _build_rag_file_copy_item(
            collection=collection,
            doc=doc,
            namespace=ns,
            namespace_source=ns_report.get("namespace_source"),
        )
        payload = {
            "collection": collection,
            **collection_report,
            **item,
            "effective_namespace": ns,
            "effective_namespace_source": ns_report.get("namespace_source"),
            **ns_report,
            "success": True,
        }
        return _with_rag_provenance(
            payload=payload,
            item_kind="rag_file_copy_item",
            source_system="mongo.concepts",
        )

    if collection == "turn_execution_records":
        coll = db["turn_execution_records"]
        doc = coll.find_one({"request_id": session_id, "namespace": ns})
        if not doc:
            return make_error_response(
                "not_found",
                f"Turn execution record {session_id} not found",
                details={"request_id": session_id, "namespace": ns},
                suggestions=["Check the request_id and namespace"],
            )

        completion_gate = (
            doc.get("completion_gate")
            if isinstance(doc.get("completion_gate"), dict)
            else {}
        )
        workflow_selection = (
            doc.get("workflow_selection")
            if isinstance(doc.get("workflow_selection"), dict)
            else {}
        )
        prompt_payload = doc.get("prompt") if isinstance(doc.get("prompt"), dict) else {}
        required_effects = (
            doc.get("required_effects") if isinstance(doc.get("required_effects"), list) else []
        )
        postcondition_checks = (
            doc.get("postcondition_checks")
            if isinstance(doc.get("postcondition_checks"), list)
            else []
        )
        critic_payload = doc.get("critic") if isinstance(doc.get("critic"), dict) else {}
        rag_indexing_state_map, rag_indexing_lookup_warning = (
            _load_turn_execution_rag_indexing_state_map(
                db=db,
                namespace=ns,
                chat_session_ids=[doc.get("session_id")]
                if isinstance(doc.get("session_id"), str)
                else [],
            )
        )
        rag_indexing_state = _build_turn_execution_rag_indexing_state(
            chat_session_id=doc.get("session_id"),
            state_by_chat_session_id=rag_indexing_state_map,
            lookup_warning=rag_indexing_lookup_warning,
        )

        payload = {
            "collection": collection,
            **collection_report,
            "session_id": doc.get("request_id"),
            "request_id": doc.get("request_id"),
            "chat_session_id": doc.get("session_id"),
            "created_at_utc": doc.get("created_at_utc"),
            "updated_at_utc": doc.get("updated_at_utc"),
            "namespace": doc.get("namespace"),
            "decision": completion_gate.get("decision"),
            "decision_reason": completion_gate.get("decision_reason"),
            "safe_to_claim_completion": completion_gate.get(
                "safe_to_claim_completion"
            ),
            "requires_follow_up": completion_gate.get("requires_follow_up"),
            "blocking_effect_ids": completion_gate.get("blocking_effect_ids"),
            "selected_workflow_id": workflow_selection.get("selected_workflow_id"),
            "selector_verdict": workflow_selection.get("selector_verdict"),
            "prompt_preview": prompt_payload.get("preview"),
            "required_effects": required_effects,
            "postcondition_checks": postcondition_checks,
            "critic": critic_payload,
            "rag_indexing_state": rag_indexing_state,
            "item_kind": "turn_execution_record",
            "source_system": "mongo.turn_execution_records",
            "namespace_source": ns_report.get("namespace_source"),
            "effective_namespace": ns,
            "effective_namespace_source": ns_report.get("namespace_source"),
            **ns_report,
            "success": True,
        }
        if rag_indexing_lookup_warning is not None:
            payload["rag_indexing_lookup_warning"] = rag_indexing_lookup_warning
        return _with_rag_provenance(
            payload=payload,
            item_kind="turn_execution_record_item",
            source_system="mongo.turn_execution_records",
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
    ns_error = _rag_namespace_resolution_error(ns_report)
    if ns_error is not None:
        if ns_error.get("error") == "namespace_required":
            ns_error["message"] = (
                "RAG sync requires an explicit namespace (e.g. #V#user@org)"
            )
        return ns_error
    if not isinstance(ns, str) or not ns.strip():
        return make_error_response(
            "namespace_required",
            "RAG sync requires an explicit namespace (e.g. #V#user@org)",
            details={"namespace_report": ns_report},
        )
    ns = ns.strip()

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
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: profile",
            details={"missing": ["profile"]},
            suggestions=["Provide a Gmail profile ID"],
        )

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
        return make_error_response(
            "gmail_api_error",
            f"Gmail list failed: {exc}",
            details={"exception_type": type(exc).__name__},
            suggestions=["Check Gmail API connectivity and credentials"],
        )


def _gmail_get_message(**kwargs):
    from ...integrations.google import gmail_service as gs

    profile = kwargs.get("profile") or kwargs.get("profile_id")
    message_id = kwargs.get("message_id")
    if not profile or not message_id:
        return make_error_response(
            "missing_parameter",
            "Missing required parameters: profile and message_id",
            details={"missing": ["profile", "message_id"]},
            suggestions=["Provide both profile ID and message ID"],
        )

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
        return make_error_response(
            "gmail_api_error",
            f"Gmail get message failed: {exc}",
            details={"exception_type": type(exc).__name__},
            suggestions=["Check Gmail API connectivity and credentials"],
        )


def _gmail_get_attachment(**kwargs):
    from ...integrations.google import gmail_service as gs

    profile = kwargs.get("profile") or kwargs.get("profile_id")
    message_id = kwargs.get("message_id")
    attachment_id = kwargs.get("attachment_id")
    if not profile or not message_id or not attachment_id:
        return make_error_response(
            "missing_parameter",
            "Missing required parameters: profile, message_id, attachment_id",
            details={"missing": ["profile", "message_id", "attachment_id"]},
            suggestions=["Provide profile ID, message ID, and attachment ID"],
        )

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
        return make_error_response(
            "gmail_api_error",
            f"Gmail get attachment failed: {exc}",
            details={"exception_type": type(exc).__name__},
            suggestions=["Check Gmail API connectivity and credentials"],
        )


def _gmail_list_labels(**kwargs):
    from ...integrations.google import gmail_service as gs

    profile = kwargs.get("profile") or kwargs.get("profile_id")
    if not profile:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: profile",
            details={"missing": ["profile"]},
            suggestions=["Provide a Gmail profile ID"],
        )

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
        return make_error_response(
            "gmail_api_error",
            f"Gmail list labels failed: {exc}",
            details={"exception_type": type(exc).__name__},
            suggestions=["Check Gmail API connectivity and credentials"],
        )


def _gmail_modify_labels(**kwargs):
    from ...integrations.google import gmail_service as gs

    profile = kwargs.get("profile") or kwargs.get("profile_id")
    message_id = kwargs.get("message_id")
    allow_mutation = bool(kwargs.get("allow_mutation"))
    if not profile or not message_id:
        return make_error_response(
            "missing_parameter",
            "Missing required parameters: profile and message_id",
            details={"missing": ["profile", "message_id"]},
            suggestions=["Provide both profile ID and message ID"],
        )
    if not allow_mutation:
        return make_error_response(
            "mutation_not_allowed",
            "allow_mutation must be true to modify labels",
            suggestions=["Set allow_mutation=True to enable label modifications"],
        )

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
        return make_error_response(
            "gmail_api_error",
            f"Gmail modify labels failed: {exc}",
            details={"exception_type": type(exc).__name__},
            suggestions=["Check Gmail API connectivity and credentials"],
        )


# Jira MCP handlers

_JIRA_WRITE_CACHE: dict[str, dict[str, Any]] = {}
_JIRA_WRITE_CACHE_TTL_SEC = 3600.0
_JIRA_WRITE_CACHE_MAX = 200
_JIRA_ATTACHMENT_MAX_SIZE_BYTES_DEFAULT = 10 * 1024 * 1024
_JIRA_ATTACHMENT_ALLOWED_MIME_TYPES_DEFAULT = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "application/pdf",
        "text/plain",
        "text/markdown",
        "application/json",
    }
)


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


def _jira_attachment_max_size_bytes() -> int:
    import os

    raw = (
        os.getenv("VON_INTERNAL_MCP_JIRA_ATTACHMENT_MAX_SIZE_BYTES")
        or os.getenv("VON_JIRA_ATTACHMENT_MAX_SIZE_BYTES")
        or str(_JIRA_ATTACHMENT_MAX_SIZE_BYTES_DEFAULT)
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _JIRA_ATTACHMENT_MAX_SIZE_BYTES_DEFAULT
    if value <= 0:
        return _JIRA_ATTACHMENT_MAX_SIZE_BYTES_DEFAULT
    return value


def _jira_attachment_allowed_mime_types() -> set[str]:
    import os

    raw = (
        os.getenv("VON_INTERNAL_MCP_JIRA_ATTACHMENT_ALLOWED_MIME_TYPES")
        or os.getenv("VON_JIRA_ATTACHMENT_ALLOWED_MIME_TYPES")
    )
    if not isinstance(raw, str) or not raw.strip():
        return set(_JIRA_ATTACHMENT_ALLOWED_MIME_TYPES_DEFAULT)

    parsed: set[str] = set()
    for item in raw.split(","):
        candidate = item.strip().lower()
        if candidate:
            parsed.add(candidate)
    if not parsed:
        return set(_JIRA_ATTACHMENT_ALLOWED_MIME_TYPES_DEFAULT)
    return parsed


def _jira_sanitise_attachment_filename(filename: Any) -> str | None:
    from pathlib import PurePosixPath
    from werkzeug.utils import secure_filename

    if not isinstance(filename, str):
        return None
    cleaned = filename.strip().replace("\\", "/")
    if not cleaned:
        return None
    leaf_name = PurePosixPath(cleaned).name
    safe_name = secure_filename(leaf_name)
    if not safe_name:
        return None
    if len(safe_name) > 180:
        if "." in safe_name:
            stem, ext = safe_name.rsplit(".", 1)
            max_stem = max(1, 180 - len(ext) - 1)
            safe_name = f"{stem[:max_stem]}.{ext[:32]}"
        else:
            safe_name = safe_name[:180]
    return safe_name


def _jira_decode_attachment_bytes(content_base64: Any) -> bytes | None:
    import base64
    import binascii

    if not isinstance(content_base64, str):
        return None
    raw = content_base64.strip()
    if not raw:
        return None

    if raw.lower().startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1].strip()

    compact = "".join(raw.split())
    if not compact:
        return None

    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        try:
            padding = "=" * (-len(compact) % 4)
            return base64.urlsafe_b64decode(compact + padding)
        except (binascii.Error, ValueError):
            return None


def _jira_search(**kwargs):
    import asyncio
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    jql = kwargs.get("jql")
    if not jql:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: jql",
            details={"missing": ["jql"]},
            suggestions=["Provide a JQL query string"],
        )

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
        return make_error_response(
            "jira_proxy_error",
            str(exc),
            details={"exception_type": "JiraProxyError"},
            suggestions=["Check Jira connectivity and authentication"],
        )


def _jira_get_issue(**kwargs):
    import asyncio
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    issue_key = kwargs.get("issue_key")
    if not issue_key:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: issue_key",
            details={"missing": ["issue_key"]},
            suggestions=["Provide a Jira issue key (e.g., PROJ-123)"],
        )

    async def _async_get_issue():
        proxy = await get_jira_proxy()
        return await proxy.get_issue(issue_key=issue_key, fields=kwargs.get("fields"))

    try:
        return _run_async_compat(_async_get_issue)
    except JiraProxyError as exc:
        return make_error_response(
            "jira_proxy_error",
            str(exc),
            details={"exception_type": "JiraProxyError"},
            suggestions=["Check Jira connectivity and authentication"],
        )


def _jira_get_transitions(**kwargs):
    import asyncio
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    issue_key = kwargs.get("issue_key")
    if not issue_key:
        return make_error_response(
            "missing_parameter",
            "Missing required parameter: issue_key",
            details={"missing": ["issue_key"]},
            suggestions=["Provide a Jira issue key (e.g., PROJ-123)"],
        )

    async def _async_get_transitions():
        proxy = await get_jira_proxy()
        return await proxy.get_transitions(issue_key=issue_key)

    try:
        return _run_async_compat(_async_get_transitions)
    except JiraProxyError as exc:
        return make_error_response(
            "jira_proxy_error",
            str(exc),
            details={"exception_type": "JiraProxyError"},
            suggestions=["Check Jira connectivity and authentication"],
        )


def _jira_add_comment(**kwargs):
    import asyncio
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    issue_key = kwargs.get("issue_key")
    comment = kwargs.get("comment")
    if not issue_key or not comment:
        return make_error_response(
            "MISSING_PARAMS",
            "Missing required parameters: issue_key and comment",
            suggestions=["Provide both issue_key (e.g. 'PROJ-123') and comment text"],
        )

    async def _async_comment():
        proxy = await get_jira_proxy()
        return await proxy.add_comment(issue_key=issue_key, comment=comment)

    try:
        return _run_async_compat(_async_comment)
    except JiraProxyError as exc:
        return make_error_response("JIRA_ERROR", str(exc))


def _jira_add_attachment(**kwargs):
    import asyncio
    import base64
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    issue_key = kwargs.get("issue_key")
    filename = kwargs.get("filename")
    content_base64 = kwargs.get("content_base64")
    mime_type = kwargs.get("mime_type")

    missing: list[str] = []
    if not issue_key:
        missing.append("issue_key")
    if not filename:
        missing.append("filename")
    if not content_base64:
        missing.append("content_base64")
    if not mime_type:
        missing.append("mime_type")
    if missing:
        return make_error_response(
            "missing_parameter",
            f"Missing required parameters: {', '.join(missing)}",
            details={"missing": missing},
            suggestions=[
                "Provide issue_key, filename, content_base64, and mime_type"
            ],
        )

    issue_key_str = str(issue_key).strip()
    if not _jira_project_from_issue_key(issue_key_str):
        return make_error_response(
            "invalid_issue_key",
            "Invalid issue_key format; expected PROJECT-123",
            suggestions=["Use the format PROJECT-123 for issue keys"],
        )

    safe_filename = _jira_sanitise_attachment_filename(filename)
    if not safe_filename:
        return make_error_response(
            "invalid_filename",
            "Invalid filename; provide a safe non-empty file name",
            suggestions=[
                "Use a simple filename such as diagram.png",
                "Avoid path separators and control characters",
            ],
        )

    mime_type_str = str(mime_type).strip().lower()
    if not mime_type_str:
        return make_error_response(
            "invalid_mime_type",
            "mime_type must be a non-empty string",
            suggestions=["Provide a MIME type such as image/png"],
        )

    allowed_mime_types = _jira_attachment_allowed_mime_types()
    if mime_type_str not in allowed_mime_types:
        return make_error_response(
            "unsupported_mime_type",
            f"mime_type '{mime_type_str}' is not allowed",
            details={"allowed_mime_types": sorted(allowed_mime_types)},
            suggestions=[
                "Use an allow-listed MIME type",
                "Configure VON_INTERNAL_MCP_JIRA_ATTACHMENT_ALLOWED_MIME_TYPES if needed",
            ],
        )

    attachment_bytes = _jira_decode_attachment_bytes(content_base64)
    if attachment_bytes is None:
        return make_error_response(
            "invalid_base64",
            "content_base64 is not valid base64-encoded file data",
            suggestions=[
                "Provide raw base64 data or a data URL with a base64 payload",
            ],
        )

    if len(attachment_bytes) == 0:
        return make_error_response(
            "empty_attachment",
            "Attachment content is empty",
            suggestions=["Provide a non-empty file payload"],
        )

    max_size_bytes = _jira_attachment_max_size_bytes()
    if len(attachment_bytes) > max_size_bytes:
        return make_error_response(
            "attachment_too_large",
            (
                f"Attachment size {len(attachment_bytes)} bytes exceeds limit "
                f"{max_size_bytes} bytes"
            ),
            details={
                "size_bytes": len(attachment_bytes),
                "max_size_bytes": max_size_bytes,
            },
            suggestions=[
                "Upload a smaller file",
                "Adjust VON_INTERNAL_MCP_JIRA_ATTACHMENT_MAX_SIZE_BYTES if appropriate",
            ],
        )

    comment = kwargs.get("comment")
    if comment is not None and not isinstance(comment, str):
        return make_error_response(
            "invalid_parameter",
            "comment must be a string when provided",
            details={"parameter": "comment"},
        )
    comment_text = comment.strip() if isinstance(comment, str) else None
    if comment_text == "":
        comment_text = None

    normalised_content_base64 = base64.b64encode(attachment_bytes).decode("ascii")

    async def _async_add_attachment():
        proxy = await get_jira_proxy()
        return await proxy.add_attachment(
            issue_key=issue_key_str,
            filename=safe_filename,
            content_base64=normalised_content_base64,
            mime_type=mime_type_str,
            comment=comment_text,
        )

    try:
        raw_result = _run_async_compat(_async_add_attachment)
    except JiraProxyError as exc:
        return make_error_response(
            "jira_proxy_error",
            str(exc),
            details={"exception_type": "JiraProxyError"},
            suggestions=["Check Jira connectivity and authentication"],
        )

    if isinstance(raw_result, dict) and raw_result.get("success") is False:
        return raw_result

    attachment_payload: dict[str, Any] | None = None
    comment_added = False
    first_item_from_list: dict[str, Any] | None = None

    if isinstance(raw_result, list):
        for item in raw_result:
            if isinstance(item, dict):
                first_item_from_list = item
                break
        attachment_payload = first_item_from_list
    elif isinstance(raw_result, dict):
        attachments = raw_result.get("attachments")
        if isinstance(attachments, list):
            for item in attachments:
                if isinstance(item, dict):
                    first_item_from_list = item
                    break
        if first_item_from_list is not None:
            attachment_payload = first_item_from_list
        elif isinstance(raw_result.get("attachment"), dict):
            attachment_payload = raw_result.get("attachment")
        elif isinstance(raw_result.get("id"), (str, int)):
            attachment_payload = raw_result
        comment_added = bool(raw_result.get("comment_added"))

    if not isinstance(attachment_payload, dict):
        return make_error_response(
            "jira_proxy_error",
            "Jira attachment upload returned an unexpected response shape",
            details={"response_type": type(raw_result).__name__},
            suggestions=["Check Jira proxy/tool output for jira_add_attachment"],
        )

    attachment_id_raw = attachment_payload.get("id") or attachment_payload.get(
        "attachment_id"
    )
    attachment_filename = attachment_payload.get("filename") or safe_filename
    attachment_size_raw = attachment_payload.get("size") or attachment_payload.get(
        "size_bytes"
    )
    attachment_content_type = (
        attachment_payload.get("mimeType")
        or attachment_payload.get("contentType")
        or attachment_payload.get("content_type")
        or mime_type_str
    )

    attachment_size: int | None = None
    if isinstance(attachment_size_raw, int):
        attachment_size = attachment_size_raw
    elif isinstance(attachment_size_raw, str) and attachment_size_raw.strip().isdigit():
        attachment_size = int(attachment_size_raw.strip())
    else:
        attachment_size = len(attachment_bytes)

    return {
        "success": True,
        "issue_key": issue_key_str,
        "attachment_id": str(attachment_id_raw) if attachment_id_raw is not None else None,
        "filename": str(attachment_filename),
        "size_bytes": attachment_size,
        "content_type": str(attachment_content_type),
        "comment_added": comment_added,
        "jira_attachment": attachment_payload,
    }


def _jira_transition_issue(**kwargs):
    import asyncio
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    issue_key = kwargs.get("issue_key")
    transition_id = kwargs.get("transition_id")
    if not issue_key or not transition_id:
        return make_error_response(
            "MISSING_PARAMS",
            "Missing required parameters: issue_key and transition_id",
            suggestions=["Use jira_get_transitions first to find available IDs"],
        )

    async def _async_transition():
        proxy = await get_jira_proxy()
        return await proxy.transition_issue(
            issue_key=issue_key, transition_id=transition_id
        )

    try:
        return _run_async_compat(_async_transition)
    except JiraProxyError as exc:
        return make_error_response("JIRA_ERROR", str(exc))


def _jira_create_issue(**kwargs):
    import asyncio
    import logging
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    logger = logging.getLogger(__name__)

    project_key = kwargs.get("project_key")
    issue_type = kwargs.get("issue_type")
    summary = kwargs.get("summary")

    if not project_key or not issue_type or not summary:
        return make_error_response(
            "MISSING_PARAMS",
            "Missing required parameters: project_key, issue_type, summary",
            suggestions=[
                "Provide project_key (e.g. 'PROJ'), issue_type (e.g. 'Task'), and summary text"
            ],
        )

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

    components = kwargs.get("components")
    if isinstance(components, list):
        normalised_components: list[dict[str, str]] = []
        for component in components:
            if isinstance(component, str) and component.strip():
                normalised_components.append({"name": component.strip()})
                continue
            if not isinstance(component, dict):
                continue

            normalised_component: dict[str, str] = {}
            name = component.get("name")
            if isinstance(name, str) and name.strip():
                normalised_component["name"] = name.strip()

            component_id = component.get("id")
            if isinstance(component_id, (str, int)) and str(component_id).strip():
                normalised_component["id"] = str(component_id).strip()

            if normalised_component:
                normalised_components.append(normalised_component)

        if normalised_components:
            payload["fields"]["components"] = normalised_components

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
        return make_error_response("JIRA_ERROR", str(exc))


def _jira_update_issue(**kwargs):
    import logging
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    logger = logging.getLogger(__name__)

    issue_key = kwargs.get("issue_key")
    update_fields = kwargs.get("update_fields")
    if not issue_key or not isinstance(update_fields, dict):
        return make_error_response(
            "MISSING_PARAMS",
            "Missing required parameters: issue_key and update_fields (dict)",
            suggestions=[
                "Provide issue_key and update_fields as a dict of field names to values"
            ],
        )

    issue_key_str = str(issue_key).strip()
    project_key = _jira_project_from_issue_key(issue_key_str)
    if not project_key:
        return make_error_response(
            "INVALID_ISSUE_KEY",
            "Invalid issue_key format; expected PROJECT-123",
            suggestions=["Use the format PROJECT-123 for issue keys"],
        )

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
        return make_error_response(
            "NO_UPDATABLE_FIELDS",
            "No updatable fields provided (project/key/id are not allowed)",
            suggestions=["Provide fields like summary, description, assignee, etc."],
        )

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
        return make_error_response("JIRA_ERROR", str(exc))


def _jira_link_issue(**kwargs):
    import logging
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    logger = logging.getLogger(__name__)

    link_type = kwargs.get("link_type")

    def _clean_issue_key_param(name: str) -> str | None:
        value = kwargs.get(name)
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    source_key = _clean_issue_key_param("source_issue_key")
    target_key = _clean_issue_key_param("target_issue_key")
    inward_key_from_kwargs = _clean_issue_key_param("inward_issue_key")
    outward_key_from_kwargs = _clean_issue_key_param("outward_issue_key")

    # Prefer semantic aliases to avoid accidental direction inversions.
    if source_key is not None or target_key is not None:
        if not source_key or not target_key:
            return make_error_response(
                "MISSING_PARAMS",
                "Missing required parameters: source_issue_key, target_issue_key, link_type",
                suggestions=[
                    "Provide both source_issue_key and target_issue_key, or use inward_issue_key and outward_issue_key."
                ],
            )
        if inward_key_from_kwargs and inward_key_from_kwargs != source_key:
            return make_error_response(
                "CONFLICTING_PARAMS",
                "Conflicting parameters: source_issue_key does not match inward_issue_key",
                suggestions=[
                    "Use only one key pair style, or provide matching values for source_issue_key/inward_issue_key."
                ],
                details={
                    "source_issue_key": source_key,
                    "inward_issue_key": inward_key_from_kwargs,
                },
            )
        if outward_key_from_kwargs and outward_key_from_kwargs != target_key:
            return make_error_response(
                "CONFLICTING_PARAMS",
                "Conflicting parameters: target_issue_key does not match outward_issue_key",
                suggestions=[
                    "Use only one key pair style, or provide matching values for target_issue_key/outward_issue_key."
                ],
                details={
                    "target_issue_key": target_key,
                    "outward_issue_key": outward_key_from_kwargs,
                },
            )
        inward_key = source_key
        outward_key = target_key
    else:
        if not inward_key_from_kwargs or not outward_key_from_kwargs or not link_type:
            return make_error_response(
                "MISSING_PARAMS",
                "Missing required parameters: link_type and one issue key pair (source/target or inward/outward)",
                suggestions=[
                    "Preferred: source_issue_key + target_issue_key + link_type.",
                    "Backward-compatible: inward_issue_key + outward_issue_key + link_type.",
                ],
            )
        inward_key = inward_key_from_kwargs
        outward_key = outward_key_from_kwargs
        source_key = inward_key
        target_key = outward_key

    if not link_type:
        return make_error_response(
            "MISSING_PARAMS",
            "Missing required parameter: link_type",
            suggestions=["Provide a link type (e.g. 'Blocks', 'Relates')."],
        )

    inward_project = _jira_project_from_issue_key(inward_key)
    outward_project = _jira_project_from_issue_key(outward_key)
    if not inward_project or not outward_project:
        return make_error_response(
            "INVALID_ISSUE_KEY",
            "Invalid issue key format; expected PROJECT-123",
            suggestions=["Use the format PROJECT-123 for issue keys"],
        )

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
            "source_issue_key": source_key,
            "target_issue_key": target_key,
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
            "[jira_write] link_issue source=%s target=%s jira_outward=%s jira_inward=%s (%s)",
            source_key,
            target_key,
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
            result["source_issue_key"] = source_key
            result["target_issue_key"] = target_key
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
        return make_error_response("JIRA_ERROR", str(exc))


def _jira_delete_issue_link(**kwargs):
    import logging
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    logger = logging.getLogger(__name__)

    issue_link_id_raw = kwargs.get("issue_link_id")
    issue_link_id = (
        str(issue_link_id_raw).strip() if issue_link_id_raw is not None else ""
    )
    if not issue_link_id:
        return make_error_response(
            "MISSING_PARAMS",
            "Missing required parameter: issue_link_id",
            suggestions=["Provide the Jira issue link ID to delete."],
        )

    if not issue_link_id.isdigit():
        return make_error_response(
            "INVALID_ISSUE_LINK_ID",
            "Invalid issue_link_id format; expected a numeric Jira link ID",
            suggestions=["Use a numeric link ID from Jira issue link metadata."],
        )

    def _clean_issue_key_param(name: str) -> str | None:
        value = kwargs.get(name)
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    source_key = _clean_issue_key_param("source_issue_key")
    target_key = _clean_issue_key_param("target_issue_key")

    source_project = (
        _jira_project_from_issue_key(source_key) if isinstance(source_key, str) else None
    )
    if source_key and not source_project:
        return make_error_response(
            "INVALID_ISSUE_KEY",
            "Invalid source_issue_key format; expected PROJECT-123",
            suggestions=["Use the format PROJECT-123 for source_issue_key."],
        )

    target_project = (
        _jira_project_from_issue_key(target_key) if isinstance(target_key, str) else None
    )
    if target_key and not target_project:
        return make_error_response(
            "INVALID_ISSUE_KEY",
            "Invalid target_issue_key format; expected PROJECT-123",
            suggestions=["Use the format PROJECT-123 for target_issue_key."],
        )

    project_keys: list[str] = []
    if source_project:
        project_keys.append(source_project)
    if target_project and target_project not in project_keys:
        project_keys.append(target_project)

    if not project_keys:
        return make_error_response(
            "MISSING_PARAMS",
            "Missing allow-list context: provide source_issue_key and/or target_issue_key",
            suggestions=[
                "Include source_issue_key and/or target_issue_key so project allow-list guardrails can be enforced."
            ],
        )

    dry_run = bool(kwargs.get("dry_run", True))
    approved = bool(kwargs.get("approved", False))
    execute = bool(kwargs.get("execute", False))
    request_id = kwargs.get("request_id")

    guardrail_error = _jira_write_guardrails(
        action="delete_issue_link",
        project_keys=project_keys,
        dry_run=dry_run,
        approved=approved,
        execute=execute,
    )
    if guardrail_error is not None:
        return guardrail_error

    if isinstance(request_id, str) and request_id.strip() and not dry_run:
        cached = _jira_cache_get("jira_delete_issue_link", request_id.strip())
        if cached is not None:
            cached["reused"] = True
            return cached

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "executed": False,
            "action": "delete_issue_link",
            "issue_link_id": issue_link_id,
            "source_issue_key": source_key,
            "target_issue_key": target_key,
            "proposed_endpoint": f"/rest/api/3/issueLink/{issue_link_id}",
        }

    async def _async_delete():
        proxy = await get_jira_proxy()
        return await proxy.delete_issue_link(issue_link_id=issue_link_id)

    try:
        logger.info(
            "[jira_write] delete_issue_link link_id=%s source=%s target=%s",
            issue_link_id,
            source_key,
            target_key,
        )
        result = _run_async_compat(_async_delete)
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("success", True)
            result["dry_run"] = False
            result["executed"] = True
            result["action"] = "delete_issue_link"
            result["issue_link_id"] = issue_link_id
            result["source_issue_key"] = source_key
            result["target_issue_key"] = target_key
            if isinstance(request_id, str) and request_id.strip():
                _jira_cache_set("jira_delete_issue_link", request_id.strip(), result)
            return result
        return {
            "success": True,
            "dry_run": False,
            "executed": True,
            "action": "delete_issue_link",
            "issue_link_id": issue_link_id,
            "result": result,
        }
    except JiraProxyError as exc:
        return make_error_response("JIRA_ERROR", str(exc))


def _jira_get_myself(**kwargs):
    import asyncio
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError

    async def _async_get_myself():
        proxy = await get_jira_proxy()
        return await proxy.get_myself()

    try:
        return _run_async_compat(_async_get_myself)
    except JiraProxyError as exc:
        return make_error_response(
            "jira_proxy_error",
            str(exc),
            details={"exception_type": "JiraProxyError"},
            suggestions=["Check Jira connectivity and authentication"],
        )


def _jira_get_auth_config(**kwargs):
    from .jira_proxy_mcp import inspect_jira_auth_config

    return inspect_jira_auth_config()


def _jira_hygiene_discover(**kwargs):
    from ...services.jira_hygiene_service import discover_jira_hygiene

    return discover_jira_hygiene(
        search_issues=lambda **search_kwargs: _jira_search(**search_kwargs),
        project_key=kwargs.get("project_key"),
        candidate_epic_keys=kwargs.get("candidate_epic_keys"),
        max_issues=kwargs.get("max_issues"),
        max_epics=kwargs.get("max_epics"),
        include_cross_cutting=_coerce_bool_input(
            kwargs.get("include_cross_cutting"),
            default=True,
        ),
        include_in_progress_candidates=_coerce_bool_input(
            kwargs.get("include_in_progress_candidates"),
            default=True,
        ),
    )


def _jira_hygiene_propose(**kwargs):
    from ...services.jira_hygiene_service import propose_jira_hygiene_plan

    discovery_payload = kwargs.get("discovery_payload")
    if not isinstance(discovery_payload, dict):
        discovery_payload = {}

    return propose_jira_hygiene_plan(
        epic_catalogue=kwargs.get("epic_catalogue")
        or discovery_payload.get("epic_catalogue"),
        orphan_candidates=kwargs.get("orphan_candidates")
        or discovery_payload.get("orphan_candidates"),
        cross_cutting_candidates=kwargs.get("cross_cutting_candidates")
        or discovery_payload.get("cross_cutting_candidates"),
        in_progress_candidates=kwargs.get("in_progress_candidates")
        or discovery_payload.get("in_progress_candidates"),
        batch_size=kwargs.get("batch_size"),
    )


def _jira_hygiene_check_approval(**kwargs):
    from ...services.jira_hygiene_service import check_jira_hygiene_approval

    proposal_payload = kwargs.get("proposal_payload")
    if not isinstance(proposal_payload, dict):
        proposal_payload = {}

    return check_jira_hygiene_approval(
        ready_to_execute=kwargs.get("ready_to_execute")
        or proposal_payload.get("ready_to_execute"),
        execution_mode=kwargs.get("execution_mode"),
        approved=kwargs.get("approved"),
        excluded_issue_keys=kwargs.get("excluded_issue_keys"),
        overrides=kwargs.get("overrides"),
        batch_size=kwargs.get("batch_size"),
    )


def _jira_hygiene_execute_batches(**kwargs):
    from ...services.jira_hygiene_service import execute_jira_hygiene_batches

    approval_payload = kwargs.get("approval_payload")
    if not isinstance(approval_payload, dict):
        approval_payload = {}

    approval_decision = approval_payload.get("approval_decision")
    approved_from_payload = (
        approval_decision.get("approved")
        if isinstance(approval_decision, dict)
        else None
    )

    return execute_jira_hygiene_batches(
        update_issue=lambda **update_kwargs: _jira_update_issue(**update_kwargs),
        add_comment=lambda **comment_kwargs: _jira_add_comment(**comment_kwargs),
        approved_operations=kwargs.get("approved_operations")
        or approval_payload.get("approved_operations"),
        execution_mode=kwargs.get("execution_mode")
        or approval_payload.get("execution_mode"),
        approved=kwargs.get("approved")
        if kwargs.get("approved") is not None
        else approved_from_payload,
        batch_size=kwargs.get("batch_size")
        or approval_payload.get("resolved_batch_size"),
        max_retries=kwargs.get("max_retries"),
        retry_backoff_seconds=kwargs.get("retry_backoff_seconds"),
    )


def _jira_hygiene_emit_audit(**kwargs):
    from ...services.jira_hygiene_service import emit_jira_hygiene_audit

    return emit_jira_hygiene_audit(
        add_comment=lambda **comment_kwargs: _jira_add_comment(**comment_kwargs),
        project_key=kwargs.get("project_key"),
        execution_mode=kwargs.get("execution_mode"),
        proposal_summary=kwargs.get("proposal_summary"),
        execution_summary=kwargs.get("execution_summary"),
        approved_operations=kwargs.get("approved_operations"),
        needs_decision=kwargs.get("needs_decision"),
        emit_epic_comments=kwargs.get("emit_epic_comments"),
    )


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
        return make_error_response(
            "missing_parameter",
            "namespace is required",
            details={"missing": ["namespace"]},
            suggestions=["Provide a user namespace for prompt context"],
        )

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

    orchestrator_cls = _get_internal_mcp_chat_orchestrator_cls()
    template_service = PromptTemplateService()
    classifier_prompt_id, classifier_prompt_text = template_service.resolve_prompt_text(
        orchestrator_cls._MISSING_TOOL_CLASSIFIER_PROMPTS,
        fallback=orchestrator_cls._FALLBACK_MISSING_TOOL_CALL_PROMPT,
        max_chars=max_chars_int,
    )
    retry_prompt_id, retry_prompt_text = template_service.resolve_prompt_text(
        orchestrator_cls._MISSING_TOOL_RETRY_PROMPTS,
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
    import os

    from src.backend.languagemodels.llm_interface import get_active_model_name
    from src.backend.services.feature_flags import (
        get_durable_workflows_enabled,
        get_event_workflow_integration_enabled,
    )
    from src.backend.services.settings_service import (
        get_setting,
        resolve_llm_setting,
    )

    def _env_flag(name: str, *, default: str = "0") -> bool:
        value = os.getenv(name, default)
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _is_sensitive_key_name(key_name: str) -> bool:
        lowered = key_name.strip().lower()
        if not lowered:
            return False
        if "env_var" in lowered:
            return False
        if lowered in {
            "provider",
            "model",
            "scope",
            "user_concept_id",
            "organisation_concept_id",
            "organization_concept_id",
        }:
            return False
        sensitive_markers = (
            "token",
            "secret",
            "password",
            "passwd",
            "api_key",
            "apikey",
            "private_key",
            "credential",
            "cookie",
            "bearer",
        )
        return any(marker in lowered for marker in sensitive_markers)

    def _to_presence_bool(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, set, dict)):
            return len(value) > 0
        return bool(value)

    def _sanitise_for_introspection(value: Any, *, depth: int = 0) -> Any:
        """Redact sensitive values by converting them to presence booleans."""
        if depth >= 8:
            return None
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for key, item in value.items():
                key_str = str(key)
                if _is_sensitive_key_name(key_str):
                    cleaned[key_str] = _to_presence_bool(item)
                else:
                    cleaned[key_str] = _sanitise_for_introspection(
                        item, depth=depth + 1
                    )
            return cleaned
        if isinstance(value, list):
            return [_sanitise_for_introspection(item, depth=depth + 1) for item in value]
        if isinstance(value, tuple):
            return tuple(
                _sanitise_for_introspection(item, depth=depth + 1) for item in value
            )
        return value

    if not namespace or not isinstance(namespace, str) or not namespace.strip():
        return make_error_response(
            "missing_parameter",
            "namespace is required",
            details={"missing": ["namespace"]},
            suggestions=["Provide a user namespace for chat introspection"],
        )

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
        resolved_llm = resolve_llm_setting(
            user_concept_id=namespace, org_concept_id=organisation_concept_id
        )
    except Exception:
        resolved_llm = None
    resolved_llm = _sanitise_for_introspection(resolved_llm)

    configured_openai_api_key_env_var = None
    try:
        configured_openai_api_key_env_var = get_setting("openai_api_key_env_var")
        if not isinstance(configured_openai_api_key_env_var, str):
            configured_openai_api_key_env_var = None
    except Exception:
        configured_openai_api_key_env_var = None

    sensitive_env_keys = {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "XAI_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "JIRA_API_TOKEN",
        "ATLASSIAN_API_TOKEN",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "FLASK_SECRET_KEY",
        "MONGO_URI",
        "VON_MONGO_URI",
    }
    if configured_openai_api_key_env_var:
        sensitive_env_keys.add(configured_openai_api_key_env_var)
    sensitive_env_presence = {
        key: bool(os.getenv(key)) for key in sorted(k for k in sensitive_env_keys if k)
    }
    sensitive_env_present_count = sum(
        1 for is_present in sensitive_env_presence.values() if is_present
    )

    gateway_enabled = None
    orchestrator_max_tool_invocations = None
    orchestrator_tool_batch_cap = None
    orchestrator_missing_tool_call_retry_cap = None
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
            orchestrator_missing_tool_call_retry_cap = getattr(
                orchestrator,
                "_max_missing_tool_call_retries_per_turn",
                None,
            )
        except Exception:
            gateway_enabled = None
            orchestrator_max_tool_invocations = None
            orchestrator_tool_batch_cap = None
            orchestrator_missing_tool_call_retry_cap = None

    workflow_selector_enabled = _env_flag(
        "VON_CHAT_WORKFLOW_SELECTOR_ENABLED", default="1"
    )
    workflow_trace_enabled = _env_flag("VON_WORKFLOWS_TRACE_ENABLED", default="0")
    critic_enabled = _env_flag("VON_CRITIC_ENABLE", default="0")
    deterministic_introspection_enabled = _env_flag(
        "VON_DETERMINISTIC_INTROSPECTION", default="0"
    )
    workflow_model_policy_enabled = _env_flag(
        "VON_WORKFLOW_MODEL_POLICY_ENABLE", default="0"
    )
    write_tools_enabled = _env_flag("VON_MCP_ALLOW_WRITES", default="0")
    durable_workflows_enabled = get_durable_workflows_enabled(default=False)
    event_workflow_integration_enabled = get_event_workflow_integration_enabled(
        default=True
    )
    jira_execute_mode_enabled = _env_flag(
        "VON_INTERNAL_MCP_JIRA_EXECUTE_MODE", default="0"
    )
    event_workflow_bindings_detailed: list[dict[str, Any]] = []
    try:
        from src.backend.services.workflow_event_integration_service import (
            list_event_workflow_bindings,
        )

        event_workflow_bindings_detailed = list_event_workflow_bindings(
            include_env_fallback=True,
            limit=200,
        )
    except Exception:
        event_workflow_bindings_detailed = []

    # Backwards-compatible compact shape:
    # - one workflow per event -> string
    # - multiple workflows per event -> list[str]
    event_workflow_bindings: dict[str, Any] = {}
    for binding in event_workflow_bindings_detailed:
        event_type = str(binding.get("event_type") or "").strip()
        workflow_id = str(binding.get("workflow_id") or "").strip()
        if not event_type or not workflow_id:
            continue
        existing = event_workflow_bindings.get(event_type)
        if existing is None:
            event_workflow_bindings[event_type] = workflow_id
            continue
        if isinstance(existing, list):
            if workflow_id not in existing:
                existing.append(workflow_id)
            continue
        if existing != workflow_id:
            event_workflow_bindings[event_type] = [existing, workflow_id]

    if gateway_enabled is False:
        inferred_runtime_mode = "llm_only"
    elif (
        isinstance(orchestrator_max_tool_invocations, int)
        and orchestrator_max_tool_invocations <= 0
    ):
        inferred_runtime_mode = "llm_only"
    elif workflow_selector_enabled:
        inferred_runtime_mode = "workflow_routed_tool_calling"
    else:
        inferred_runtime_mode = "legacy_tool_calling"

    workflow_mode = {
        "runtime_mode": inferred_runtime_mode,
        "workflow_selector_enabled": workflow_selector_enabled,
        "deterministic_introspection_enabled": deterministic_introspection_enabled,
        "workflow_trace_enabled": workflow_trace_enabled,
        "critic_enabled": critic_enabled,
        "workflow_model_policy_enabled": workflow_model_policy_enabled,
        "write_tools_enabled": write_tools_enabled,
        "durable_workflows_enabled": durable_workflows_enabled,
        "event_workflow_integration_enabled": event_workflow_integration_enabled,
        "jira_execute_mode_enabled": jira_execute_mode_enabled,
    }

    # Tool-guidance fingerprint (stable-ish) without dumping full text by default
    tool_guidance_text = ""
    tool_guidance_hash = None
    tool_guidance_preview = None

    try:
        # Prefer the live orchestrator (includes the real tool listing) when available.
        orchestrator_cls = _get_internal_mcp_chat_orchestrator_cls()
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

            dummy_orchestrator = orchestrator_cls(gateway=_StubGateway())  # type: ignore[arg-type]
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

    # Keep these keys aligned with the MethodDefinition output_schema for
    # chat_introspect: InternalMCPGateway validates success payloads end-to-end.
    return {
        "success": True,
        "introspection_version": "v2",
        "namespace": namespace,
        "organisation_concept_id": organisation_concept_id,
        "active_model_name": active_model_name,
        "active_llm": resolved_llm,  # Resolved LLM shape with sensitive keys redacted to booleans.
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
        "orchestrator_missing_tool_call_retry_cap": orchestrator_missing_tool_call_retry_cap,
        "workflow_mode": workflow_mode,
        "configured_openai_api_key_env_var": configured_openai_api_key_env_var,
        "configured_openai_api_key_env_var_present": (
            bool(
                configured_openai_api_key_env_var
                and os.getenv(configured_openai_api_key_env_var)
            )
        ),
        "event_workflow_bindings": event_workflow_bindings,
        "event_workflow_bindings_detailed": event_workflow_bindings_detailed,
        "sensitive_env_presence": sensitive_env_presence,
        "sensitive_env_present_count": sensitive_env_present_count,
    }


# --------------------------------------------------------------------------- #
# Task management MCP handlers (JVNAUTOSCI-1040)
# --------------------------------------------------------------------------- #


def _parse_optional_iso_datetime_param(
    value: Any,
    *,
    field_name: str,
) -> tuple[datetime | None, dict[str, Any] | None]:
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, make_error_response(
            "INVALID_DATE",
            f"Invalid {field_name} format: {value}",
            suggestions=[
                f"Use ISO format for {field_name} (for example '2025-12-31' or '2025-12-31T23:59:59Z')"
            ],
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None, make_error_response(
            "INVALID_DATE",
            f"Invalid {field_name} format: {value}",
            suggestions=[
                f"Use ISO format for {field_name} (for example '2025-12-31' or '2025-12-31T23:59:59Z')"
            ],
        )
    return parsed, None


def _task_create(**kwargs):
    """Create a new task via the task management service."""
    from ...services.task_management_service import (
        InvalidTaskDataError,
        TaskManagementError,
        create_task,
    )

    title = kwargs.get("title")
    description = kwargs.get("description")
    if not title or not isinstance(title, str) or not title.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: title",
            suggestions=["Provide a non-empty title string for the task"],
        )
    if not description or not isinstance(description, str) or not description.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: description",
            suggestions=["Provide a non-empty description string for the task"],
        )

    assignee_id = kwargs.get("assignee_id") or kwargs.get("assignee_concept_id")
    session_id = kwargs.get("originating_session_id") or kwargs.get("session_id")
    created_by = kwargs.get("created_by_concept_id") or kwargs.get("namespace")
    priority = kwargs.get("priority", "medium")
    org_id = kwargs.get("organisation_concept_id")
    epic_task_concept_id = kwargs.get("epic_task_concept_id")
    components = kwargs.get("components")
    fix_versions = kwargs.get("fix_versions")
    sprint_values = kwargs.get("sprint_values")
    backlog_rank = kwargs.get("backlog_rank")

    start_date, start_error = _parse_optional_iso_datetime_param(
        kwargs.get("start_date"),
        field_name="start_date",
    )
    if start_error:
        return start_error
    due_date, due_error = _parse_optional_iso_datetime_param(
        kwargs.get("due_date"),
        field_name="due_date",
    )
    if due_error:
        return due_error

    try:
        result = create_task(
            title=title.strip(),
            description=description.strip(),
            assignee_concept_id=assignee_id,
            originating_session_id=session_id,
            created_by_concept_id=created_by,
            start_date=start_date,
            due_date=due_date,
            epic_task_concept_id=epic_task_concept_id,
            components=components,
            fix_versions=fix_versions,
            sprint_values=sprint_values,
            backlog_rank=backlog_rank,
            priority=priority,
            organisation_concept_id=org_id,
        )
        result["success"] = True
        return result
    except InvalidTaskDataError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except TaskManagementError as exc:
        return make_error_response("TASK_ERROR", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_get(**kwargs):
    """Get a task by concept_id."""
    from ...services.task_management_service import TaskNotFoundError, get_task

    task_id = kwargs.get("task_concept_id") or kwargs.get("task_id")
    if not task_id or not isinstance(task_id, str) or not task_id.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide the task's concept_id"],
        )

    try:
        result = get_task(task_id.strip())
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_list(**kwargs):
    """List tasks with optional filters."""
    from ...services.task_management_service import list_tasks, get_tasks_for_user

    user_id = kwargs.get("user_concept_id") or kwargs.get("assignee_id")
    status = kwargs.get("status_filter") or kwargs.get("status")
    priority = kwargs.get("priority_filter") or kwargs.get("priority")
    org_id = kwargs.get("organisation_concept_id")
    limit = kwargs.get("limit", 50)

    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50

    try:
        if user_id and isinstance(user_id, str) and user_id.strip():
            # Get tasks for a specific user
            tasks = get_tasks_for_user(
                user_id.strip(),
                status_filter=status if isinstance(status, str) else None,
                include_created=bool(kwargs.get("include_created", True)),
            )
        else:
            # List tasks with filters
            tasks = list_tasks(
                organisation_concept_id=org_id,
                status_filter=status if isinstance(status, str) else None,
                priority_filter=priority if isinstance(priority, str) else None,
                limit=limit,
            )
        return {"success": True, "tasks": tasks, "count": len(tasks)}
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_update_status(**kwargs):
    """Update a task's status."""
    from ...services.task_management_service import (
        InvalidTaskDataError,
        TaskNotFoundError,
        update_task_status,
    )

    task_id = kwargs.get("task_concept_id") or kwargs.get("task_id")
    status = kwargs.get("status")

    if not task_id or not isinstance(task_id, str) or not task_id.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide the task's concept_id"],
        )
    if not status or not isinstance(status, str) or not status.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: status",
            suggestions=["Provide status (e.g. 'pending', 'in_progress', 'completed')"],
        )

    try:
        result = update_task_status(task_id.strip(), status.strip())
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except InvalidTaskDataError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_assign(**kwargs):
    """Assign a task to a user."""
    from ...services.task_management_service import (
        InvalidTaskDataError,
        TaskNotFoundError,
        assign_task,
    )

    task_id = kwargs.get("task_concept_id") or kwargs.get("task_id")
    assignee_id = kwargs.get("assignee_concept_id") or kwargs.get("assignee_id")

    if not task_id or not isinstance(task_id, str) or not task_id.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide the task's concept_id"],
        )
    if not assignee_id or not isinstance(assignee_id, str) or not assignee_id.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: assignee_concept_id",
            suggestions=["Provide the assignee's concept_id"],
        )

    try:
        result = assign_task(task_id.strip(), assignee_id.strip())
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except InvalidTaskDataError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_delete(**kwargs):
    """Delete (cancel) a task."""
    from ...services.task_management_service import TaskNotFoundError, delete_task

    task_id = kwargs.get("task_concept_id") or kwargs.get("task_id")

    if not task_id or not isinstance(task_id, str) or not task_id.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide the task's concept_id"],
        )

    try:
        deleted = delete_task(task_id.strip())
        return {"success": True, "deleted": deleted, "task_concept_id": task_id.strip()}
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _resolve_task_identifier(payload: dict[str, Any]) -> str | None:
    task_id = payload.get("task_concept_id") or payload.get("task_id")
    if not isinstance(task_id, str):
        return None
    cleaned = task_id.strip()
    return cleaned or None


def _task_search(**kwargs):
    """Search tasks with Jira-like rich filtering."""
    from ...services.task_management_service import (
        InvalidTaskDataError,
        search_tasks,
    )

    try:
        result = search_tasks(
            query=kwargs.get("query"),
            status_filter=kwargs.get("status_filter") or kwargs.get("status"),
            statuses=kwargs.get("statuses"),
            assignee_concept_id=kwargs.get("assignee_concept_id")
            or kwargs.get("assignee_id")
            or kwargs.get("user_concept_id"),
            labels=kwargs.get("labels"),
            components=kwargs.get("components"),
            fix_versions=kwargs.get("fix_versions") or kwargs.get("fixVersions"),
            sprint_values=kwargs.get("sprint_values") or kwargs.get("sprints"),
            backlog_rank=kwargs.get("backlog_rank"),
            parent_task_concept_id=kwargs.get("parent_task_concept_id"),
            epic_task_concept_id=kwargs.get("epic_task_concept_id"),
            has_parent=kwargs.get("has_parent"),
            has_subtasks=kwargs.get("has_subtasks"),
            has_epic=kwargs.get("has_epic"),
            has_backlog_rank=kwargs.get("has_backlog_rank"),
            start_from=kwargs.get("start_from"),
            start_to=kwargs.get("start_to"),
            due_from=kwargs.get("due_from"),
            due_to=kwargs.get("due_to"),
            created_from=kwargs.get("created_from"),
            created_to=kwargs.get("created_to"),
            updated_from=kwargs.get("updated_from"),
            updated_to=kwargs.get("updated_to"),
            dependency_state=kwargs.get("dependency_state"),
            organisation_concept_id=kwargs.get("organisation_concept_id"),
            limit=kwargs.get("limit", 50),
            offset=kwargs.get("offset", 0),
        )
        result["success"] = True
        return result
    except InvalidTaskDataError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _normalise_issue_keys_input(raw_issue_keys: Any) -> list[str]:
    if not isinstance(raw_issue_keys, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for raw in raw_issue_keys:
        cleaned: str | None = None
        if isinstance(raw, str):
            cleaned = raw.strip().upper()
        elif isinstance(raw, Mapping):
            for key in ("key", "issue_key", "external_id"):
                value = raw.get(key)
                if isinstance(value, str) and value.strip():
                    cleaned = value.strip().upper()
                    break
        if not isinstance(cleaned, str):
            continue
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _coerce_bool_input(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _normalise_jira_label(value: Any, *, default: str | None = None) -> str | None:
    candidate = value if value is not None else default
    if not isinstance(candidate, str):
        return None
    cleaned = candidate.strip()
    return cleaned or None


def _normalise_jira_labels(raw_labels: Any) -> list[str]:
    if not isinstance(raw_labels, list):
        return []
    labels: list[str] = []
    seen: set[str] = set()
    for item in raw_labels:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned:
            continue
        lowered = cleaned.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        labels.append(cleaned)
    return labels


def _task_import_jira_issues(**kwargs):
    """Import Jira issues into Von tasks with dry-run and idempotent reruns."""
    from .jira_proxy_mcp import get_jira_proxy, JiraProxyError
    from ...services.jira_task_import_service import (
        import_jira_issues_to_tasks,
        list_imported_jira_issue_keys,
    )
    from ...services.workflow_event_integration_service import (
        resolve_event_actor_context,
    )

    issue_keys = _normalise_issue_keys_input(kwargs.get("issue_keys"))
    jql = kwargs.get("jql")
    backfill_existing_imports = _coerce_bool_input(
        kwargs.get("backfill_existing_imports"),
        default=False,
    )
    if (
        not issue_keys
        and (not isinstance(jql, str) or not jql.strip())
        and not backfill_existing_imports
    ):
        return make_error_response(
            "MISSING_PARAM",
            "Provide issue_keys, jql, or set backfill_existing_imports=true.",
            suggestions=[
                "Pass issue_keys=['JVNAUTOSCI-123', ...] for explicit import",
                "Or pass a JQL query via jql",
                "Or set backfill_existing_imports=true to reprocess already-imported Jira tasks",
            ],
        )

    try:
        max_results = int(kwargs.get("max_results", 50))
    except (TypeError, ValueError):
        max_results = 50
    max_results = max(1, min(max_results, 200))
    dry_run = _coerce_bool_input(kwargs.get("dry_run"), default=True)
    sync_source_labels = _coerce_bool_input(
        kwargs.get("sync_source_labels"),
        default=True,
    )
    source_migrated_label = _normalise_jira_label(
        kwargs.get("source_migrated_label"),
        default="migrated",
    )
    include_watchers = _coerce_bool_input(kwargs.get("include_watchers"), default=True)
    auto_map_namespace_to_jira_user = _coerce_bool_input(
        kwargs.get("auto_map_namespace_to_jira_user"),
        default=True,
    )
    auto_resolve_participants = _coerce_bool_input(
        kwargs.get("auto_resolve_participants"),
        default=True,
    )
    create_missing_participant_concepts = _coerce_bool_input(
        kwargs.get("create_missing_participant_concepts"),
        default=True,
    )
    namespace_value = _clean_optional_string(kwargs.get("namespace"))
    requested_actor_concept_id = _normalise_optional_concept_id(
        kwargs.get("actor_concept_id")
    )
    requested_org_concept_id = _normalise_optional_concept_id(
        kwargs.get("organisation_concept_id")
    )
    resolved_user_concept_id, resolved_org_concept_id = resolve_event_actor_context(
        user_id=requested_actor_concept_id,
        org_id=requested_org_concept_id,
        namespace=namespace_value,
    )
    actor_concept_id = requested_actor_concept_id or _normalise_optional_concept_id(
        resolved_user_concept_id
    )
    if actor_concept_id is None:
        # Backwards compatibility for callers passing namespace=#V#user only.
        actor_concept_id = _normalise_optional_concept_id(namespace_value)
    organisation_concept_id = requested_org_concept_id or _normalise_optional_concept_id(
        resolved_org_concept_id
    )
    try:
        backfill_limit = int(kwargs.get("backfill_limit", 2000))
    except (TypeError, ValueError):
        backfill_limit = 2000
    backfill_limit = max(1, min(backfill_limit, 2000))

    assignee_map_raw = kwargs.get("assignee_account_id_to_concept_id")
    assignee_map = assignee_map_raw if isinstance(assignee_map_raw, dict) else {}
    jira_map_raw = kwargs.get("jira_account_id_to_concept_id")
    jira_map = jira_map_raw if isinstance(jira_map_raw, dict) else {}
    participant_map: dict[str, str] = {}
    participant_map.update(assignee_map)
    participant_map.update(jira_map)

    async def _fetch_jira_issues() -> dict[str, Any]:
        proxy = await get_jira_proxy()
        discovered_keys = list(issue_keys)
        fetch_errors: list[dict[str, str]] = []
        search_count = 0
        backfill_discovered_count = 0
        namespace_account_id: str | None = None

        if backfill_existing_imports:
            backfill_keys = list_imported_jira_issue_keys(
                organisation_concept_id=organisation_concept_id,
                limit=backfill_limit,
            )
            for issue_key in backfill_keys:
                if issue_key not in discovered_keys:
                    discovered_keys.append(issue_key)
                    backfill_discovered_count += 1

        if (
            auto_map_namespace_to_jira_user
            and isinstance(namespace_value, str)
            and namespace_value.strip()
            and hasattr(proxy, "get_myself")
        ):
            try:
                myself_payload = await proxy.get_myself()
                account_id_value = (
                    myself_payload.get("accountId")
                    if isinstance(myself_payload, Mapping)
                    else None
                )
                if isinstance(account_id_value, str) and account_id_value.strip():
                    namespace_account_id = account_id_value.strip()
            except Exception:
                # Best-effort identity mapping should not block import.
                namespace_account_id = None

        if isinstance(jql, str) and jql.strip():
            search_result = await proxy.search(
                jql=jql.strip(),
                max_results=max_results,
                fields=["key"],
            )
            issues = (
                search_result.get("issues")
                if isinstance(search_result, dict)
                else None
            )
            if isinstance(issues, list):
                search_count = len(issues)
                for item in issues:
                    if not isinstance(item, dict):
                        continue
                    key_value = item.get("key")
                    if isinstance(key_value, str):
                        cleaned = key_value.strip().upper()
                        if cleaned and cleaned not in discovered_keys:
                            discovered_keys.append(cleaned)

        issue_docs: list[dict[str, Any]] = []
        for issue_key in discovered_keys:
            try:
                issue_doc = await proxy.get_issue(issue_key=issue_key)
            except Exception as exc:
                fetch_errors.append(
                    {
                        "issue_key": issue_key,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue

            if not isinstance(issue_doc, dict):
                fetch_errors.append(
                    {
                        "issue_key": issue_key,
                        "error": "jira_get_issue returned non-dict payload",
                    }
                )
                continue
            resolved_key = issue_doc.get("key")
            if not isinstance(resolved_key, str) or not resolved_key.strip():
                fetch_errors.append(
                    {
                        "issue_key": issue_key,
                        "error": "jira_get_issue payload missing key",
                    }
                )
                continue

            if include_watchers and hasattr(proxy, "get_watchers"):
                try:
                    watchers_payload = await proxy.get_watchers(issue_key=resolved_key.strip())
                    if isinstance(watchers_payload, Mapping):
                        issue_doc = dict(issue_doc)
                        issue_doc["watchers"] = watchers_payload
                except Exception as exc:
                    fetch_errors.append(
                        {
                            "issue_key": resolved_key.strip(),
                            "error": f"jira_get_watchers_failed:{type(exc).__name__}:{exc}",
                        }
                    )
            issue_docs.append(issue_doc)

        return {
            "issues": issue_docs,
            "requested_issue_keys": discovered_keys,
            "search_result_count": search_count,
            "backfill_discovered_count": backfill_discovered_count,
            "namespace_account_id": namespace_account_id,
            "fetch_errors": fetch_errors,
        }

    try:
        fetch_payload = _run_async_compat(_fetch_jira_issues)
    except JiraProxyError as exc:
        return make_error_response(
            "jira_proxy_error",
            str(exc),
            details={"exception_type": "JiraProxyError"},
            suggestions=["Check Jira connectivity and authentication"],
        )
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")

    issue_docs = fetch_payload.get("issues") if isinstance(fetch_payload, dict) else None
    if not isinstance(issue_docs, list):
        issue_docs = []
    namespace_account_id = (
        fetch_payload.get("namespace_account_id")
        if isinstance(fetch_payload, Mapping)
        else None
    )
    namespace_identity_concept_id = actor_concept_id
    if (
        namespace_identity_concept_id is None
        and isinstance(namespace_value, str)
        and namespace_value.strip()
        and "@" not in namespace_value
        and "/" not in namespace_value
    ):
        namespace_identity_concept_id = _normalise_optional_concept_id(namespace_value)
    if (
        isinstance(namespace_account_id, str)
        and namespace_account_id.strip()
        and isinstance(namespace_identity_concept_id, str)
        and namespace_identity_concept_id.strip()
    ):
        participant_map.setdefault(
            namespace_account_id.strip(),
            namespace_identity_concept_id.strip(),
        )

    report = import_jira_issues_to_tasks(
        issues=[item for item in issue_docs if isinstance(item, dict)],
        dry_run=dry_run,
        actor_concept_id=actor_concept_id,
        organisation_concept_id=organisation_concept_id,
        assignee_account_id_to_concept_id=assignee_map,
        jira_account_id_to_concept_id=participant_map,
        update_existing=_coerce_bool_input(
            kwargs.get("update_existing"),
            default=True,
        ),
        auto_resolve_participants=auto_resolve_participants,
        create_missing_participant_concepts=create_missing_participant_concepts,
    )
    if not isinstance(report, dict):
        return make_error_response("UNEXPECTED_ERROR", "Importer returned invalid payload")

    issue_docs_by_key: dict[str, Mapping[str, Any]] = {}
    for issue_doc in issue_docs:
        if not isinstance(issue_doc, Mapping):
            continue
        issue_key_raw = issue_doc.get("key")
        if not isinstance(issue_key_raw, str) or not issue_key_raw.strip():
            continue
        issue_docs_by_key[issue_key_raw.strip().upper()] = issue_doc

    issue_rows_raw = report.get("issues")
    issue_rows = issue_rows_raw if isinstance(issue_rows_raw, list) else []

    def _is_label_sync_candidate(action_value: Any) -> bool:
        if not isinstance(action_value, str):
            return False
        if dry_run:
            return action_value in {"would_create", "would_update"}
        return action_value in {"created", "updated", "skipped_existing"}

    async def _sync_source_labels_to_jira() -> dict[str, Any]:
        sync_report: dict[str, Any] = {
            "enabled": bool(sync_source_labels),
            "dry_run": bool(dry_run),
            "label": source_migrated_label,
            "eligible_issue_count": 0,
            "updated_count": 0,
            "would_update_count": 0,
            "already_present_count": 0,
            "error_count": 0,
            "results": [],
        }

        if not sync_source_labels:
            sync_report["status"] = "disabled"
            return sync_report

        if not isinstance(source_migrated_label, str) or not source_migrated_label:
            sync_report["status"] = "disabled_invalid_label"
            return sync_report

        candidate_keys: list[str] = []
        for issue_row in issue_rows:
            if not isinstance(issue_row, Mapping):
                continue
            issue_key_raw = issue_row.get("jira_issue_key")
            if not isinstance(issue_key_raw, str) or not issue_key_raw.strip():
                continue
            issue_key = issue_key_raw.strip().upper()
            if issue_key in candidate_keys:
                continue
            if not _is_label_sync_candidate(issue_row.get("action")):
                continue
            candidate_keys.append(issue_key)

        sync_report["eligible_issue_count"] = len(candidate_keys)

        if not candidate_keys:
            sync_report["status"] = "no_candidates"
            return sync_report

        target_label_cf = source_migrated_label.casefold()
        proxy = await get_jira_proxy() if not dry_run else None

        for issue_key in candidate_keys:
            issue_doc = issue_docs_by_key.get(issue_key)
            fields_value = (
                issue_doc.get("fields") if isinstance(issue_doc, Mapping) else None
            )
            labels_value = (
                fields_value.get("labels") if isinstance(fields_value, Mapping) else None
            )
            existing_labels = _normalise_jira_labels(labels_value)

            # If labels are missing from the fetched payload, re-fetch just labels to
            # avoid overwriting existing source labels.
            if (
                not dry_run
                and labels_value is None
                and proxy is not None
            ):
                try:
                    refreshed_issue = await proxy.get_issue(
                        issue_key=issue_key,
                        fields=["labels"],
                    )
                    if isinstance(refreshed_issue, Mapping):
                        refreshed_fields = refreshed_issue.get("fields")
                        if isinstance(refreshed_fields, Mapping):
                            existing_labels = _normalise_jira_labels(
                                refreshed_fields.get("labels")
                            )
                except Exception:
                    # Keep existing_labels from the original payload and let update attempt decide.
                    pass

            has_label = any(
                isinstance(label, str) and label.casefold() == target_label_cf
                for label in existing_labels
            )
            labels_with_migrated = _normalise_jira_labels(
                list(existing_labels) + [source_migrated_label]
            )

            if has_label:
                sync_report["already_present_count"] = int(
                    sync_report["already_present_count"]
                ) + 1
                sync_report["results"].append(
                    {
                        "issue_key": issue_key,
                        "status": "already_present",
                        "labels": existing_labels,
                    }
                )
                continue

            if dry_run:
                sync_report["would_update_count"] = int(
                    sync_report["would_update_count"]
                ) + 1
                sync_report["results"].append(
                    {
                        "issue_key": issue_key,
                        "status": "would_update",
                        "labels_before": existing_labels,
                        "labels_after": labels_with_migrated,
                    }
                )
                continue

            if proxy is None:
                sync_report["error_count"] = int(sync_report["error_count"]) + 1
                sync_report["results"].append(
                    {
                        "issue_key": issue_key,
                        "status": "error",
                        "error": "jira_proxy_unavailable",
                    }
                )
                continue

            try:
                update_result = await proxy.update_issue(
                    issue_key=issue_key,
                    payload={"fields": {"labels": labels_with_migrated}},
                )
                if isinstance(update_result, Mapping):
                    if update_result.get("success") is False:
                        raise RuntimeError(str(update_result.get("error") or update_result))
                    if update_result.get("error"):
                        raise RuntimeError(str(update_result.get("error")))
                sync_report["updated_count"] = int(sync_report["updated_count"]) + 1
                sync_report["results"].append(
                    {
                        "issue_key": issue_key,
                        "status": "updated",
                        "labels_before": existing_labels,
                        "labels_after": labels_with_migrated,
                    }
                )
            except Exception as exc:
                sync_report["error_count"] = int(sync_report["error_count"]) + 1
                sync_report["results"].append(
                    {
                        "issue_key": issue_key,
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "labels_before": existing_labels,
                    }
                )

        if int(sync_report["error_count"]) > 0:
            sync_report["status"] = "completed_with_errors"
        elif dry_run and int(sync_report["would_update_count"]) > 0:
            sync_report["status"] = "dry_run_preview"
        else:
            sync_report["status"] = "completed"

        return sync_report

    try:
        source_label_sync_report = _run_async_compat(_sync_source_labels_to_jira)
    except JiraProxyError as exc:
        source_label_sync_report = {
            "enabled": bool(sync_source_labels),
            "dry_run": bool(dry_run),
            "label": source_migrated_label,
            "eligible_issue_count": 0,
            "updated_count": 0,
            "would_update_count": 0,
            "already_present_count": 0,
            "error_count": 1,
            "status": "jira_proxy_error",
            "results": [
                {
                    "status": "error",
                    "error": f"JiraProxyError: {exc}",
                }
            ],
        }
    except Exception as exc:
        source_label_sync_report = {
            "enabled": bool(sync_source_labels),
            "dry_run": bool(dry_run),
            "label": source_migrated_label,
            "eligible_issue_count": 0,
            "updated_count": 0,
            "would_update_count": 0,
            "already_present_count": 0,
            "error_count": 1,
            "status": "unexpected_error",
            "results": [
                {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            ],
        }

    report["success"] = bool(report.get("success", True))
    if int(source_label_sync_report.get("error_count", 0)) > 0:
        report["success"] = False
    report["source_label_sync"] = source_label_sync_report
    report["fetch"] = {
        "requested_issue_count": len(fetch_payload.get("requested_issue_keys", []))
        if isinstance(fetch_payload, dict)
        else 0,
        "fetched_issue_count": len(issue_docs),
        "search_result_count": (
            fetch_payload.get("search_result_count", 0)
            if isinstance(fetch_payload, dict)
            else 0
        ),
        "fetch_errors": (
            fetch_payload.get("fetch_errors", [])
            if isinstance(fetch_payload, dict)
            else []
        ),
        "backfill_existing_imports": bool(backfill_existing_imports),
        "backfill_discovered_issue_count": (
            int(fetch_payload.get("backfill_discovered_count", 0))
            if isinstance(fetch_payload, dict)
            else 0
        ),
        "namespace_account_id": (
            fetch_payload.get("namespace_account_id")
            if isinstance(fetch_payload, dict)
            else None
        ),
    }
    return report


def _task_update_fields(**kwargs):
    from ...services.task_management_service import (
        InvalidTaskDataError,
        TaskNotFoundError,
        update_task_fields,
    )

    task_id = _resolve_task_identifier(kwargs)
    if not task_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide task_concept_id (or task_id)"],
        )

    fields = kwargs.get("fields")
    if not isinstance(fields, dict) or not fields:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: fields (non-empty dict)",
            suggestions=["Provide fields to update, e.g. {'status': 'in_progress'}"],
        )

    try:
        result = update_task_fields(
            task_id,
            fields=fields,
            actor_concept_id=kwargs.get("actor_concept_id") or kwargs.get("namespace"),
        )
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except InvalidTaskDataError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_get_transitions(**kwargs):
    from ...services.task_management_service import (
        TaskNotFoundError,
        get_task_transitions,
    )

    task_id = _resolve_task_identifier(kwargs)
    if not task_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide task_concept_id (or task_id)"],
        )

    try:
        result = get_task_transitions(task_id)
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_transition(**kwargs):
    from ...services.task_management_service import (
        InvalidTaskDataError,
        TaskNotFoundError,
        transition_task,
    )

    task_id = _resolve_task_identifier(kwargs)
    if not task_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide task_concept_id (or task_id)"],
        )

    transition_id = kwargs.get("transition_id")
    to_status = kwargs.get("to_status")
    if not (isinstance(transition_id, str) and transition_id.strip()) and not (
        isinstance(to_status, str) and to_status.strip()
    ):
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: transition_id or to_status",
            suggestions=[
                "Call task_get_transitions first, then pass transition_id",
                "Or pass to_status directly",
            ],
        )

    try:
        result = transition_task(
            task_id,
            transition_id=transition_id,
            to_status=to_status,
            actor_concept_id=kwargs.get("actor_concept_id") or kwargs.get("namespace"),
        )
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except InvalidTaskDataError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_unassign(**kwargs):
    from ...services.task_management_service import (
        TaskNotFoundError,
        unassign_task,
    )

    task_id = _resolve_task_identifier(kwargs)
    if not task_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide task_concept_id (or task_id)"],
        )
    try:
        result = unassign_task(task_id)
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_set_parent(**kwargs):
    from ...services.task_management_service import (
        InvalidTaskDataError,
        TaskNotFoundError,
        set_task_parent,
    )

    task_id = _resolve_task_identifier(kwargs)
    if not task_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide task_concept_id (or task_id)"],
        )
    try:
        result = set_task_parent(
            task_id,
            kwargs.get("parent_task_concept_id"),
            actor_concept_id=kwargs.get("actor_concept_id") or kwargs.get("namespace"),
        )
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except InvalidTaskDataError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_create_subtask(**kwargs):
    from ...services.task_management_service import (
        InvalidTaskDataError,
        TaskNotFoundError,
        create_subtask,
    )

    parent_task_concept_id = kwargs.get("parent_task_concept_id")
    title = kwargs.get("title")
    description = kwargs.get("description")
    if (
        not isinstance(parent_task_concept_id, str)
        or not parent_task_concept_id.strip()
    ):
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: parent_task_concept_id",
            suggestions=["Provide parent_task_concept_id"],
        )
    if not isinstance(title, str) or not title.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: title",
            suggestions=["Provide a non-empty title string for the subtask"],
        )
    if not isinstance(description, str) or not description.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: description",
            suggestions=["Provide a non-empty description string for the subtask"],
        )

    start_date, start_error = _parse_optional_iso_datetime_param(
        kwargs.get("start_date"),
        field_name="start_date",
    )
    if start_error:
        return start_error
    due_date, due_error = _parse_optional_iso_datetime_param(
        kwargs.get("due_date"),
        field_name="due_date",
    )
    if due_error:
        return due_error

    try:
        result = create_subtask(
            parent_task_concept_id=parent_task_concept_id.strip(),
            title=title.strip(),
            description=description.strip(),
            assignee_concept_id=kwargs.get("assignee_concept_id")
            or kwargs.get("assignee_id"),
            created_by_concept_id=kwargs.get("created_by_concept_id")
            or kwargs.get("namespace"),
            start_date=start_date,
            due_date=due_date,
            epic_task_concept_id=kwargs.get("epic_task_concept_id"),
            priority=kwargs.get("priority", "medium"),
            organisation_concept_id=kwargs.get("organisation_concept_id"),
            originating_session_id=kwargs.get("originating_session_id")
            or kwargs.get("session_id"),
        )
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except InvalidTaskDataError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_link(**kwargs):
    from ...services.task_management_service import (
        InvalidTaskDataError,
        TaskNotFoundError,
        link_tasks,
    )

    source_id = kwargs.get("source_task_concept_id") or kwargs.get("source_task_id")
    target_id = kwargs.get("target_task_concept_id") or kwargs.get("target_task_id")
    link_type = kwargs.get("link_type")
    if not isinstance(source_id, str) or not source_id.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: source_task_concept_id",
            suggestions=["Provide source_task_concept_id (or source_task_id)"],
        )
    if not isinstance(target_id, str) or not target_id.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: target_task_concept_id",
            suggestions=["Provide target_task_concept_id (or target_task_id)"],
        )
    if not isinstance(link_type, str) or not link_type.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: link_type",
            suggestions=[
                "Provide a link_type such as depends_on, blocks, relates_to, blocked_by, required_by"
            ],
        )

    try:
        result = link_tasks(
            source_id.strip(),
            target_id.strip(),
            link_type=link_type.strip(),
            actor_concept_id=kwargs.get("actor_concept_id") or kwargs.get("namespace"),
        )
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except InvalidTaskDataError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_unlink(**kwargs):
    from ...services.task_management_service import (
        InvalidTaskDataError,
        TaskNotFoundError,
        unlink_tasks,
    )

    source_id = kwargs.get("source_task_concept_id") or kwargs.get("source_task_id")
    target_id = kwargs.get("target_task_concept_id") or kwargs.get("target_task_id")
    link_type = kwargs.get("link_type")
    if not isinstance(source_id, str) or not source_id.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: source_task_concept_id",
            suggestions=["Provide source_task_concept_id (or source_task_id)"],
        )
    if not isinstance(target_id, str) or not target_id.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: target_task_concept_id",
            suggestions=["Provide target_task_concept_id (or target_task_id)"],
        )
    if not isinstance(link_type, str) or not link_type.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: link_type",
            suggestions=[
                "Provide a link_type such as depends_on, blocks, relates_to, blocked_by, required_by"
            ],
        )

    try:
        result = unlink_tasks(
            source_id.strip(),
            target_id.strip(),
            link_type=link_type.strip(),
            actor_concept_id=kwargs.get("actor_concept_id") or kwargs.get("namespace"),
        )
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except InvalidTaskDataError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_add_comment(**kwargs):
    from ...services.task_management_service import (
        InvalidTaskDataError,
        TaskNotFoundError,
        add_task_comment,
    )

    task_id = _resolve_task_identifier(kwargs)
    body = kwargs.get("body") or kwargs.get("comment")
    if not task_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide task_concept_id (or task_id)"],
        )
    if not isinstance(body, str) or not body.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: body",
            suggestions=["Provide comment body text (or use 'comment')"],
        )
    try:
        comment = add_task_comment(
            task_id,
            body=body.strip(),
            author_concept_id=kwargs.get("author_concept_id") or kwargs.get("namespace"),
        )
        return {"success": True, "task_concept_id": task_id, "comment": comment}
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except InvalidTaskDataError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_list_comments(**kwargs):
    from ...services.task_management_service import (
        TaskNotFoundError,
        list_task_comments,
    )

    task_id = _resolve_task_identifier(kwargs)
    if not task_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide task_concept_id (or task_id)"],
        )
    try:
        result = list_task_comments(
            task_id,
            limit=kwargs.get("limit", 100),
            offset=kwargs.get("offset", 0),
        )
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_add_attachment(**kwargs):
    from ...services.task_management_service import (
        InvalidTaskDataError,
        TaskNotFoundError,
        add_task_attachment,
    )

    task_id = _resolve_task_identifier(kwargs)
    filename = kwargs.get("filename") or kwargs.get("name")
    uri = kwargs.get("uri")
    if not task_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide task_concept_id (or task_id)"],
        )
    if not isinstance(filename, str) or not filename.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: filename",
            suggestions=["Provide filename (or name)"],
        )
    if not isinstance(uri, str) or not uri.strip():
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: uri",
            suggestions=["Provide attachment URI"],
        )
    try:
        attachment = add_task_attachment(
            task_id,
            filename=filename.strip(),
            uri=uri.strip(),
            media_type=kwargs.get("media_type") or kwargs.get("mime_type"),
            size_bytes=kwargs.get("size_bytes"),
            added_by_concept_id=kwargs.get("added_by_concept_id")
            or kwargs.get("author_concept_id")
            or kwargs.get("namespace"),
            note=kwargs.get("note"),
        )
        return {"success": True, "task_concept_id": task_id, "attachment": attachment}
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except InvalidTaskDataError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_list_attachments(**kwargs):
    from ...services.task_management_service import (
        TaskNotFoundError,
        list_task_attachments,
    )

    task_id = _resolve_task_identifier(kwargs)
    if not task_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide task_concept_id (or task_id)"],
        )
    try:
        result = list_task_attachments(
            task_id,
            limit=kwargs.get("limit", 100),
            offset=kwargs.get("offset", 0),
        )
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_add_worklog(**kwargs):
    from ...services.task_management_service import (
        InvalidTaskDataError,
        TaskNotFoundError,
        add_task_worklog,
    )

    task_id = _resolve_task_identifier(kwargs)
    time_spent_minutes = kwargs.get("time_spent_minutes")
    if not task_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide task_concept_id (or task_id)"],
        )
    if not isinstance(time_spent_minutes, int) or time_spent_minutes <= 0:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: time_spent_minutes",
            suggestions=["Provide a positive integer time_spent_minutes value"],
        )
    try:
        worklog = add_task_worklog(
            task_id,
            time_spent_minutes=time_spent_minutes,
            author_concept_id=kwargs.get("author_concept_id")
            or kwargs.get("added_by_concept_id")
            or kwargs.get("namespace"),
            comment=kwargs.get("comment"),
            started_at=kwargs.get("started_at"),
        )
        return {"success": True, "task_concept_id": task_id, "worklog": worklog}
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except InvalidTaskDataError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_list_worklog(**kwargs):
    from ...services.task_management_service import (
        TaskNotFoundError,
        list_task_worklog,
    )

    task_id = _resolve_task_identifier(kwargs)
    if not task_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide task_concept_id (or task_id)"],
        )
    try:
        result = list_task_worklog(
            task_id,
            limit=kwargs.get("limit", 100),
            offset=kwargs.get("offset", 0),
        )
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_get_history(**kwargs):
    from ...services.task_management_service import (
        TaskNotFoundError,
        get_task_history,
    )

    task_id = _resolve_task_identifier(kwargs)
    if not task_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_id",
            suggestions=["Provide task_concept_id (or task_id)"],
        )
    try:
        result = get_task_history(
            task_id,
            limit=kwargs.get("limit", 200),
            offset=kwargs.get("offset", 0),
        )
        result["success"] = True
        return result
    except TaskNotFoundError as exc:
        return make_error_response("NOT_FOUND", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _task_bulk_update(**kwargs):
    from ...services.task_management_service import (
        InvalidTaskDataError,
        bulk_update_tasks,
    )

    task_ids = kwargs.get("task_concept_ids") or kwargs.get("task_ids")
    fields = kwargs.get("fields")
    if not isinstance(task_ids, list) or not task_ids:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: task_concept_ids (non-empty list)",
            suggestions=["Provide a list of task IDs to update"],
        )
    if not isinstance(fields, dict) or not fields:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: fields (non-empty dict)",
            suggestions=["Provide fields to apply to all task IDs"],
        )
    try:
        result = bulk_update_tasks(
            task_ids,
            fields=fields,
            actor_concept_id=kwargs.get("actor_concept_id") or kwargs.get("namespace"),
        )
        result["success"] = True
        return result
    except InvalidTaskDataError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response("UNEXPECTED_ERROR", f"Unexpected error: {exc}")


def _normalise_optional_concept_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not cleaned.startswith("#V#"):
        cleaned = f"#V#{cleaned}"
    return cleaned


def _clean_optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _concept_id_to_namespace_slug(concept_id: str | None) -> str | None:
    if not isinstance(concept_id, str):
        return None
    cleaned = concept_id.strip()
    if not cleaned:
        return None
    if cleaned.startswith("#V#"):
        cleaned = cleaned[3:]
    cleaned = cleaned.strip()
    return cleaned or None


def _derive_namespace_for_actor(
    user_concept_id: str | None,
    organisation_concept_id: str | None,
) -> str | None:
    from ...services.namespace_service import derive_namespace

    user_slug = _concept_id_to_namespace_slug(user_concept_id)
    if not user_slug:
        return None
    org_slug = _concept_id_to_namespace_slug(organisation_concept_id)
    try:
        return derive_namespace(user_slug, org_slug)
    except Exception:
        return None


def _bootstrap_coding_agent_identity_for_actor(actor_concept_id: str | None) -> None:
    if actor_concept_id not in {"#V#coding_agent", "#V#github_copilot_instance"}:
        return
    try:
        from ...services.coding_agent_identity_bootstrap_service import (
            ensure_coding_agent_identity_concepts,
        )

        ensure_coding_agent_identity_concepts()
    except Exception:
        # Best effort only: conversation actions should still proceed even if bootstrap fails.
        return


def _resolve_shared_conversation_actor_context(
    payload: Mapping[str, Any],
    *,
    allow_actor_bootstrap_writes: bool = False,
) -> tuple[str | None, str | None, str | None, str | None]:
    from ...services.workflow_event_integration_service import resolve_event_actor_context

    namespace = _clean_optional_string(payload.get("namespace"))
    requested_user = _normalise_optional_concept_id(
        payload.get("user_concept_id")
        or payload.get("acting_user_concept_id")
        or payload.get("actor_user_id")
        or payload.get("on_behalf_of_user_concept_id")
    )
    requested_org = _normalise_optional_concept_id(
        payload.get("organisation_concept_id") or payload.get("org_id")
    )
    actor_concept_id = _normalise_optional_concept_id(
        payload.get("actor_concept_id") or payload.get("agent_concept_id")
    )
    # Keep shared-conversation read paths side-effect free: bootstrap writes are
    # only allowed when the caller explicitly opts in on write-category paths.
    if allow_actor_bootstrap_writes:
        _bootstrap_coding_agent_identity_for_actor(actor_concept_id)

    resolved_user, resolved_org = resolve_event_actor_context(
        user_id=requested_user,
        org_id=requested_org,
        namespace=namespace,
    )

    user_concept_id = _normalise_optional_concept_id(resolved_user) or requested_user
    organisation_concept_id = (
        _normalise_optional_concept_id(resolved_org) or requested_org
    )
    return user_concept_id, organisation_concept_id, actor_concept_id, namespace


def _is_user_member_of_organisation(
    *,
    user_concept_id: str,
    organisation_concept_id: str,
) -> bool:
    from ...services.organisation_membership_service import get_user_memberships

    memberships = get_user_memberships(user_concept_id)
    for membership in memberships.get("memberships", []):
        if not isinstance(membership, dict):
            continue
        member_org = _normalise_optional_concept_id(
            membership.get("organisation_concept_id")
        )
        if member_org == organisation_concept_id:
            return True
    return False


def _shared_conversation_create_session(**kwargs):
    from ...services import chat_history_service
    from ...services.episode_logging_service import log_episode

    user_concept_id, organisation_concept_id, actor_concept_id, namespace = (
        _resolve_shared_conversation_actor_context(
            kwargs,
            allow_actor_bootstrap_writes=True,
        )
    )
    session_id = _clean_optional_string(kwargs.get("session_id"))
    session_name = _clean_optional_string(kwargs.get("session_name"))
    role_in_org = _clean_optional_string(kwargs.get("role_in_org"))

    if not user_concept_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: user_concept_id",
            suggestions=[
                "Provide user_concept_id (or acting_user_concept_id) explicitly",
                "Or pass namespace containing a user identity (for example #V#user@org)",
            ],
        )
    if not session_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: session_id",
            suggestions=["Provide a non-empty session_id"],
        )

    try:
        if organisation_concept_id and not _is_user_member_of_organisation(
            user_concept_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
        ):
            return make_error_response(
                "PERMISSION_DENIED",
                f"User {user_concept_id} is not a member of {organisation_concept_id}",
                details={
                    "user_concept_id": user_concept_id,
                    "organisation_concept_id": organisation_concept_id,
                },
            )
    except PermissionError as exc:
        return make_error_response("PERMISSION_DENIED", str(exc))
    except ValueError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response(
            "ORG_MEMBERSHIP_CHECK_FAILED",
            f"Failed to verify organisation membership: {exc}",
        )

    effective_namespace = namespace or _derive_namespace_for_actor(
        user_concept_id, organisation_concept_id
    )
    created = True
    try:
        created = not chat_history_service.has_chat_history_session(
            user_concept_id,
            session_id,
            namespace=effective_namespace,
            include_legacy=True,
        )
    except Exception:
        # Best effort: session creation remains idempotent even without pre-check.
        created = True

    try:
        session_result = chat_history_service.create_chat_session(
            user_id=user_concept_id,
            session_id=session_id,
            session_name=session_name,
            namespace=effective_namespace,
            organisation_concept_id=organisation_concept_id,
            role_in_org=role_in_org,
        )
    except Exception as exc:
        return make_error_response("CHAT_HISTORY_ERROR", str(exc))

    episode_id = log_episode(
        episode_type="shared_conversation_session_created",
        actor_user_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
        session_id=session_id,
        payload={
            "actor_concept_id": actor_concept_id,
            "created": created,
            "namespace": effective_namespace,
        },
        status="created" if created else "exists",
    )

    return {
        "success": True,
        "created": created,
        "session_id": session_id,
        "session": session_result,
        "user_concept_id": user_concept_id,
        "actor_user_id": user_concept_id,
        "actor_concept_id": actor_concept_id,
        "organisation_concept_id": organisation_concept_id,
        "episode_id": episode_id,
    }


def _shared_conversation_join_session(**kwargs):
    from ...services import chat_history_service
    from ...services.episode_logging_service import log_episode
    from ...services.shared_conversation_service import (
        get_accepted_invite_for_user_session,
        resolve_conversation_owner,
    )

    user_concept_id, organisation_concept_id, actor_concept_id, namespace = (
        _resolve_shared_conversation_actor_context(
            kwargs,
            allow_actor_bootstrap_writes=True,
        )
    )
    session_id = _clean_optional_string(kwargs.get("session_id"))
    session_name = _clean_optional_string(kwargs.get("session_name"))

    if not user_concept_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: user_concept_id",
            suggestions=[
                "Provide user_concept_id (or acting_user_concept_id) explicitly",
                "Or pass namespace containing a user identity (for example #V#user@org)",
            ],
        )
    if not session_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: session_id",
            suggestions=["Provide a non-empty session_id"],
        )

    try:
        invite = get_accepted_invite_for_user_session(
            user_concept_id=user_concept_id,
            session_id=session_id,
        )
        owner_user_id = _normalise_optional_concept_id(
            resolve_conversation_owner(session_id=session_id)
        )
        has_accepted_invite = isinstance(invite, dict)
        is_owner = owner_user_id == user_concept_id
    except Exception as exc:
        return make_error_response(
            "SHARED_CONVERSATION_LOOKUP_FAILED",
            f"Failed to resolve shared conversation access: {exc}",
        )

    if not is_owner and not has_accepted_invite:
        return make_error_response(
            "PERMISSION_DENIED",
            "User does not own this conversation and has no accepted invite",
            details={
                "user_concept_id": user_concept_id,
                "session_id": session_id,
            },
        )

    invite_org = (
        _normalise_optional_concept_id((invite or {}).get("organisation_concept_id"))
        if isinstance(invite, dict)
        else None
    )
    effective_org = organisation_concept_id or invite_org
    if effective_org:
        try:
            if not _is_user_member_of_organisation(
                user_concept_id=user_concept_id,
                organisation_concept_id=effective_org,
            ):
                return make_error_response(
                    "PERMISSION_DENIED",
                    f"User {user_concept_id} is not a member of {effective_org}",
                    details={
                        "user_concept_id": user_concept_id,
                        "organisation_concept_id": effective_org,
                    },
                )
        except PermissionError as exc:
            return make_error_response("PERMISSION_DENIED", str(exc))
        except ValueError as exc:
            return make_error_response("INVALID_DATA", str(exc))
        except Exception as exc:
            return make_error_response(
                "ORG_MEMBERSHIP_CHECK_FAILED",
                f"Failed to verify organisation membership: {exc}",
            )

    effective_namespace = namespace or _derive_namespace_for_actor(
        user_concept_id, effective_org
    )
    session_exists = False
    try:
        session_exists = chat_history_service.has_chat_history_session(
            user_concept_id,
            session_id,
            namespace=effective_namespace,
            include_legacy=True,
        )
    except Exception:
        session_exists = False

    try:
        if session_exists:
            session_result = chat_history_service.get_chat_history_session_summary(
                user_concept_id,
                session_id,
                namespace=effective_namespace,
                include_legacy=True,
                summary_mode="light",
            ) or {"session_id": session_id}
        else:
            session_result = chat_history_service.create_chat_session(
                user_id=user_concept_id,
                session_id=session_id,
                session_name=session_name,
                namespace=effective_namespace,
                organisation_concept_id=effective_org,
            )
    except Exception as exc:
        return make_error_response("CHAT_HISTORY_ERROR", str(exc))

    access_mode = "owner" if is_owner else "invitee"
    created_session = not session_exists
    episode_id = log_episode(
        episode_type="shared_conversation_session_joined",
        actor_user_id=user_concept_id,
        organisation_concept_id=effective_org,
        session_id=session_id,
        payload={
            "actor_concept_id": actor_concept_id,
            "access_mode": access_mode,
            "has_accepted_invite": has_accepted_invite,
            "created_session": created_session,
            "owner_user_id": owner_user_id,
        },
        status="joined",
    )

    return {
        "success": True,
        "joined": True,
        "created_session": created_session,
        "access_mode": access_mode,
        "is_owner": is_owner,
        "has_accepted_invite": has_accepted_invite,
        "session_id": session_id,
        "session": session_result,
        "invite": invite if isinstance(invite, dict) else None,
        "owner_user_id": owner_user_id,
        "user_concept_id": user_concept_id,
        "actor_user_id": user_concept_id,
        "actor_concept_id": actor_concept_id,
        "organisation_concept_id": effective_org,
        "episode_id": episode_id,
    }


def _shared_conversation_invite_create(**kwargs):
    from ...services.episode_logging_service import log_episode
    from ...services.organisation_membership_service import (
        get_organisation_members,
    )
    from ...services.shared_conversation_service import create_invite

    user_concept_id, organisation_concept_id, actor_concept_id, _namespace = (
        _resolve_shared_conversation_actor_context(
            kwargs,
            allow_actor_bootstrap_writes=True,
        )
    )
    session_id = _clean_optional_string(kwargs.get("session_id"))
    invitee_concept_id = _normalise_optional_concept_id(
        kwargs.get("invitee_concept_id") or kwargs.get("invitee_user_id")
    )

    if not user_concept_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: user_concept_id",
            suggestions=[
                "Provide user_concept_id (or acting_user_concept_id) explicitly",
                "Or pass namespace containing a user identity (for example #V#user@org)",
            ],
        )
    if not session_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: session_id",
            suggestions=["Provide a non-empty session_id"],
        )
    if not invitee_concept_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: invitee_concept_id",
            suggestions=["Provide invitee_concept_id (or invitee_user_id)"],
        )
    if invitee_concept_id == user_concept_id:
        return make_error_response(
            "INVALID_DATA",
            "Cannot invite the actor to their own conversation",
            details={"invitee_concept_id": invitee_concept_id},
        )
    if not organisation_concept_id:
        return make_error_response(
            "MISSING_ORGANISATION_CONTEXT",
            "organisation_concept_id is required for shared conversation invites",
            suggestions=[
                "Provide organisation_concept_id (or org_id)",
                "Or pass namespace in #V#user@org format",
            ],
        )

    try:
        if not _is_user_member_of_organisation(
            user_concept_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
        ):
            return make_error_response(
                "PERMISSION_DENIED",
                f"User {user_concept_id} is not a member of {organisation_concept_id}",
                details={
                    "user_concept_id": user_concept_id,
                    "organisation_concept_id": organisation_concept_id,
                },
            )
    except PermissionError as exc:
        return make_error_response("PERMISSION_DENIED", str(exc))
    except ValueError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response(
            "ORG_MEMBERSHIP_CHECK_FAILED",
            f"Failed to verify organisation membership: {exc}",
        )

    try:
        organisation_members = get_organisation_members(organisation_concept_id)
        valid_member_ids = {
            _normalise_optional_concept_id(member.get("user_concept_id"))
            for member in organisation_members.get("members", [])
            if isinstance(member, dict)
        }
        if invitee_concept_id not in valid_member_ids:
            return make_error_response(
                "PERMISSION_DENIED",
                "Invitee is not a member of the organisation",
                details={
                    "invitee_concept_id": invitee_concept_id,
                    "organisation_concept_id": organisation_concept_id,
                },
            )
    except PermissionError as exc:
        return make_error_response("PERMISSION_DENIED", str(exc))
    except ValueError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response(
            "ORGANISATION_MEMBER_LOOKUP_FAILED",
            f"Failed to validate organisation members: {exc}",
        )

    try:
        invite_result = create_invite(
            session_id=session_id,
            inviter_user_id=user_concept_id,
            invitee_user_id=invitee_concept_id,
            organisation_concept_id=organisation_concept_id,
        )
    except Exception as exc:
        return make_error_response("SHARED_CONVERSATION_INVITE_FAILED", str(exc))

    invite_payload = invite_result.get("invite", {})
    episode_id = log_episode(
        episode_type="shared_conversation_invite_created",
        actor_user_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
        session_id=session_id,
        related_invite_id=invite_payload.get("invite_id"),
        payload={
            "actor_concept_id": actor_concept_id,
            "invitee_user_id": invitee_concept_id,
            "created": bool(invite_result.get("created")),
        },
        status="created" if invite_result.get("created") else "exists",
    )

    return {
        "success": True,
        "created": bool(invite_result.get("created")),
        "invite": invite_payload if isinstance(invite_payload, dict) else None,
        "session_id": session_id,
        "user_concept_id": user_concept_id,
        "actor_user_id": user_concept_id,
        "actor_concept_id": actor_concept_id,
        "organisation_concept_id": organisation_concept_id,
        "episode_id": episode_id,
    }


def _shared_conversation_list_invites(**kwargs):
    from ...services.shared_conversation_service import list_invites_for_user

    user_concept_id, organisation_concept_id, actor_concept_id, _namespace = (
        _resolve_shared_conversation_actor_context(
            kwargs,
            allow_actor_bootstrap_writes=False,
        )
    )
    direction_raw = _clean_optional_string(kwargs.get("direction")) or "incoming"
    direction_value = direction_raw.lower()
    if direction_value in {"incoming", "in"}:
        direction = "incoming"
    elif direction_value in {"outgoing", "out"}:
        direction = "outgoing"
    else:
        return make_error_response(
            "INVALID_DATA",
            f"Invalid direction: {direction_raw}",
            suggestions=["Use direction='incoming' or direction='outgoing'"],
        )

    status_raw = _clean_optional_string(kwargs.get("status"))
    status = status_raw if status_raw is not None else "pending"
    session_id = _clean_optional_string(kwargs.get("session_id"))

    if not user_concept_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: user_concept_id",
            suggestions=[
                "Provide user_concept_id (or acting_user_concept_id) explicitly",
                "Or pass namespace containing a user identity (for example #V#user@org)",
            ],
        )

    if organisation_concept_id:
        try:
            if not _is_user_member_of_organisation(
                user_concept_id=user_concept_id,
                organisation_concept_id=organisation_concept_id,
            ):
                return make_error_response(
                    "PERMISSION_DENIED",
                    f"User {user_concept_id} is not a member of {organisation_concept_id}",
                    details={
                        "user_concept_id": user_concept_id,
                        "organisation_concept_id": organisation_concept_id,
                    },
                )
        except PermissionError as exc:
            return make_error_response("PERMISSION_DENIED", str(exc))
        except ValueError as exc:
            return make_error_response("INVALID_DATA", str(exc))
        except Exception as exc:
            return make_error_response(
                "ORG_MEMBERSHIP_CHECK_FAILED",
                f"Failed to verify organisation membership: {exc}",
            )

    try:
        invites = list_invites_for_user(
            user_concept_id=user_concept_id,
            status=status,
            direction=direction,
            session_id=session_id,
        )
    except Exception as exc:
        return make_error_response("SHARED_CONVERSATION_LOOKUP_FAILED", str(exc))

    if organisation_concept_id:
        invites = [
            invite
            for invite in invites
            if (
                _normalise_optional_concept_id(invite.get("organisation_concept_id"))
                in (None, organisation_concept_id)
            )
        ]

    return {
        "success": True,
        "invites": invites,
        "count": len(invites),
        "user_concept_id": user_concept_id,
        "actor_user_id": user_concept_id,
        "actor_concept_id": actor_concept_id,
        "organisation_concept_id": organisation_concept_id,
    }


def _shared_conversation_respond_invite(**kwargs):
    from ...services import chat_history_service
    from ...services.episode_logging_service import log_episode
    from ...services.shared_conversation_service import respond_to_invite

    user_concept_id, organisation_concept_id, actor_concept_id, namespace = (
        _resolve_shared_conversation_actor_context(
            kwargs,
            allow_actor_bootstrap_writes=True,
        )
    )
    invite_id = _clean_optional_string(kwargs.get("invite_id"))
    action = (_clean_optional_string(kwargs.get("action")) or "").lower()
    session_name = _clean_optional_string(kwargs.get("session_name"))
    join_session_on_accept = _coerce_bool_input(
        kwargs.get("join_session_on_accept"),
        default=True,
    )

    if not user_concept_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: user_concept_id",
            suggestions=[
                "Provide user_concept_id (or acting_user_concept_id) explicitly",
                "Or pass namespace containing a user identity (for example #V#user@org)",
            ],
        )
    if not invite_id:
        return make_error_response(
            "MISSING_PARAM",
            "Missing required parameter: invite_id",
            suggestions=["Provide a non-empty invite_id"],
        )
    if action not in {"accept", "decline"}:
        return make_error_response(
            "INVALID_DATA",
            "action must be 'accept' or 'decline'",
            details={"action": kwargs.get("action")},
        )

    try:
        invite = respond_to_invite(
            invite_id=invite_id,
            user_concept_id=user_concept_id,
            action=action,
        )
    except ValueError as exc:
        return make_error_response("INVALID_DATA", str(exc))
    except Exception as exc:
        return make_error_response("SHARED_CONVERSATION_UPDATE_FAILED", str(exc))

    if not isinstance(invite, dict):
        return make_error_response(
            "NOT_FOUND",
            "Invite not found",
            details={"invite_id": invite_id},
        )

    invite_org = _normalise_optional_concept_id(invite.get("organisation_concept_id"))
    effective_org = organisation_concept_id or invite_org
    if organisation_concept_id and invite_org and invite_org != organisation_concept_id:
        return make_error_response(
            "PERMISSION_DENIED",
            "Invite organisation scope does not match supplied organisation_concept_id",
            details={
                "invite_id": invite_id,
                "invite_organisation_concept_id": invite_org,
                "organisation_concept_id": organisation_concept_id,
            },
        )

    created_session = False
    session_result = None
    session_id = _clean_optional_string(invite.get("session_id"))
    if action == "accept" and join_session_on_accept and session_id:
        join_namespace = namespace or _derive_namespace_for_actor(
            user_concept_id, effective_org
        )
        try:
            session_exists = chat_history_service.has_chat_history_session(
                user_concept_id,
                session_id,
                namespace=join_namespace,
                include_legacy=True,
            )
        except Exception:
            session_exists = False
        try:
            if session_exists:
                session_result = chat_history_service.get_chat_history_session_summary(
                    user_concept_id,
                    session_id,
                    namespace=join_namespace,
                    include_legacy=True,
                    summary_mode="light",
                ) or {"session_id": session_id}
            else:
                session_result = chat_history_service.create_chat_session(
                    user_id=user_concept_id,
                    session_id=session_id,
                    session_name=session_name,
                    namespace=join_namespace,
                    organisation_concept_id=effective_org,
                )
                created_session = True
        except Exception as exc:
            return make_error_response("CHAT_HISTORY_ERROR", str(exc))

    episode_id = log_episode(
        episode_type="shared_conversation_invite_responded",
        actor_user_id=user_concept_id,
        organisation_concept_id=effective_org,
        session_id=session_id,
        related_invite_id=invite_id,
        payload={
            "actor_concept_id": actor_concept_id,
            "action": action,
            "join_session_on_accept": join_session_on_accept,
            "created_session": created_session,
        },
        status=invite.get("status"),
    )

    return {
        "success": True,
        "invite_id": invite_id,
        "action": action,
        "invite": invite,
        "session_id": session_id,
        "session": session_result,
        "created_session": created_session,
        "user_concept_id": user_concept_id,
        "actor_user_id": user_concept_id,
        "actor_concept_id": actor_concept_id,
        "organisation_concept_id": effective_org,
        "episode_id": episode_id,
    }


def build_default_catalogue() -> MethodCatalogue:
    """Return a catalogue pre-populated with the baseline method set."""

    catalogue = MethodCatalogue()
    concept_search_input_schema = _concept_search_input_schema()
    concept_search_output_schema = _concept_search_output_schema()
    jira_search_output_schema = _jira_generic_output_schema("search")
    jira_get_issue_output_schema = _jira_generic_output_schema("get_issue")
    jira_get_transitions_output_schema = _jira_generic_output_schema("get_transitions")
    jira_add_comment_output_schema = _jira_generic_output_schema("add_comment")
    jira_add_attachment_output_schema = _jira_add_attachment_output_schema()
    jira_transition_output_schema = _jira_generic_output_schema("transition")
    jira_create_issue_output_schema = _jira_generic_output_schema("create_issue")
    jira_update_issue_output_schema = _jira_generic_output_schema("update_issue")
    jira_link_issue_output_schema = _jira_generic_output_schema("link_issue")
    jira_delete_issue_link_output_schema = _jira_generic_output_schema(
        "delete_issue_link"
    )
    jira_get_myself_output_schema = _jira_generic_output_schema("get_myself")
    jira_get_auth_config_output_schema = _jira_get_auth_config_output_schema()
    jira_hygiene_discover_output_schema = _jira_generic_output_schema(
        "hygiene_discover"
    )
    jira_hygiene_propose_output_schema = _jira_generic_output_schema(
        "hygiene_propose"
    )
    jira_hygiene_check_approval_output_schema = _jira_generic_output_schema(
        "hygiene_check_approval"
    )
    jira_hygiene_execute_batches_output_schema = _jira_generic_output_schema(
        "hygiene_execute_batches"
    )
    jira_hygiene_emit_audit_output_schema = _jira_generic_output_schema(
        "hygiene_emit_audit"
    )
    task_create_output_schema = _task_generic_output_schema("create")
    task_get_output_schema = _task_generic_output_schema("get")
    task_list_output_schema = _task_generic_output_schema("list")
    task_search_output_schema = _task_generic_output_schema("search")
    task_import_jira_issues_output_schema = _task_generic_output_schema(
        "import_jira_issues"
    )
    task_update_status_output_schema = _task_generic_output_schema("update_status")
    task_update_fields_output_schema = _task_generic_output_schema("update_fields")
    task_get_transitions_output_schema = _task_generic_output_schema("get_transitions")
    task_transition_output_schema = _task_generic_output_schema("transition")
    task_assign_output_schema = _task_generic_output_schema("assign")
    task_unassign_output_schema = _task_generic_output_schema("unassign")
    task_set_parent_output_schema = _task_generic_output_schema("set_parent")
    task_create_subtask_output_schema = _task_generic_output_schema("create_subtask")
    task_link_output_schema = _task_generic_output_schema("link")
    task_unlink_output_schema = _task_generic_output_schema("unlink")
    task_add_comment_output_schema = _task_generic_output_schema("add_comment")
    task_list_comments_output_schema = _task_generic_output_schema("list_comments")
    task_add_attachment_output_schema = _task_generic_output_schema("add_attachment")
    task_list_attachments_output_schema = _task_generic_output_schema(
        "list_attachments"
    )
    task_add_worklog_output_schema = _task_generic_output_schema("add_worklog")
    task_list_worklog_output_schema = _task_generic_output_schema("list_worklog")
    task_get_history_output_schema = _task_generic_output_schema("get_history")
    task_bulk_update_output_schema = _task_generic_output_schema("bulk_update")
    task_delete_output_schema = _task_generic_output_schema("delete")
    shared_conversation_create_session_output_schema = (
        _shared_conversation_generic_output_schema("create_session")
    )
    shared_conversation_join_session_output_schema = (
        _shared_conversation_generic_output_schema("join_session")
    )
    shared_conversation_invite_create_output_schema = (
        _shared_conversation_generic_output_schema("invite_create")
    )
    shared_conversation_list_invites_output_schema = (
        _shared_conversation_generic_output_schema("list_invites")
    )
    shared_conversation_respond_invite_output_schema = (
        _shared_conversation_generic_output_schema("respond_invite")
    )
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
                # Accept orchestrator context keys (for example namespace); handler has no required inputs.
                allow_unknown=True,
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
                # Accept orchestrator context keys (for example namespace); handler reads session state only.
                allow_unknown=True,
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
                    "introspection_version": str,
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
                    "orchestrator_missing_tool_call_retry_cap": (int, type(None)),
                },
                optional={
                    "error": str,
                    # Introspection diagnostics evolve over time; keep these
                    # explicit for discoverability while allowing forward fields.
                    "behaviour_prompt_concept_ids": list,
                    "narration_prompt_concept_ids": list,
                    "narration_prompt_concepts": list,
                    "workflow_mode": dict,
                    "configured_openai_api_key_env_var": (str, type(None)),
                    "configured_openai_api_key_env_var_present": bool,
                    "event_workflow_bindings": dict,
                    "event_workflow_bindings_detailed": list,
                    "sensitive_env_presence": dict,
                    "sensitive_env_present_count": int,
                },
                allow_unknown=True,
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
            name="find_relations_with_argument",
            handler=_find_relations_with_argument,
            input_schema=_find_relations_with_argument_input_schema(),
            output_schema=_find_relations_with_argument_output_schema(),
            category="read",
            description=(
                "Find relation assertions where a concept appears in one or more argument "
                "positions. Supports exact graph matching and full-text text-relation matching "
                "with optional predicate/kind filters and pagination."
            ),
        ),
        MethodDefinition(
            name="create_concepts",
            handler=_create_concepts,
            input_schema=_concepts_create_input_schema(),
            output_schema=_concepts_create_output_schema(),
            category="write",
            description="Create one or more concepts (instances, types, or predicates). Each concept needs name and kind ('instance' for individuals, 'type' for subtypes/default, 'predicate' for relationships). Accepts array of {name, kind?, description?, notes?}. Default visibility is user+organisation scoped when authenticated context exists. Override with scope_mode='organisation_general' for organisation-shared concepts, or scope_mode='global_general' for broadly visible concepts when the concept is clearly general. Supports singleton arrays. Use add_names afterward for alternative names/translations.",
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
            name="get_predicate_extent",
            handler=_get_predicate_extent,
            input_schema=_predicate_extent_input_schema(),
            output_schema=_predicate_extent_output_schema(),
            category="read",
            description=(
                "Return the extent (all uses) of a predicate concept. Supports filtering by subject/object type, "
                "source (text_relations|structured|all), pagination, and optional sampling (sample_size)."
            ),
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
            name="upsert_uncertain_relationship_assertion",
            handler=_upsert_uncertain_relationship_assertion,
            input_schema=_upsert_uncertain_relationship_assertion_input_schema(),
            output_schema=_uncertain_relationship_operation_output_schema(),
            category="write",
            description="Create or update a canonical uncertain relationship assertion (confidence + provenance + lifecycle status) for a source concept.",
        ),
        MethodDefinition(
            name="list_uncertain_relationship_assertions",
            handler=_list_uncertain_relationship_assertions,
            input_schema=_list_uncertain_relationship_assertions_input_schema(),
            output_schema=_uncertain_relationship_operation_output_schema(),
            category="read",
            description="List canonical uncertain relationship assertions for a concept, optionally filtered by predicate/status and optionally merged with legacy hypothesised relation mappings.",
        ),
        MethodDefinition(
            name="promote_uncertain_relationship_assertion",
            handler=_promote_uncertain_relationship_assertion,
            input_schema=_promote_uncertain_relationship_assertion_input_schema(),
            output_schema=_uncertain_relationship_operation_output_schema(),
            category="write",
            description="Promote an uncertain relationship assertion to an asserted relationship or text relation and persist promotion linkage metadata.",
        ),
        MethodDefinition(
            name="reject_uncertain_relationship_assertion",
            handler=_reject_uncertain_relationship_assertion,
            input_schema=_reject_uncertain_relationship_assertion_input_schema(),
            output_schema=_uncertain_relationship_operation_output_schema(),
            category="write",
            description="Reject/archive an uncertain relationship assertion with a required reason while preserving provenance and lifecycle history.",
        ),
        MethodDefinition(
            name="migrate_legacy_hypothesized_relations",
            handler=_migrate_legacy_hypothesized_relations,
            input_schema=_migrate_legacy_hypothesized_relations_input_schema(),
            output_schema=_uncertain_relationship_operation_output_schema(),
            category="write",
            description="Migrate legacy hypothesized_relations into canonical uncertain relationship assertions non-destructively, with dry-run support.",
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
            name="preview_remove_relationship",
            handler=_preview_remove_relationship,
            input_schema=_preview_remove_relationship_input_schema(),
            output_schema=_preview_remove_relationship_output_schema(),
            category="read",
            description="Preview relationship removal effects without mutating data. Supports relation_id or triple selectors and returns impact/warning diagnostics.",
        ),
        MethodDefinition(
            name="remove_relationships_bulk",
            handler=_remove_relationships_bulk,
            input_schema=_remove_relationships_bulk_input_schema(),
            output_schema=_remove_relationships_bulk_output_schema(),
            category="write",
            description="Bulk-remove concept relationships using explicit relation IDs, triples, and/or filter selectors. Returns deterministic per-item status with partial-failure reporting.",
        ),
        MethodDefinition(
            name="undo_relationship_removal",
            handler=_undo_relationship_removal,
            input_schema=_undo_relationship_removal_input_schema(),
            output_schema=_undo_relationship_removal_output_schema(),
            category="write",
            description="Restore relationships removed in soft-delete mode by supplying an undo token returned from prior removal operations.",
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
            name="rename_concept",
            handler=_rename_concept,
            input_schema=_rename_concept_input_schema(),
            output_schema=_rename_concept_output_schema(),
            category="write",
            description=(
                "Rename a concept's human-readable ID (JVNAUTOSCI-945). "
                "The concept's GUID remains unchanged for stable references. "
                "All relationship references are automatically updated. "
                "The old ID is preserved as a CODE alias for backwards compatibility. "
                "Can simulate first to preview changes. Use when a concept_id needs "
                "correction (e.g., typo, better naming) without losing data or breaking links."
            ),
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
        MethodDefinition(
            name="index_file_copy",
            handler=_index_file_copy,
            input_schema=_index_file_copy_input_schema(),
            output_schema=_index_file_copy_output_schema(),
            category="write",
            timeout_sec=30.0,
            description=(
                "Read a blob-backed #V#computer_file_copy and index its extracted text into RAG "
                "for the effective namespace. Supports PDFs and other file types handled by read_file_copy."
            ),
        ),
        MethodDefinition(
            name="interpret_file_copy",
            handler=_interpret_file_copy,
            input_schema=_interpret_file_copy_input_schema(),
            output_schema=_interpret_file_copy_output_schema(),
            category="write",
            timeout_sec=45.0,
            description=(
                "Interpret an uploaded #V#computer_file_copy and persist rich concept text relations. "
                "For screenshots and other images (including face/building photos), this runs OCR plus "
                "semantic image description where available, then writes hasDescription/hasContent and "
                "structured interpretation metadata."
            ),
        ),
        MethodDefinition(
            name="import_local_file_copy",
            handler=_import_local_file_copy,
            input_schema=_import_local_file_copy_input_schema(),
            output_schema=_import_local_file_copy_output_schema(),
            category="write",
            timeout_sec=30.0,
            description=(
                "Import a local workspace file into the configured blob store and register a "
                "#V#computer_file_copy concept with canonical provenance metadata."
            ),
        ),
        MethodDefinition(
            name="list_recent_screenshots",
            handler=_list_recent_screenshots,
            input_schema=_list_recent_screenshots_input_schema(),
            output_schema=_list_recent_screenshots_output_schema(),
            category="read",
            timeout_sec=25.0,
            description=(
                "List recent screenshot files from local machine folders and optionally "
                "match against clipboard image content. Supports optional base64 payload "
                "inclusion for direct jira_add_attachment calls."
            ),
        ),
        # LinkedIn Data Dump MCP tools (local external server)
        MethodDefinition(
            name="linkedin_list_exports",
            handler=_linkedin_list_exports,
            input_schema=_linkedin_list_exports_input_schema(),
            output_schema=_linkedin_list_exports_output_schema(),
            category="read",
            timeout_sec=20.0,
            description=(
                "List available LinkedIn data exports from the configured local data root. "
                "Use this first to discover valid export_name values for subsequent LinkedIn tools."
            ),
        ),
        MethodDefinition(
            name="linkedin_list_files",
            handler=_linkedin_list_files,
            input_schema=_linkedin_list_files_input_schema(),
            output_schema=_linkedin_list_files_output_schema(),
            category="read",
            timeout_sec=20.0,
            description=(
                "List files within one LinkedIn export. "
                "Use after linkedin_list_exports to discover available CSV/file names."
            ),
        ),
        MethodDefinition(
            name="linkedin_get_profile",
            handler=_linkedin_get_profile,
            input_schema=_linkedin_get_profile_input_schema(),
            output_schema=_linkedin_get_profile_output_schema(),
            category="read",
            timeout_sec=20.0,
            description=(
                "Get profile information from Profile.csv for a selected LinkedIn export."
            ),
        ),
        MethodDefinition(
            name="linkedin_get_csv_data",
            handler=_linkedin_get_csv_data,
            input_schema=_linkedin_get_csv_data_input_schema(),
            output_schema=_linkedin_get_csv_data_output_schema(),
            category="read",
            timeout_sec=25.0,
            description=(
                "Read sampled rows from any CSV file in a LinkedIn export "
                "(for example Education.csv, Languages.csv, Publications.csv)."
            ),
        ),
        MethodDefinition(
            name="linkedin_get_company_stats",
            handler=_linkedin_get_company_stats,
            input_schema=_linkedin_get_company_stats_input_schema(),
            output_schema=_linkedin_get_company_stats_output_schema(),
            category="read",
            timeout_sec=25.0,
            description=(
                "Get top company counts from Connections.csv for a LinkedIn export."
            ),
        ),
        MethodDefinition(
            name="linkedin_get_messages",
            handler=_linkedin_get_messages,
            input_schema=_linkedin_get_messages_input_schema(),
            output_schema=_linkedin_get_messages_output_schema(),
            category="read",
            timeout_sec=25.0,
            description=(
                "Retrieve message rows from a LinkedIn export (optionally filtered by query text)."
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
        MethodDefinition(
            name="search_proxy_diagnostics",
            handler=_search_proxy_diagnostics,
            input_schema=_search_proxy_diagnostics_input_schema(),
            output_schema=_search_proxy_diagnostics_output_schema(),
            category="read",
            timeout_sec=30.0,
            description=(
                "Get diagnostics and health status for the Tavily search proxy. Returns stats (call count, error rate, "
                "average latency), recent call telemetry with timing and error details, and configuration. "
                "Set include_health_check=true to run a live connectivity test. "
                "Use this to debug search/extraction failures or verify Tavily API connectivity."
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
            description="Fetch full details for a Jira issue by key (e.g., JVNAUTOSCI-123). Use when you need issue fields, summary, status, or metadata.",
        ),
        MethodDefinition(
            name="jira_get_transitions",
            handler=_jira_get_transitions,
            input_schema=_jira_get_transitions_input_schema(),
            output_schema=jira_get_transitions_output_schema,
            category="read",
            timeout_sec=15.0,
            description="List available transitions for a Jira issue key and return transition IDs required by jira_transition.",
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
            name="jira_add_attachment",
            handler=_jira_add_attachment,
            input_schema=_jira_add_attachment_input_schema(),
            output_schema=jira_add_attachment_output_schema,
            category="write",
            timeout_sec=20.0,
            description=(
                "Upload an attachment to a Jira issue using base64 content. "
                "Validates MIME type, file size, and filename safety before upload."
            ),
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
            name="jira_delete_issue_link",
            handler=_jira_delete_issue_link,
            input_schema=_jira_delete_issue_link_input_schema(),
            output_schema=jira_delete_issue_link_output_schema,
            category="write",
            timeout_sec=20.0,
            description=(
                "Delete a Jira issue link by link ID with safety guardrails. Default dry_run=true (no mutation). "
                "At least one of source_issue_key or target_issue_key is required so allow-listed project checks can be enforced. "
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
            name="jira_hygiene_discover",
            handler=_jira_hygiene_discover,
            input_schema=_jira_hygiene_discover_input_schema(),
            output_schema=jira_hygiene_discover_output_schema,
            category="read",
            timeout_sec=25.0,
            description=(
                "Discover Jira hygiene candidates for a project: epic catalogue, true orphans, "
                "and optional cross-cutting candidates with bounded query sizes."
            ),
        ),
        MethodDefinition(
            name="jira_hygiene_propose",
            handler=_jira_hygiene_propose,
            input_schema=_jira_hygiene_propose_input_schema(),
            output_schema=jira_hygiene_propose_output_schema,
            category="read",
            timeout_sec=20.0,
            description=(
                "Build a dry-run Jira hygiene proposal grouped by target epic, with ready-to-execute "
                "operations, ambiguous items needing human decision, and no-action items."
            ),
        ),
        MethodDefinition(
            name="jira_hygiene_check_approval",
            handler=_jira_hygiene_check_approval,
            input_schema=_jira_hygiene_check_approval_input_schema(),
            output_schema=jira_hygiene_check_approval_output_schema,
            category="read",
            timeout_sec=15.0,
            description=(
                "Apply approval-gate controls to Jira hygiene proposals (approved flag, exclusions, "
                "overrides, batch size) and emit approved_operations plus boolean result."
            ),
        ),
        MethodDefinition(
            name="jira_hygiene_execute_batches",
            handler=_jira_hygiene_execute_batches,
            input_schema=_jira_hygiene_execute_batches_input_schema(),
            output_schema=jira_hygiene_execute_batches_output_schema,
            category="write",
            timeout_sec=40.0,
            description=(
                "Execute approved Jira hygiene operations in batches with bounded retry/backoff and "
                "checkpoint telemetry for partial-progress safety."
            ),
        ),
        MethodDefinition(
            name="jira_hygiene_emit_audit",
            handler=_jira_hygiene_emit_audit,
            input_schema=_jira_hygiene_emit_audit_input_schema(),
            output_schema=jira_hygiene_emit_audit_output_schema,
            category="write",
            timeout_sec=20.0,
            description=(
                "Emit structured Jira hygiene audit output and, when enabled, post concise per-epic "
                "audit comments after execution."
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
                "Use when user asks 'what is in my RAG store?' or needs to disambiguate KA sessions, "
                "chat sessions, blob-store file copies, and vector chunks."
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
                description="List namespace-scoped RAG collections (sessions, text relations, file copies)",
            ),
            output_schema=None,
            category="read",
            description="List namespace-scoped RAG items for the selected collection (KA sessions, chat sessions, file-copy concepts, turn execution records, text relations).",
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
                description="Fetch one namespace-scoped RAG item with optional collection selector",
            ),
            output_schema=None,
            category="read",
            description="Get one namespace-scoped RAG item (session/relation/file copy/turn record) with a safe preview. Respects namespace isolation.",
        ),
        MethodDefinition(
            name="turn_execution_list",
            handler=_turn_execution_list,
            input_schema=Schema(
                required={},
                optional={
                    "namespace": (str, type(None)),
                    "limit": (int,),
                    "offset": (int,),
                    "decision": (str, type(None)),
                    "decisions": (list,),
                    "workflow_id": (str, type(None)),
                    "requires_follow_up": (bool,),
                    "prompt_contains": (str, type(None)),
                    "from_utc": (str, type(None)),
                    "to_utc": (str, type(None)),
                },
                allow_unknown=True,
                description=(
                    "List turn execution records with structured filters. "
                    "This is a convenience wrapper over rag_list_indexed(collection='turn_execution_records')."
                ),
            ),
            output_schema=None,
            category="read",
            description=(
                "List assistant turn execution records (workflow selection, required effects, postcondition checks, completion gate). "
                "Use for deterministic evidence triage across conversations. "
                "Includes MCP-visible rag_indexing_state diagnostics per record."
            ),
        ),
        MethodDefinition(
            name="turn_execution_get",
            handler=_turn_execution_get,
            input_schema=Schema(
                required={},
                optional={
                    "request_id": (str, type(None)),
                    "session_id": (str, type(None)),
                    "namespace": (str, type(None)),
                },
                allow_unknown=True,
                description=(
                    "Get one turn execution record by request_id (session_id accepted as alias)."
                ),
            ),
            output_schema=None,
            category="read",
            description=(
                "Fetch a single turn execution record by request_id for detailed failure analysis, "
                "including MCP-visible rag_indexing_state diagnostics."
            ),
        ),
        MethodDefinition(
            name="turn_execution_search_failures",
            handler=_turn_execution_search_failures,
            input_schema=Schema(
                required={},
                optional={
                    "namespace": (str, type(None)),
                    "limit": (int,),
                    "offset": (int,),
                    "decision": (str, type(None)),
                    "decisions": (list,),
                    "workflow_id": (str, type(None)),
                    "requires_follow_up": (bool,),
                    "prompt_contains": (str, type(None)),
                    "from_utc": (str, type(None)),
                    "to_utc": (str, type(None)),
                    "include_completed": (bool,),
                },
                allow_unknown=True,
                description=(
                    "Search turn execution records for likely failure-to-act patterns and return aggregated failure modes."
                ),
            ),
            output_schema=None,
            category="read",
            description=(
                "Mine turn execution records for mutation-not-executed, blocked writes, inconclusive postconditions, and false completion claims."
            ),
        ),
        MethodDefinition(
            name="turn_execution_build_benchmark",
            handler=_turn_execution_build_benchmark,
            input_schema=Schema(
                required={},
                optional={
                    "namespace": (str, type(None)),
                    "limit": (int,),
                    "offset": (int,),
                    "decision": (str, type(None)),
                    "decisions": (list,),
                    "workflow_id": (str, type(None)),
                    "requires_follow_up": (bool,),
                    "prompt_contains": (str, type(None)),
                    "from_utc": (str, type(None)),
                    "to_utc": (str, type(None)),
                    "include_completed": (bool,),
                    "max_cases": (int,),
                    "jira_base_url": (str, type(None)),
                    "baseline_likely_failure_rate_pct": (int, float),
                    "baseline_false_success_rate_pct": (int, float),
                    "baseline_unresolved_follow_up_rate_pct": (int, float),
                    "regression_tolerance_pct": (int, float),
                },
                allow_unknown=True,
                description=(
                    "Build reproducible failure-mining benchmark metrics and replay cases from turn execution records."
                ),
            ),
            output_schema=None,
            category="read",
            description=(
                "Generate corpus-level turn execution reliability metrics, seeded replay cases, and capability-gap signals."
            ),
        ),
        MethodDefinition(
            name="turn_execution_backfill_from_chat_history",
            handler=_turn_execution_backfill_from_chat_history,
            input_schema=Schema(
                required={"namespace": str},
                optional={
                    "limit_sessions": (int,),
                    "dry_run": (bool,),
                    "synthesise_missing_records": (bool,),
                },
                allow_unknown=True,
                description=(
                    "Backfill turn execution projection records from assistant chat history "
                    "for a specific namespace. Defaults to dry-run mode and enables "
                    "record synthesis from llm_debug_data when embedded payloads are absent."
                ),
            ),
            output_schema=None,
            category="write",
            timeout_sec=60.0,
            description=(
                "Rebuild turn_execution_records from historical chat messages with llm_debug_data.turn_execution_record payloads."
            ),
        ),
        MethodDefinition(
            name="turn_execution_namespace_coverage_report",
            handler=_turn_execution_namespace_coverage_report,
            input_schema=Schema(
                required={},
                optional={
                    "namespace": (str, type(None)),
                    "limit_namespaces": (int,),
                    "limit_sessions_per_namespace": (int,),
                    "limit_projected_records_per_namespace": (int,),
                },
                allow_unknown=True,
                description=(
                    "Build namespace-level coverage metrics for turn execution instrumentation "
                    "in chat history and turn_execution_records projection."
                ),
            ),
            output_schema=None,
            category="read",
            description=(
                "Report namespace-by-namespace turn execution coverage, request-id overlap, "
                "and gap signals so benchmark readiness can be validated."
            ),
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
        # Task management MCP tools (JVNAUTOSCI-1040)
        MethodDefinition(
            name="task_create",
            handler=_task_create,
            input_schema=Schema(
                required={"title": str, "description": str},
                optional={
                    "assignee_id": (str, type(None)),
                    "assignee_concept_id": (str, type(None)),
                    "originating_session_id": (str, type(None)),
                    "session_id": (str, type(None)),
                    "created_by_concept_id": (str, type(None)),
                    "namespace": (str, type(None)),
                    "priority": (str,),
                    "start_date": (str, type(None)),
                    "due_date": (str, type(None)),
                    "epic_task_concept_id": (str, type(None)),
                    "components": (list, type(None)),
                    "fix_versions": (list, type(None)),
                    "sprint_values": (list, type(None)),
                    "backlog_rank": (str, type(None)),
                    "organisation_concept_id": (str, type(None)),
                },
                allow_unknown=True,
                description="Create a new task in Vontology.",
            ),
            output_schema=task_create_output_schema,
            category="write",
            description=(
                "Create a Von task (stored as a Vontology concept). Use this to track work items, "
                "action items, or to-dos. Tasks can be assigned to users and linked to conversations. "
                "Priority: low, medium, high, critical. Tasks start in 'pending' status and can include "
                "planning metadata (components, fix versions, sprint values, backlog rank)."
            ),
        ),
        MethodDefinition(
            name="task_get",
            handler=_task_get,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                },
                allow_unknown=True,
                description="Get a task by its concept_id.",
            ),
            output_schema=task_get_output_schema,
            category="read",
            description=(
                "Retrieve a Von task by its concept_id. Returns title, description, status, "
                "priority, assignee, and other metadata."
            ),
        ),
        MethodDefinition(
            name="task_list",
            handler=_task_list,
            input_schema=Schema(
                required={},
                optional={
                    "user_concept_id": (str, type(None)),
                    "assignee_id": (str, type(None)),
                    "status_filter": (str, type(None)),
                    "status": (str, type(None)),
                    "priority_filter": (str, type(None)),
                    "priority": (str, type(None)),
                    "organisation_concept_id": (str, type(None)),
                    "limit": (int,),
                    "include_created": (bool,),
                },
                allow_unknown=True,
                description="List tasks with optional filters.",
            ),
            output_schema=task_list_output_schema,
            category="read",
            description=(
                "List Von tasks. Filter by user (assignee), status, priority, or organisation. "
                "If user_concept_id is provided, returns tasks assigned to that user. "
                "Valid statuses: pending, in_progress, completed, cancelled, blocked."
            ),
        ),
        MethodDefinition(
            name="task_search",
            handler=_task_search,
            input_schema=Schema(
                required={},
                optional={
                    "query": (str, type(None)),
                    "status_filter": (str, type(None)),
                    "status": (str, type(None)),
                    "statuses": (list, type(None)),
                    "assignee_concept_id": (str, type(None)),
                    "assignee_id": (str, type(None)),
                    "user_concept_id": (str, type(None)),
                    "labels": (list, type(None)),
                    "components": (list, type(None)),
                    "fix_versions": (list, type(None)),
                    "fixVersions": (list, type(None)),
                    "sprint_values": (list, type(None)),
                    "sprints": (list, type(None)),
                    "backlog_rank": (str, type(None)),
                    "parent_task_concept_id": (str, type(None)),
                    "epic_task_concept_id": (str, type(None)),
                    "has_parent": (bool, type(None)),
                    "has_subtasks": (bool, type(None)),
                    "has_epic": (bool, type(None)),
                    "has_backlog_rank": (bool, type(None)),
                    "start_from": (str, type(None)),
                    "start_to": (str, type(None)),
                    "due_from": (str, type(None)),
                    "due_to": (str, type(None)),
                    "created_from": (str, type(None)),
                    "created_to": (str, type(None)),
                    "updated_from": (str, type(None)),
                    "updated_to": (str, type(None)),
                    "dependency_state": (str, type(None)),
                    "organisation_concept_id": (str, type(None)),
                    "limit": (int, type(None)),
                    "offset": (int, type(None)),
                },
                allow_unknown=True,
                description="Search tasks with Jira-like rich filtering.",
            ),
            output_schema=task_search_output_schema,
            category="read",
            description=(
                "Search Von tasks with rich filters (status, assignee, labels, planning "
                "metadata, hierarchy, date ranges, dependency state) to support Jira-like "
                "triage and planning."
            ),
        ),
        MethodDefinition(
            name="task_import_jira_issues",
            handler=_task_import_jira_issues,
            input_schema=Schema(
                required={},
                optional={
                    "issue_keys": (list, type(None)),
                    "jql": (str, type(None)),
                    "max_results": (int, type(None)),
                    "dry_run": (bool, type(None)),
                    "update_existing": (bool, type(None)),
                    "backfill_existing_imports": (bool, type(None)),
                    "backfill_limit": (int, type(None)),
                    "sync_source_labels": (bool, type(None)),
                    "source_migrated_label": (str, type(None)),
                    "include_watchers": (bool, type(None)),
                    "auto_map_namespace_to_jira_user": (bool, type(None)),
                    "auto_resolve_participants": (bool, type(None)),
                    "create_missing_participant_concepts": (bool, type(None)),
                    "assignee_account_id_to_concept_id": (dict, type(None)),
                    "jira_account_id_to_concept_id": (dict, type(None)),
                    "organisation_concept_id": (str, type(None)),
                    "namespace": (str, type(None)),
                },
                allow_unknown=True,
                description=(
                    "Import Jira issues into Von tasks using issue_keys and/or jql. "
                    "Set backfill_existing_imports=true to reprocess previously imported Jira-linked tasks. "
                    "Dry-run is enabled by default for preview-safe execution. "
                    "When dry_run=false, source Jira labels are synchronised with "
                    "'migrated' by default (configurable)."
                ),
            ),
            output_schema=task_import_jira_issues_output_schema,
            category="write",
            description=(
                "Migrate Jira issues to Von tasks with idempotent reruns keyed by Jira issue key. "
                "Produces a mapping report listing mapped/dropped fields, relation outcomes, "
                "participant concept preservation outcomes, source-label sync outcomes, "
                "and pilot validation recommendations."
            ),
        ),
        MethodDefinition(
            name="task_update_fields",
            handler=_task_update_fields,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                    "fields": (dict,),
                    "actor_concept_id": (str, type(None)),
                    "namespace": (str, type(None)),
                },
                allow_unknown=True,
                description=(
                    "Update multiple task fields in one operation. "
                    "Supported fields: status, assignee_concept_id, created_by_concept_id, "
                    "title, description, priority, start_date, due_date, labels, components, "
                    "fix_versions, sprint_values, backlog_rank, reporter_concept_id, "
                    "watcher_concept_ids, parent_task_concept_id, epic_task_concept_id."
                ),
            ),
            output_schema=task_update_fields_output_schema,
            category="write",
            description=(
                "Update multiple task fields atomically from an MCP perspective. "
                "Useful for Jira-style edit operations."
            ),
        ),
        MethodDefinition(
            name="task_get_transitions",
            handler=_task_get_transitions,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                },
                allow_unknown=True,
                description="List available transitions for the task's current status.",
            ),
            output_schema=task_get_transitions_output_schema,
            category="read",
            description=(
                "Return available task status transitions (Jira-style), including "
                "transition IDs and resulting statuses."
            ),
        ),
        MethodDefinition(
            name="task_transition",
            handler=_task_transition,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                    "transition_id": (str, type(None)),
                    "to_status": (str, type(None)),
                    "actor_concept_id": (str, type(None)),
                    "namespace": (str, type(None)),
                },
                allow_unknown=True,
                description=(
                    "Transition a task by transition_id or directly by to_status."
                ),
            ),
            output_schema=task_transition_output_schema,
            category="write",
            description=(
                "Apply a status transition to a task (Jira-style transition operation). "
                "Use task_get_transitions first for valid transition IDs."
            ),
        ),
        MethodDefinition(
            name="task_unassign",
            handler=_task_unassign,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                },
                allow_unknown=True,
                description="Unassign all current assignees from a task.",
            ),
            output_schema=task_unassign_output_schema,
            category="write",
            description="Remove task assignee links (Jira-style unassign operation).",
        ),
        MethodDefinition(
            name="task_set_parent",
            handler=_task_set_parent,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                    "parent_task_concept_id": (str, type(None)),
                    "actor_concept_id": (str, type(None)),
                    "namespace": (str, type(None)),
                },
                allow_unknown=True,
                description=(
                    "Set or clear parent task relationship for hierarchy management."
                ),
            ),
            output_schema=task_set_parent_output_schema,
            category="write",
            description=(
                "Set or clear a task's parent. Supports parent reassignment and "
                "hierarchy cycle protection."
            ),
        ),
        MethodDefinition(
            name="task_create_subtask",
            handler=_task_create_subtask,
            input_schema=Schema(
                required={
                    "parent_task_concept_id": str,
                    "title": str,
                    "description": str,
                },
                optional={
                    "assignee_concept_id": (str, type(None)),
                    "assignee_id": (str, type(None)),
                    "originating_session_id": (str, type(None)),
                    "session_id": (str, type(None)),
                    "created_by_concept_id": (str, type(None)),
                    "namespace": (str, type(None)),
                    "priority": (str,),
                    "start_date": (str, type(None)),
                    "due_date": (str, type(None)),
                    "epic_task_concept_id": (str, type(None)),
                    "organisation_concept_id": (str, type(None)),
                },
                allow_unknown=True,
                description="Create a task and attach it as a subtask of a parent task.",
            ),
            output_schema=task_create_subtask_output_schema,
            category="write",
            description=(
                "Create a subtask under an existing parent task using the same core "
                "task creation pathway."
            ),
        ),
        MethodDefinition(
            name="task_update_status",
            handler=_task_update_status,
            input_schema=Schema(
                required={"status": str},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                },
                allow_unknown=True,
                description="Update a task's status.",
            ),
            output_schema=task_update_status_output_schema,
            category="write",
            description=(
                "Update the status of a Von task. Valid statuses: pending, in_progress, "
                "completed, cancelled, blocked. Use this to track task progress."
            ),
        ),
        MethodDefinition(
            name="task_assign",
            handler=_task_assign,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                    "assignee_concept_id": (str,),
                    "assignee_id": (str,),
                },
                allow_unknown=True,
                description="Assign a task to a user.",
            ),
            output_schema=task_assign_output_schema,
            category="write",
            description=(
                "Assign or reassign a Von task to a user. The assignee_concept_id should be "
                "a person or agent concept_id (e.g., #V#michael_witbrock)."
            ),
        ),
        MethodDefinition(
            name="task_link",
            handler=_task_link,
            input_schema=Schema(
                required={
                    "source_task_concept_id": str,
                    "target_task_concept_id": str,
                    "link_type": str,
                },
                optional={
                    "source_task_id": (str, type(None)),
                    "target_task_id": (str, type(None)),
                    "actor_concept_id": (str, type(None)),
                    "namespace": (str, type(None)),
                },
                allow_unknown=True,
                description="Create a typed dependency link between two tasks.",
            ),
            output_schema=task_link_output_schema,
            category="write",
            description=(
                "Create typed task dependencies (e.g., depends_on, blocks, relates_to) "
                "for Jira-like task graph management."
            ),
        ),
        MethodDefinition(
            name="task_unlink",
            handler=_task_unlink,
            input_schema=Schema(
                required={
                    "source_task_concept_id": str,
                    "target_task_concept_id": str,
                    "link_type": str,
                },
                optional={
                    "source_task_id": (str, type(None)),
                    "target_task_id": (str, type(None)),
                    "actor_concept_id": (str, type(None)),
                    "namespace": (str, type(None)),
                },
                allow_unknown=True,
                description="Remove a typed dependency link between two tasks.",
            ),
            output_schema=task_unlink_output_schema,
            category="write",
            description="Remove typed task dependency links.",
        ),
        MethodDefinition(
            name="task_add_comment",
            handler=_task_add_comment,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                    "body": (str,),
                    "comment": (str,),
                    "author_concept_id": (str, type(None)),
                    "namespace": (str, type(None)),
                },
                allow_unknown=True,
                description="Add a comment to a task.",
            ),
            output_schema=task_add_comment_output_schema,
            category="write",
            description="Add a task comment (Jira-style comment operation).",
        ),
        MethodDefinition(
            name="task_list_comments",
            handler=_task_list_comments,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                    "limit": (int, type(None)),
                    "offset": (int, type(None)),
                },
                allow_unknown=True,
                description="List comments for a task.",
            ),
            output_schema=task_list_comments_output_schema,
            category="read",
            description="List task comments with offset/limit pagination.",
        ),
        MethodDefinition(
            name="task_add_attachment",
            handler=_task_add_attachment,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                    "filename": (str,),
                    "name": (str,),
                    "uri": (str,),
                    "media_type": (str, type(None)),
                    "mime_type": (str, type(None)),
                    "size_bytes": (int, type(None)),
                    "note": (str, type(None)),
                    "added_by_concept_id": (str, type(None)),
                    "author_concept_id": (str, type(None)),
                    "namespace": (str, type(None)),
                },
                allow_unknown=True,
                description="Add attachment metadata to a task.",
            ),
            output_schema=task_add_attachment_output_schema,
            category="write",
            description=(
                "Record task attachment metadata (filename/URI/media type/size) in a "
                "Jira-like attachment operation."
            ),
        ),
        MethodDefinition(
            name="task_list_attachments",
            handler=_task_list_attachments,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                    "limit": (int, type(None)),
                    "offset": (int, type(None)),
                },
                allow_unknown=True,
                description="List attachments recorded on a task.",
            ),
            output_schema=task_list_attachments_output_schema,
            category="read",
            description="List task attachments with offset/limit pagination.",
        ),
        MethodDefinition(
            name="task_add_worklog",
            handler=_task_add_worklog,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                    "time_spent_minutes": (int,),
                    "comment": (str, type(None)),
                    "started_at": (str, type(None)),
                    "author_concept_id": (str, type(None)),
                    "added_by_concept_id": (str, type(None)),
                    "namespace": (str, type(None)),
                },
                allow_unknown=True,
                description="Add a worklog entry for a task.",
            ),
            output_schema=task_add_worklog_output_schema,
            category="write",
            description="Add a task worklog entry for effort tracking.",
        ),
        MethodDefinition(
            name="task_list_worklog",
            handler=_task_list_worklog,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                    "limit": (int, type(None)),
                    "offset": (int, type(None)),
                },
                allow_unknown=True,
                description="List task worklog entries.",
            ),
            output_schema=task_list_worklog_output_schema,
            category="read",
            description="List task worklog entries with aggregate effort totals.",
        ),
        MethodDefinition(
            name="task_get_history",
            handler=_task_get_history,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                    "limit": (int, type(None)),
                    "offset": (int, type(None)),
                },
                allow_unknown=True,
                description="Get task audit history/timeline entries.",
            ),
            output_schema=task_get_history_output_schema,
            category="read",
            description="Return task history/audit timeline.",
        ),
        MethodDefinition(
            name="task_bulk_update",
            handler=_task_bulk_update,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_ids": (list,),
                    "task_ids": (list, type(None)),
                    "fields": (dict,),
                    "actor_concept_id": (str, type(None)),
                    "namespace": (str, type(None)),
                },
                allow_unknown=True,
                description="Apply the same field updates to multiple tasks.",
            ),
            output_schema=task_bulk_update_output_schema,
            category="write",
            description=(
                "Bulk-update multiple tasks with the same field patch. Useful for "
                "high-throughput maintenance operations."
            ),
        ),
        MethodDefinition(
            name="task_delete",
            handler=_task_delete,
            input_schema=Schema(
                required={},
                optional={
                    "task_concept_id": (str,),
                    "task_id": (str,),
                },
                allow_unknown=True,
                description="Delete (cancel) a task.",
            ),
            output_schema=task_delete_output_schema,
            category="write",
            description=(
                "Delete a Von task by marking it as cancelled. The task remains in the system "
                "but is no longer active."
            ),
        ),
        MethodDefinition(
            name="shared_conversation_create_session",
            handler=_shared_conversation_create_session,
            input_schema=_shared_conversation_create_session_input_schema(),
            output_schema=shared_conversation_create_session_output_schema,
            category="write",
            description=(
                "Create or materialise a chat session for a user in shared-conversation "
                "flows. Supports actor/on-behalf context for coding-agent execution."
            ),
        ),
        MethodDefinition(
            name="shared_conversation_join_session",
            handler=_shared_conversation_join_session,
            input_schema=_shared_conversation_join_session_input_schema(),
            output_schema=shared_conversation_join_session_output_schema,
            category="write",
            description=(
                "Validate owner/invite access to a shared conversation and materialise "
                "a user session record for participation."
            ),
        ),
        MethodDefinition(
            name="shared_conversation_invite_create",
            handler=_shared_conversation_invite_create,
            input_schema=_shared_conversation_invite_create_input_schema(),
            output_schema=shared_conversation_invite_create_output_schema,
            category="write",
            description=(
                "Create a shared-conversation invite with organisation-membership checks "
                "for inviter and invitee."
            ),
        ),
        MethodDefinition(
            name="shared_conversation_list_invites",
            handler=_shared_conversation_list_invites,
            input_schema=_shared_conversation_list_invites_input_schema(),
            output_schema=shared_conversation_list_invites_output_schema,
            category="read",
            description=(
                "List incoming or outgoing shared-conversation invites for a user, with "
                "optional organisation and session filtering."
            ),
        ),
        MethodDefinition(
            name="shared_conversation_respond_invite",
            handler=_shared_conversation_respond_invite,
            input_schema=_shared_conversation_respond_invite_input_schema(),
            output_schema=shared_conversation_respond_invite_output_schema,
            category="write",
            description=(
                "Accept or decline a shared-conversation invite. On acceptance, can also "
                "materialise a participant session record for immediate join."
            ),
        ),
        # Description generation tools (JVNAUTOSCI-1044)
        MethodDefinition(
            name="generate_concept_description",
            handler=_generate_concept_description,
            input_schema=_generate_concept_description_input_schema(),
            output_schema=_generate_concept_description_output_schema(),
            category="write",
            description=(
                "Generate a description for a concept using LLM. Use this when a concept lacks "
                "a proper description or has an ugly auto-generated placeholder. The generated "
                "description uses the concept's name, type hierarchy, and relationships as context. "
                "Set force=true to regenerate even if a description exists."
            ),
        ),
        MethodDefinition(
            name="check_placeholder_description",
            handler=_check_placeholder_description,
            input_schema=_check_placeholder_description_input_schema(),
            output_schema=_check_placeholder_description_output_schema(),
            category="read",
            description=(
                "Check if a description text appears to be an auto-generated placeholder "
                "(e.g., derived from concept ID, timestamp-based, or too short). "
                "Use this to identify concepts that need proper descriptions."
            ),
        ),
        MethodDefinition(
            name="renderer_resolve_applicability",
            handler=_renderer_resolve_applicability,
            input_schema=_renderer_resolve_applicability_input_schema(),
            output_schema=_renderer_resolve_applicability_output_schema(),
            category="read",
            description=(
                "Resolve candidate renderers from Vontology-defined applicability metadata. "
                "Supports concept-backed and transient microtheory payloads, including "
                "multimodal selection and fallback diagnostics. Renderer definitions "
                "can be supplied inline or loaded from Vontology concept text relations."
            ),
        ),
        MethodDefinition(
            name="upsert_renderer_profile",
            handler=_upsert_renderer_profile,
            input_schema=_upsert_renderer_profile_input_schema(),
            output_schema=_upsert_renderer_profile_output_schema(),
            category="write",
            description=(
                "Validate and persist renderer applicability metadata on a renderer concept. "
                "Stores canonical profile JSON as a singleton text relation."
            ),
        ),
        # =============================================================================
        # Durable Workflow Instance Tools (JVNAUTOSCI-1075)
        # =============================================================================
        MethodDefinition(
            name="workflow_list_definitions",
            handler=_workflow_list_definitions,
            input_schema=Schema(
                required={},
                optional={"limit": int},
                allow_unknown=True,
                description="List available workflow definitions.",
            ),
            output_schema=Schema(
                required={"success": bool, "definitions": list, "count": int},
                optional={
                    "parity_inventory": dict,
                    "baseline_telemetry": dict,
                    "capability_matrix": dict,
                    "error": str,
                    "error_code": str,
                },
                allow_unknown=True,
                description="List of workflow definitions.",
            ),
            category="read",
            description="List available workflow definitions (IDs, descriptions) that can be instantiated.",
        ),
        MethodDefinition(
            name="workflow_mcp_health_check",
            handler=_workflow_mcp_health_check,
            input_schema=Schema(
                required={},
                optional={
                    "namespace": (str, type(None)),
                    "include_introspection": bool,
                },
                allow_unknown=True,
                description="Run read-only health checks for core workflow MCP tools.",
            ),
            output_schema=Schema(
                required={"success": bool, "checked_tools": list, "checks": list},
                optional={
                    "failed_tools": list,
                    "capability_matrix": dict,
                    "error": str,
                    "error_code": str,
                },
                allow_unknown=True,
                description="Workflow MCP health-check result with per-tool diagnostics.",
            ),
            category="read",
            description=(
                "Run lightweight workflow/introspection MCP health checks through "
                "InternalMCPGateway.invoke() and return actionable diagnostics."
            ),
        ),
        MethodDefinition(
            name="workflow_create_instance",
            handler=_workflow_create_instance,
            input_schema=Schema(
                required={"workflow_id": str},
                optional={
                    "user_id": str,
                    "org_id": str,
                    "namespace": (str, type(None)),
                    "inputs": (dict, type(None)),
                    "max_retries": int,
                },
                allow_unknown=True,
                description="Create a durable workflow instance.",
            ),
            output_schema=Schema(
                required={"success": bool},
                optional={
                    "instance_id": str,
                    "status": str,
                    "error": str,
                    "error_code": str,
                },
                allow_unknown=True,
                description="Result with instance_id if successful.",
            ),
            category="write",
            description=(
                "Create a new durable workflow instance. The workflow will be queued for "
                "execution by a background worker. Use workflow_get_instance to check status."
            ),
        ),
        MethodDefinition(
            name="workflow_bind_event",
            handler=_workflow_bind_event,
            input_schema=Schema(
                required={"event_type": str, "workflow_id": str},
                optional={
                    "input_mapping": (dict, type(None)),
                    "enabled": bool,
                    "replace_existing": bool,
                    "actor": (str, type(None)),
                },
                allow_unknown=True,
                description="Create or update an event -> workflow binding.",
            ),
            output_schema=Schema(
                required={"success": bool},
                optional={
                    "binding": dict,
                    "created": bool,
                    "updated": bool,
                    "unchanged": bool,
                    "workflow_registry_known": (bool, type(None)),
                    "error": str,
                    "error_code": str,
                },
                allow_unknown=True,
                description="Binding write result.",
            ),
            category="write",
            description=(
                "Register or update an event-to-workflow binding with optional input mapping. "
                "Set replace_existing=true to overwrite an existing conflicting binding."
            ),
        ),
        MethodDefinition(
            name="workflow_list_event_bindings",
            handler=_workflow_list_event_bindings,
            input_schema=Schema(
                required={},
                optional={
                    "event_type": (str, type(None)),
                    "enabled_only": bool,
                    "include_env_fallback": bool,
                    "limit": int,
                },
                allow_unknown=True,
                description="List event->workflow bindings.",
            ),
            output_schema=Schema(
                required={"success": bool, "bindings": list, "count": int},
                optional={"error": str, "error_code": str},
                allow_unknown=True,
                description="List of event->workflow bindings.",
            ),
            category="read",
            description=(
                "List event bindings from persistent storage, with optional environment fallback "
                "entries for legacy compatibility."
            ),
        ),
        MethodDefinition(
            name="workflow_list_instances",
            handler=_workflow_list_instances,
            input_schema=Schema(
                required={},
                optional={
                    "user_id": (str, type(None)),
                    "org_id": (str, type(None)),
                    "namespace": (str, type(None)),
                    "status": (str, type(None)),
                    "workflow_id": (str, type(None)),
                    "source_event_type": (str, type(None)),
                    "source_event_id": (str, type(None)),
                    "session_id": (str, type(None)),
                    "request_id": (str, type(None)),
                    "from_utc": (str, type(None)),
                    "to_utc": (str, type(None)),
                    "limit": int,
                },
                allow_unknown=True,
                description="List workflow instances with filters.",
            ),
            output_schema=Schema(
                required={"success": bool, "instances": list, "count": int},
                optional={"error": str, "error_code": str},
                allow_unknown=True,
                description="List of workflow instance summaries.",
            ),
            category="read",
            description=(
                "List durable workflow instances. Filter by user, org, namespace, status, or workflow_id. "
                "Supports source_event_type/source_event_id filters for event-to-workflow traceability, "
                "session_id/request_id filters for turn traceability, and from_utc/to_utc date windows. "
                "Valid statuses: pending, running, completed, failed, cancelled, paused."
            ),
        ),
        MethodDefinition(
            name="workflow_get_instance",
            handler=_workflow_get_instance,
            input_schema=Schema(
                required={"instance_id": str},
                optional={},
                allow_unknown=True,
                description="Get a workflow instance by ID.",
            ),
            output_schema=Schema(
                required={"success": bool},
                optional={
                    "instance_id": str,
                    "workflow_id": str,
                    "status": str,
                    "current_state": (str, type(None)),
                    "step_index": int,
                    "inputs": dict,
                    "outputs": (dict, type(None)),
                    "error": (str, type(None)),
                    "error_code": (str, type(None)),
                },
                allow_unknown=True,
                description="Full workflow instance details.",
            ),
            category="read",
            description=(
                "Get detailed status of a durable workflow instance including current state, "
                "inputs, outputs, and any errors."
            ),
        ),
        MethodDefinition(
            name="workflow_cancel_instance",
            handler=_workflow_cancel_instance,
            input_schema=Schema(
                required={"instance_id": str},
                optional={},
                allow_unknown=True,
                description="Cancel a workflow instance.",
            ),
            output_schema=Schema(
                required={"success": bool},
                optional={
                    "instance_id": str,
                    "status": str,
                    "error": (str, type(None)),
                    "error_code": (str, type(None)),
                },
                allow_unknown=True,
                description="Cancellation result.",
            ),
            category="write",
            description=(
                "Cancel a running or pending workflow instance. The worker will stop "
                "execution at the next checkpoint. Cannot cancel already-terminal instances."
            ),
        ),
        MethodDefinition(
            name="workflow_retry_instance",
            handler=_workflow_retry_instance,
            input_schema=Schema(
                required={"instance_id": str},
                optional={},
                allow_unknown=True,
                description="Retry a failed workflow instance.",
            ),
            output_schema=Schema(
                required={"success": bool},
                optional={
                    "instance_id": str,
                    "status": str,
                    "retry_count": int,
                    "error": (str, type(None)),
                    "error_code": (str, type(None)),
                },
                allow_unknown=True,
                description="Retry result.",
            ),
            category="write",
            description=(
                "Reset a failed workflow instance for retry. Only works if the instance "
                "has not exceeded its max_retries limit. Execution will resume from "
                "the last checkpoint."
            ),
        ),
        # =============================================================================
        # Durable Workflow Schedule Tools
        # =============================================================================
        MethodDefinition(
            name="workflow_create_schedule",
            handler=_workflow_create_schedule,
            input_schema=Schema(
                required={"workflow_id": str, "schedule_type": str},
                optional={
                    "user_id": str,
                    "org_id": str,
                    "namespace": (str, type(None)),
                    "interval_seconds": int,
                    "cron_expression": str,
                    "run_at": str,
                    "default_inputs": (dict, type(None)),
                    "description": (str, type(None)),
                },
                allow_unknown=True,
                description=(
                    "Create a workflow schedule. schedule_type: 'interval', 'cron', or 'once'. "
                    "Provide interval_seconds for interval, cron_expression for cron, "
                    "run_at (ISO datetime) for once."
                ),
            ),
            output_schema=Schema(
                required={"success": bool},
                optional={
                    "schedule_id": str,
                    "schedule_type": str,
                    "error": (str, type(None)),
                    "error_code": (str, type(None)),
                },
                allow_unknown=True,
                description="Schedule creation result.",
            ),
            category="write",
            description=(
                "Create a scheduled trigger for a workflow. Supports three types: "
                "'interval' (run every N seconds), 'cron' (5-field cron expression), "
                "'once' (run at a specific time). The scheduler will automatically "
                "create workflow instances when schedules are due."
            ),
        ),
        MethodDefinition(
            name="workflow_list_schedules",
            handler=_workflow_list_schedules,
            input_schema=Schema(
                required={},
                optional={
                    "user_id": (str, type(None)),
                    "enabled_only": bool,
                    "limit": int,
                },
                allow_unknown=True,
                description="List workflow schedules.",
            ),
            output_schema=Schema(
                required={"success": bool, "schedules": list, "count": int},
                optional={"error": str, "error_code": str},
                allow_unknown=True,
                description="List of workflow schedules.",
            ),
            category="read",
            description="List workflow schedules. Filter by user or enabled status.",
        ),
        MethodDefinition(
            name="workflow_get_schedule",
            handler=_workflow_get_schedule,
            input_schema=Schema(
                required={"schedule_id": str},
                optional={},
                allow_unknown=True,
                description="Get a schedule by ID.",
            ),
            output_schema=Schema(
                required={"success": bool},
                optional={
                    "schedule_id": str,
                    "workflow_id": str,
                    "schedule_type": str,
                    "enabled": bool,
                    "cron_expression": (str, type(None)),
                    "interval_seconds": (int, type(None)),
                    "next_run_at": (str, type(None)),
                    "last_run_at": (str, type(None)),
                    "error": (str, type(None)),
                    "error_code": (str, type(None)),
                },
                allow_unknown=True,
                description="Full schedule details.",
            ),
            category="read",
            description="Get detailed information about a workflow schedule.",
        ),
        MethodDefinition(
            name="workflow_set_schedule_enabled",
            handler=_workflow_set_schedule_enabled,
            input_schema=Schema(
                required={"schedule_id": str, "enabled": bool},
                optional={},
                allow_unknown=True,
                description="Enable or disable a schedule.",
            ),
            output_schema=Schema(
                required={"success": bool},
                optional={
                    "schedule_id": str,
                    "enabled": bool,
                    "error": (str, type(None)),
                    "error_code": (str, type(None)),
                },
                allow_unknown=True,
                description="Update result.",
            ),
            category="write",
            description="Enable or disable a workflow schedule.",
        ),
        MethodDefinition(
            name="workflow_delete_schedule",
            handler=_workflow_delete_schedule,
            input_schema=Schema(
                required={"schedule_id": str},
                optional={},
                allow_unknown=True,
                description="Delete a schedule.",
            ),
            output_schema=Schema(
                required={"success": bool},
                optional={
                    "deleted": bool,
                    "schedule_id": str,
                    "error": (str, type(None)),
                    "error_code": (str, type(None)),
                },
                allow_unknown=True,
                description="Deletion result.",
            ),
            category="write",
            description="Delete a workflow schedule permanently.",
        ),
        MethodDefinition(
            name="workflow_trigger_schedule",
            handler=_workflow_trigger_schedule,
            input_schema=Schema(
                required={"schedule_id": str},
                optional={},
                allow_unknown=True,
                description="Manually trigger a schedule.",
            ),
            output_schema=Schema(
                required={"success": bool},
                optional={
                    "instance_id": str,
                    "schedule_id": str,
                    "status": str,
                    "error": (str, type(None)),
                    "error_code": (str, type(None)),
                },
                allow_unknown=True,
                description="Trigger result with new instance_id.",
            ),
            category="write",
            description=(
                "Manually trigger a workflow schedule immediately, creating a new "
                "workflow instance. Useful for testing or ad-hoc execution."
            ),
        ),
    ]

    for definition in definitions:
        catalogue.register(definition)

    try:
        built_in_definitions = {definition.name: definition for definition in definitions}
        dynamic_result = load_dynamic_method_definitions(
            base_definitions=built_in_definitions,
            protected_method_names=built_in_definitions.keys(),
        )
        for dynamic_definition in dynamic_result.definitions:
            catalogue.register(dynamic_definition)
    except Exception as exc:
        # Fail closed: keep baseline built-ins available even if dynamic loading fails.
        logger.warning(
            "[internal_mcp_catalogue] Dynamic MCP tool registration failed: %s", exc
        )

    return catalogue
