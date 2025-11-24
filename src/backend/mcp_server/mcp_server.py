# src/backend/mcp_server/mcp_server.py

import sys
import os
import asyncio
from flask import Flask, request, jsonify

# Adjust path to import from the project root
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from ..vontology.utils_vontology import (
    create_vontology_concept,
    get_all_vontology_nodes_with_details,
    simulate_or_delete_concept,
)
from ..db.repositories.concepts_repository import ConceptsRepository
from ..services.concept_service import get_concept_display_name_with_names_fallback
from ..services.concept_search_service import search_concepts
from ..services.text_value_service import upsert_text_for_concept
from ..services.concept_merge_service import merge_concepts
from ..integrations.internal_mcp.arxiv_proxy import get_arxiv_proxy, ArxivProxyError
from ..integrations.internal_mcp.search_proxy_mcp import get_search_proxy, SearchProxyError

app = Flask(__name__)

@app.route('/get_context', methods=['GET'])
def mcp_get_context():
    """
    Get current server-side context information.

    Note: User and organisation information is managed client-side (localStorage)
    per JVNAUTOSCI-628 architecture. This endpoint returns only server-managed
    configuration: active model, language preference, and runtime settings.
    """
    try:
        from ..services.settings_service import (
            get_active_llm_setting,
            get_preferred_language,
            get_setting
        )
        from datetime import datetime, timezone

        # Get active model setting (returns dict with model name and provider)
        model_setting = get_active_llm_setting()

        context = {
            "llm_model": model_setting.get("model") if isinstance(model_setting, dict) else model_setting,
            "llm_provider": model_setting.get("provider") if isinstance(model_setting, dict) else None,
            "language_preference": get_preferred_language(),
            "fetch_counts_on_load": get_setting("fetch_counts_on_load"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "note": "User and organisation context managed client-side (localStorage)"
        }

        return jsonify(context), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/create_concepts', methods=['POST'])
def mcp_create_concepts():
    """
    Creates one or more concepts (instances, types, or predicates).
    Expects JSON: {
        "parent_id": "#V#...",
        "concepts": [
            {"name": "...", "kind": "instance"|"type"|"predicate", "description": "...", "notes": "..."},
            ...
        ]
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    parent_id = data.get('parent_id')
    concepts = data.get('concepts', [])

    if not parent_id:
        return jsonify({"error": "Missing 'parent_id'"}), 400
    if not concepts or not isinstance(concepts, list):
        return jsonify({"error": "Missing or invalid 'concepts' array"}), 400

    try:
        results = []
        for concept_data in concepts:
            name = concept_data.get('name')
            kind = concept_data.get('kind', 'type')  # Default to type

            if not name:
                results.append({"error": "Concept missing required 'name' field", "data": concept_data})
                continue

            # Map kind to create_as_instance parameter
            create_as_instance = (kind == "instance")

            result = create_vontology_concept(
                parent_id=parent_id,
                new_concept_name=name,
                create_as_instance=create_as_instance,
                description=concept_data.get('description'),
                notes=concept_data.get('notes')
            )
            results.append(result)

        successful = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
        status_code = 201 if successful == len(concepts) else (207 if successful > 0 else 400)

        return jsonify({
            "results": results,
            "total": len(concepts),
            "successful": successful
        }), status_code

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/find_subconcepts', methods=['GET'])
def mcp_find_subconcepts():
    """
    Finds direct subconcepts (children) of a given concept.
    Expects query parameter: ?concept_id=...
    Returns: Array of subconcepts with id, name, and kind metadata
    """
    concept_id = request.args.get('concept_id')
    if not concept_id:
        return jsonify({"error": "Missing 'concept_id' parameter"}), 400

    try:
        # Optimized query: fetch concepts where parent matches, include metadata
        cursor = ConceptsRepository.find(
            {"relationships.is_a_type_of": concept_id},
            {"concept_id": 1, "name": 1, "names": 1, "computed_kind": 1}
        )

        subconcepts = []
        for doc in cursor:
            cid = doc.get('concept_id')
            if not cid:
                continue

            # Get human-readable name with names[] fallback
            try:
                name = get_concept_display_name_with_names_fallback(doc)
            except Exception:
                name = doc.get('name') or cid

            # Get kind, defaulting to "individual" if not set
            kind = doc.get('computed_kind') or 'individual'

            subconcepts.append({
                "id": cid,
                "name": name,
                "kind": kind
            })

        return jsonify(subconcepts), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/find_concepts_by_name', methods=['GET'])
def mcp_find_concepts_by_name():
    """
    Finds concepts with a name containing the provided string.
    Uses the unified concept_search_service which handles text relations properly.
    Expects query parameter: ?name=...
    Returns: Array of matching concepts with id, name, and kind metadata
    """
    name_substring = request.args.get('name')
    if not name_substring:
        return jsonify({"error": "Missing 'name' parameter"}), 400

    try:
        # Use the proper search service that handles text relations AND legacy name fields
        search_result = search_concepts(
            query=name_substring,
            match_type="substring",
            include_description=False,  # Name search only
            limit=100
        )

        # Transform to MCP format {id, name, kind}
        matching_concepts = []
        for result in search_result.get('results', []):
            matching_concepts.append({
                "id": result.get('concept_id'),
                "name": result.get('name'),
                "kind": result.get('kind', 'individual')
            })

        return jsonify(matching_concepts), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/add_names', methods=['POST'])
def mcp_add_names():
    """
    Adds one or more names (aliases/synonyms) to an existing concept using text relations.
    Uses the hasName predicate to properly store names in the text_values system.

    Supports multilingual names and different name classifications:
    - Language codes: Use ISO 639-1 or BCP 47 (e.g., 'en-NZ', 'fr', 'de', 'mi', 'zh')
    - Name types:
        * 'NL' (Natural Language): Standard names, synonyms, translations (default)
        * 'ABBR' (Abbreviation): Short forms like 'EU', 'NATO', 'PhD'
        * 'CODE': Technical identifiers, URIs, system codes (e.g., OpenCyc URIs)

    Expects JSON: {
        "concept_id": "#V#...",
        "names": [
            "Simple name",  // String format (defaults: en-NZ, NL)
            {               // Object format for precise control
                "name": "Name text",
                "language": "en-NZ",  // Optional, defaults to en-NZ
                "name_type": "NL"     // Optional: NL, ABBR, or CODE (defaults to NL)
            },
            ...
        ]
    }

    Examples:
        ["EU member", "EU member state"]  // Simple strings
        [{"name": "État membre", "language": "fr"}]  // French translation
        [{"name": "EU MS", "name_type": "ABBR"}]  // Abbreviation

    Returns: {
        "success": bool,
        "concept_id": str,
        "added_count": int,
        "error_count": int,
        "results": [...],  // Successful additions
        "errors": [...]    // Any failures
    }
    Status codes: 201 (full success), 207 (partial success), 400 (complete failure)
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    concept_id = data.get('concept_id')
    names = data.get('names')

    if not concept_id:
        return jsonify({"error": "Missing 'concept_id'"}), 400
    if not names or not isinstance(names, list) or len(names) == 0:
        return jsonify({"error": "Missing or invalid 'names' array"}), 400

    try:
        # Verify concept exists
        concept = ConceptsRepository.find_one({"concept_id": concept_id})
        if not concept:
            return jsonify({"error": f"Concept '{concept_id}' not found"}), 404

        results = []
        errors = []

        # Process each name in the array
        for idx, name_obj in enumerate(names):
            # Handle both dict and string formats
            if isinstance(name_obj, str):
                name_text = name_obj
                language = 'en-NZ'
                name_type = 'NL'
            elif isinstance(name_obj, dict):
                name_text = name_obj.get('name')
                language = name_obj.get('language', 'en-NZ')
                name_type = name_obj.get('name_type', 'NL')
            else:
                errors.append({"index": idx, "error": "Invalid name format (must be string or object)"})
                continue

            if not name_text or not isinstance(name_text, str) or not name_text.strip():
                errors.append({"index": idx, "error": "Missing or invalid name text"})
                continue

            try:
                # Use text relations to add the name
                result = upsert_text_for_concept(
                    subject_concept_id=concept_id,
                    predicate="hasName",
                    text=name_text.strip(),
                    lang=language,
                    context={'name_type': name_type}
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

        status_code = 201 if len(errors) == 0 else (207 if len(results) > 0 else 400)

        return jsonify({
            "success": len(errors) == 0,
            "concept_id": concept_id,
            "added_count": len(results),
            "error_count": len(errors),
            "results": results,
            "errors": errors if errors else []
        }), status_code

    except PermissionError as e:
        return jsonify({"error": f"Permission denied: {str(e)}"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/delete_concept', methods=['POST'])
def mcp_delete_concept():
    """
    Deletes a concept and handles its relationships.
    Expects JSON: {
        "concept_id": "#V#...",
        "simulate": true/false (default true)
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    concept_id = data.get('concept_id')
    simulate = data.get('simulate', True)

    if not concept_id:
        return jsonify({"error": "Missing 'concept_id'"}), 400

    try:
        # Reuse the existing logic
        result = simulate_or_delete_concept(concept_id, execute=not simulate)

        status_code = 200
        if not result.get('success', False):
             status_code = 404 if 'not found' in result.get('error', '').lower() else 400

        return jsonify(result), status_code

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/merge_concepts', methods=['POST'])
def mcp_merge_concepts():
    """
    Merges a source concept into a target concept.
    Expects JSON: {
        "source_id": "#V#...",
        "target_id": "#V#...",
        "simulate": true/false (default true)
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    source_id = data.get('source_id')
    target_id = data.get('target_id')
    simulate = data.get('simulate', True)

    if not source_id or not target_id:
        return jsonify({"error": "Missing 'source_id' or 'target_id'"}), 400

    try:
        result = merge_concepts(source_id, target_id, simulate=simulate)

        status_code = 200
        if not result.get('success', False):
             status_code = 400

        return jsonify(result), status_code

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Search MCP tool routes
@app.route('/search_web', methods=['GET', 'POST'])
def mcp_search_web():
    """Perform a Tavily-backed web search via the MCP proxy."""

    def _get_payload():
        if request.method == 'POST':
            return request.get_json() or {}
        return request.args.to_dict(flat=False)

    def _get_raw(payload, key):
        value = payload.get(key)
        if isinstance(value, list):
            return value[-1] if value else None
        return value

    def _get_str(payload, key):
        raw = _get_raw(payload, key)
        if raw is None:
            return None
        return raw if isinstance(raw, str) else str(raw)

    def _get_list(payload, key):
        value = payload.get(key)
        if value is None:
            return None
        raw_items = value if isinstance(value, list) else [value]
        items = []
        for item in raw_items:
            if item is None:
                continue
            if isinstance(item, str):
                parts = [part.strip() for part in item.split(',')]
                items.extend(part for part in parts if part)
            else:
                items.append(str(item).strip())
        cleaned = [item for item in items if item]
        return cleaned or None

    def _coerce_bool(value, default=False):
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "on"}
        return bool(value)

    try:
        payload = _get_payload()
        query = _get_str(payload, 'query')
        if not query:
            return jsonify({"error": "Missing required parameter: query"}), 400

        max_results_raw = _get_str(payload, 'max_results') or '10'
        try:
            max_results = int(max_results_raw)
        except (TypeError, ValueError):
            raise ValueError("max_results must be an integer")

        search_depth = (_get_str(payload, 'search_depth') or 'basic').lower()
        if search_depth not in {"basic", "advanced"}:
            raise ValueError("search_depth must be 'basic' or 'advanced'")

        include_domains = _get_list(payload, 'include_domains')
        exclude_domains = _get_list(payload, 'exclude_domains')
        include_answer = _coerce_bool(_get_raw(payload, 'include_answer'))
        include_raw_content = _coerce_bool(_get_raw(payload, 'include_raw_content'))
        include_images = _coerce_bool(_get_raw(payload, 'include_images'))

        async def _run_search():
            proxy = await get_search_proxy()
            return await proxy.search(
                query=query,
                max_results=max_results,
                search_depth=search_depth,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                include_answer=include_answer,
                include_raw_content=include_raw_content,
                include_images=include_images,
            )

        result = asyncio.run(_run_search())
        return jsonify(result), 200

    except ValueError as e:
        return jsonify({"error": str(e), "success": False}), 400
    except SearchProxyError as e:
        return jsonify({"error": str(e), "success": False}), 500
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}", "success": False}), 500


@app.route('/context_search', methods=['GET', 'POST'])
def mcp_context_search():
    """Perform a context-aware Tavily search via the MCP proxy."""

    def _get_payload():
        if request.method == 'POST':
            return request.get_json() or {}
        return request.args.to_dict(flat=False)

    def _get_raw(payload, key):
        value = payload.get(key)
        if isinstance(value, list):
            return value[-1] if value else None
        return value

    def _get_str(payload, key):
        raw = _get_raw(payload, key)
        if raw is None:
            return None
        return raw if isinstance(raw, str) else str(raw)

    def _coerce_bool(value, default=False):
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "on"}
        return bool(value)

    try:
        payload = _get_payload()
        query = _get_str(payload, 'query')
        context_value = _get_str(payload, 'context')
        if not query:
            return jsonify({"error": "Missing required parameter: query"}), 400
        if not context_value:
            return jsonify({"error": "Missing required parameter: context"}), 400

        max_results_raw = _get_str(payload, 'max_results') or '10'
        try:
            max_results = int(max_results_raw)
        except (TypeError, ValueError):
            raise ValueError("max_results must be an integer")

        search_depth = (_get_str(payload, 'search_depth') or 'basic').lower()
        if search_depth not in {"basic", "advanced"}:
            raise ValueError("search_depth must be 'basic' or 'advanced'")

        include_answer = _coerce_bool(_get_raw(payload, 'include_answer'))

        async def _run_context_search():
            proxy = await get_search_proxy()
            return await proxy.context_search(
                query=query,
                context=context_value,
                max_results=max_results,
                search_depth=search_depth,
                include_answer=include_answer,
            )

        result = asyncio.run(_run_context_search())
        return jsonify(result), 200

    except ValueError as e:
        return jsonify({"error": str(e), "success": False}), 400
    except SearchProxyError as e:
        return jsonify({"error": str(e), "success": False}), 500
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}", "success": False}), 500


@app.route('/qna_search', methods=['GET', 'POST'])
def mcp_qna_search():
    """Perform a Tavily question-answering search via the MCP proxy."""

    def _get_payload():
        if request.method == 'POST':
            return request.get_json() or {}
        return request.args.to_dict(flat=False)

    def _get_raw(payload, key):
        value = payload.get(key)
        if isinstance(value, list):
            return value[-1] if value else None
        return value

    def _get_str(payload, key):
        raw = _get_raw(payload, key)
        if raw is None:
            return None
        return raw if isinstance(raw, str) else str(raw)

    try:
        payload = _get_payload()
        query = _get_str(payload, 'query')
        if not query:
            return jsonify({"error": "Missing required parameter: query"}), 400

        max_results_raw = _get_str(payload, 'max_results') or '5'
        try:
            max_results = int(max_results_raw)
        except (TypeError, ValueError):
            raise ValueError("max_results must be an integer")

        search_depth = (_get_str(payload, 'search_depth') or 'advanced').lower()
        if search_depth not in {"basic", "advanced"}:
            raise ValueError("search_depth must be 'basic' or 'advanced'")

        async def _run_qna_search():
            proxy = await get_search_proxy()
            return await proxy.qna_search(
                query=query,
                max_results=max_results,
                search_depth=search_depth,
            )

        result = asyncio.run(_run_qna_search())
        return jsonify(result), 200

    except ValueError as e:
        return jsonify({"error": str(e), "success": False}), 400
    except SearchProxyError as e:
        return jsonify({"error": str(e), "success": False}), 500
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}", "success": False}), 500


@app.route('/extract_url', methods=['GET', 'POST'])
def mcp_extract_url():
    """Extract content from a URL via the Tavily MCP proxy."""

    def _get_payload():
        if request.method == 'POST':
            return request.get_json() or {}
        return request.args.to_dict(flat=False)

    def _get_raw(payload, key):
        value = payload.get(key)
        if isinstance(value, list):
            return value[-1] if value else None
        return value

    def _get_str(payload, key):
        raw = _get_raw(payload, key)
        if raw is None:
            return None
        return raw if isinstance(raw, str) else str(raw)

    try:
        payload = _get_payload()
        url = _get_str(payload, 'url')
        if not url:
            return jsonify({"error": "Missing required parameter: url"}), 400

        async def _run_extract():
            proxy = await get_search_proxy()
            return await proxy.extract(url=url)

        result = asyncio.run(_run_extract())
        return jsonify(result), 200

    except SearchProxyError as e:
        return jsonify({"error": str(e), "success": False}), 500
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}", "success": False}), 500


# arXiv MCP tool routes
@app.route('/search_arxiv', methods=['GET', 'POST'])
def mcp_search_arxiv():
    """
    Search arXiv.org for scholarly articles.

    Query parameters (GET) or JSON body (POST):
    - query: Search query string (supports boolean operators)
    - max_results: Maximum results to return (default: 10)
    - sort_by: Sort order - "relevance", "lastUpdatedDate", "submittedDate" (default: relevance)
    - sort_order: "ascending" or "descending" (default: descending)

    Returns: {
        "results": [{id, title, authors, summary, published, ...}, ...],
        "total": int,
        "query": str
    }
    """
    try:
        if request.method == 'POST':
            data = request.get_json() or {}
        else:
            data = request.args.to_dict()

        query = data.get('query', '')
        max_results = int(data.get('max_results', 10))
        sort_by = data.get('sort_by', 'relevance')
        sort_order = data.get('sort_order', 'descending')

        proxy = get_arxiv_proxy()
        result = proxy.search_arxiv(
            query=query,
            max_results=max_results,
            sort_by=sort_by,
            sort_order=sort_order
        )

        return jsonify(result), 200

    except ArxivProxyError as e:
        return jsonify({"error": str(e), "success": False}), 500
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}", "success": False}), 500


@app.route('/get_paper_metadata', methods=['GET', 'POST'])
def mcp_get_paper_metadata():
    """
    Get detailed metadata for a specific arXiv paper.

    Query parameter (GET) or JSON body (POST):
    - arxiv_id: arXiv identifier (e.g., "2506.16596" or "arXiv:2506.16596")

    Returns: {
        "id": str,
        "title": str,
        "authors": [str, ...],
        "abstract": str,
        "published": str,
        "updated": str,
        "categories": [str, ...],
        "doi": str,
        "pdf_url": str,
        "comments": str
    }
    """
    try:
        if request.method == 'POST':
            data = request.get_json() or {}
        else:
            data = request.args.to_dict()

        arxiv_id = data.get('arxiv_id')

        if not arxiv_id:
            return jsonify({"error": "Missing required parameter: arxiv_id"}), 400

        proxy = get_arxiv_proxy()
        result = proxy.get_paper_metadata(arxiv_id=arxiv_id)

        return jsonify(result), 200

    except ArxivProxyError as e:
        return jsonify({"error": str(e), "success": False}), 500
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}", "success": False}), 500


@app.route('/download_paper', methods=['POST'])
def mcp_download_paper():
    """
    Download PDF of an arXiv paper to local storage.

    JSON body:
    - arxiv_id: arXiv identifier (required)
    - filename: Optional custom filename (defaults to arxiv_id.pdf)

    Returns: {
        "success": bool,
        "file_path": str,
        "arxiv_id": str
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    arxiv_id = data.get('arxiv_id')

    if not arxiv_id:
        return jsonify({"error": "Missing required parameter: arxiv_id"}), 400

    try:
        proxy = get_arxiv_proxy()
        result = proxy.download_paper(
            arxiv_id=arxiv_id,
            filename=data.get('filename')
        )

        status_code = 200 if result.get('success') else 500
        return jsonify(result), status_code

    except ArxivProxyError as e:
        return jsonify({"error": str(e), "success": False}), 500
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}", "success": False}), 500


if __name__ == '__main__':
    # For simplicity, running on a different port than the main app
    # To run: python src/backend/mcp_server/mcp_server.py
    app.run(host='0.0.0.0', port=5002, debug=True)
