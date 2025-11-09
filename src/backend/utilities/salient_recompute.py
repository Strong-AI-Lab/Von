"""Utility helpers for recomputing inherited salient predicates.

This module hosts the authoritative logic for the salient predicate recompute
workflow so it can be imported both by Flask routes and CLI entrypoints.
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..db.mongo_client import CONCEPTS_COLLECTION_NAME, get_db  # type: ignore
from ..vontology.utils_vontology import (  # type: ignore
    SALIENT_SCOPE_FIELD,
    SALIENT_SCOPE_INSTANCE_KEY,
    SALIENT_SCOPE_TYPE_KEY,
    SALIENT_SCOPE_UNCLASSIFIED_KEY,
    normalise_salient_scope_map,
)

DIRECT_FIELD = '#V#salient_binary_predicate_for_type'
INHERITED_FIELD = 'inherited_salient_binary_predicates'
STAMP_FIELD = 'inherited_salient_computed_at'
ERROR_FIELD = 'inherited_salient_error'
UPDATED_LIST_LIMIT = 100


def fetch_type_docs(limit: Optional[int] = None):
    """Fetch all type documents (concept ids beginning with #V#)."""
    db = get_db()
    if db is None:
        raise RuntimeError('Database unavailable (get_db returned None)')

    coll = db[CONCEPTS_COLLECTION_NAME]
    cursor = coll.find(
        {
            'concept_id': {'$regex': '^#V#'}
        },
        {
            'concept_id': 1,
            'relationships.is_a_type_of': 1,
            f'relationships.{DIRECT_FIELD}': 1,
            INHERITED_FIELD: 1,
            STAMP_FIELD: 1,
            SALIENT_SCOPE_FIELD: 1,
        }
    )
    if limit:
        cursor = cursor.limit(int(limit))
    return list(cursor)


def build_graph(type_docs: Iterable[dict]) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, int]]:
    """Construct parent, child, and indegree mappings for the type graph."""
    parents_map: Dict[str, List[str]] = {}
    children_map: Dict[str, List[str]] = {}
    indegree: Dict[str, int] = {}

    for doc in type_docs:
        cid = doc.get('concept_id')
        if not isinstance(cid, str):
            continue
        rels = (doc.get('relationships') or {})
        parents = rels.get('is_a_type_of') or []
        plist = [p for p in parents if isinstance(p, str)]
        parents_map[cid] = plist
        indegree[cid] = len(plist)

    for child, plist in parents_map.items():
        for parent in plist:
            children_map.setdefault(parent, []).append(child)

    return parents_map, children_map, indegree


def ordered_union(*lists: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for lst in lists:
        for value in lst:
            if isinstance(value, str) and value not in seen:
                seen.add(value)
                out.append(value)
    return out


def recompute_salient_predicates(*, force: bool = False, limit: Optional[int] = None, dry_run: bool = False) -> dict:
    """Recompute inherited salient predicates for all ontology types."""
    start = time.time()
    docs = fetch_type_docs(limit)
    parents_map, _, _ = build_graph(docs)
    doc_by_id = {d['concept_id']: d for d in docs if 'concept_id' in d}

    processed = len(doc_by_id)
    updated = 0
    max_len = 0
    cycles_detected = False

    # Extract direct salient predicate lists once for reuse
    direct_map: Dict[str, List[str]] = {}
    for cid, doc in doc_by_id.items():
        rels = (doc.get('relationships') or {})
        direct_list = rels.get(DIRECT_FIELD) or []
        if isinstance(direct_list, str):
            direct_list = [direct_list]
        direct_map[cid] = [v for v in direct_list if isinstance(v, str)]

    # Iterative fixed-point propagation to handle cycles without discarding state
    inherited_cache: Dict[str, List[str]] = {cid: list(direct_map.get(cid, [])) for cid in doc_by_id}
    max_iterations = max(4 * max(1, len(doc_by_id)), 32)
    iterations = 0
    while iterations < max_iterations:
        iterations += 1
        changed = False
        for cid in doc_by_id:
            parent_lists = [inherited_cache.get(parent, []) for parent in parents_map.get(cid, [])]
            new_list = ordered_union(*parent_lists, direct_map.get(cid, []))
            if new_list != inherited_cache.get(cid, []):
                inherited_cache[cid] = new_list
                changed = True
        if not changed:
            break
    else:
        cycles_detected = True

    for inherited in inherited_cache.values():
        if inherited:
            max_len = max(max_len, len(inherited))

    # Write / dry-run accounting
    db = get_db()
    if db is None:
        raise RuntimeError('Database unavailable (get_db returned None) during write phase')
    coll = db[CONCEPTS_COLLECTION_NAME]
    now_stamp = int(time.time())
    scope_updates = 0
    updated_ids: List[str] = []

    if not dry_run:
        for cid, inherited_list in inherited_cache.items():
            doc = doc_by_id[cid]
            existing = doc.get(INHERITED_FIELD)
            direct_values = direct_map.get(cid, [])
            scope_existing = normalise_salient_scope_map(doc.get(SALIENT_SCOPE_FIELD))
            scope_target = {
                SALIENT_SCOPE_INSTANCE_KEY: list(scope_existing.get(SALIENT_SCOPE_INSTANCE_KEY, [])),
                SALIENT_SCOPE_TYPE_KEY: ordered_union(direct_values),
                SALIENT_SCOPE_UNCLASSIFIED_KEY: list(scope_existing.get(SALIENT_SCOPE_UNCLASSIFIED_KEY, [])),
            }
            scope_changed = scope_target != scope_existing
            changed = force or existing != inherited_list

            if changed or cycles_detected or scope_changed:
                update_doc = {
                    INHERITED_FIELD: inherited_list,
                    STAMP_FIELD: now_stamp,
                    ERROR_FIELD: bool(cycles_detected),
                }
                if scope_changed:
                    update_doc[SALIENT_SCOPE_FIELD] = scope_target
                coll.update_one({'concept_id': cid}, {'$set': update_doc})
                updated += 1
                if len(updated_ids) < UPDATED_LIST_LIMIT:
                    updated_ids.append(cid)
                if scope_changed:
                    scope_updates += 1
    else:
        for cid, inherited_list in inherited_cache.items():
            doc = doc_by_id[cid]
            existing = doc.get(INHERITED_FIELD)
            direct_values = direct_map.get(cid, [])
            scope_existing = normalise_salient_scope_map(doc.get(SALIENT_SCOPE_FIELD))
            scope_target = {
                SALIENT_SCOPE_INSTANCE_KEY: list(scope_existing.get(SALIENT_SCOPE_INSTANCE_KEY, [])),
                SALIENT_SCOPE_TYPE_KEY: ordered_union(direct_values),
                SALIENT_SCOPE_UNCLASSIFIED_KEY: list(scope_existing.get(SALIENT_SCOPE_UNCLASSIFIED_KEY, [])),
            }
            scope_changed = scope_target != scope_existing
            if force or existing != inherited_list or scope_changed or cycles_detected:
                updated += 1
                if len(updated_ids) < UPDATED_LIST_LIMIT:
                    updated_ids.append(cid)
                if scope_changed:
                    scope_updates += 1

    duration_ms = int((time.time() - start) * 1000)
    summary = {
        'success': True,
        'types_processed': processed,
        'types_total': len(doc_by_id),
        'types_updated': updated,
        'duration_ms': duration_ms,
        'max_inherited_length': max_len,
        'cycles_detected': cycles_detected,
        'force': force,
        'dry_run': dry_run,
        'scope_updates': scope_updates,
    }
    if updated and updated <= UPDATED_LIST_LIMIT:
        summary['updated_concepts'] = updated_ids
    return summary


def _print_summary(summary: dict) -> None:
    """Pretty-print summary for CLI usage."""
    pretty = json.dumps(summary, indent=2)
    print(pretty)
    print(json.dumps(summary, separators=(',', ':')))


def cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true', help='Rewrite even if unchanged')
    parser.add_argument('--limit', type=int, default=None, help='Limit number of type docs to process (debug)')
    parser.add_argument('--dry-run', action='store_true', help='Compute but do not write changes')
    args = parser.parse_args(argv)

    try:
        summary = recompute_salient_predicates(force=args.force, limit=args.limit, dry_run=args.dry_run)
    except Exception as exc:  # pragma: no cover - surfaced to caller
        print(json.dumps({'success': False, 'error': str(exc)}), flush=True)
        return 1

    _print_summary(summary)
    return 0


def main(argv: Optional[List[str]] = None) -> None:
    exit_code = cli(argv)
    if exit_code:
        raise SystemExit(exit_code)


__all__ = [
    'recompute_salient_predicates',
    'fetch_type_docs',
    'build_graph',
    'ordered_union',
    'cli',
    'main',
]
