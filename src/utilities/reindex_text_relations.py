from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Sequence

from src.backend.db.repositories.text_value_repository import TextRelationsRepository
from src.backend.services.rag_service import RAGBackendUnavailable, get_rag_service
from src.backend.services.rag_text_relation_sync_service import (
    TextRelationRagDoc,
    collect_text_relation_docs_for_namespace,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReindexArgs:
    namespace: str
    predicates: Optional[List[str]]
    languages: Optional[List[str]]
    concept_ids: Optional[List[str]]
    updated_since: Optional[datetime]
    dry_run: bool
    page_size: int
    batch_size: int
    scan_limit: int


def _parse_iso_datetime(value: str) -> datetime:
    raw = value.strip()
    if not raw:
        raise ValueError("since value is empty")

    # Allow Z suffix.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        # Allow date-only input.
        try:
            dt = datetime.fromisoformat(raw + "T00:00:00")
        except ValueError as exc:
            raise ValueError(
                "since must be ISO format (e.g. 2025-12-01 or 2025-12-01T12:34:56Z)"
            ) from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def _normalise_list(values: Optional[Sequence[str]]) -> Optional[List[str]]:
    if not values:
        return None

    cleaned: List[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        parts = [p.strip() for p in item.split(",")]
        for p in parts:
            if p:
                cleaned.append(p)

    return cleaned or None


def _resolve_namespace(
    *, namespace: Optional[str], user: Optional[str], org: Optional[str]
) -> str:
    if namespace and namespace.strip():
        return namespace.strip()

    if user or org:
        if not (user and org):
            raise ValueError("--user and --org must be provided together")
        return f"{user.strip()}@{org.strip()}"

    raise ValueError("--namespace is required (or provide both --user and --org)")


def parse_args(argv: Optional[Sequence[str]] = None) -> ReindexArgs:
    parser = argparse.ArgumentParser(
        description="Bulk reindex Vontology text relations into the RAG store."
    )

    parser.add_argument(
        "--namespace",
        help="Namespace in the form '#V#user@#V#organisation'",
    )
    parser.add_argument(
        "--user",
        help="Namespace user part (requires --org)",
    )
    parser.add_argument(
        "--org",
        help="Namespace organisation part (requires --user). Can be '#V#org' or 'org'.",
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="Reindex all visible relations in the namespace (default if no other filters).",
    )

    parser.add_argument(
        "--predicate",
        action="append",
        help="Only index relations with this predicate (repeatable, or comma-separated).",
    )
    parser.add_argument(
        "--language",
        action="append",
        help="Only index text values with this language (repeatable, or comma-separated).",
    )
    parser.add_argument(
        "--concept-id",
        action="append",
        help="Only index relations for these concept ids (repeatable, or comma-separated).",
    )

    parser.add_argument(
        "--since",
        help="Only index relations updated at or after this ISO datetime/date.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and count, but do not write to the RAG store.",
    )

    parser.add_argument(
        "--page-size",
        type=int,
        default=5000,
        help="How many raw relations to scan per DB page (default: 5000).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="How many RAG docs to upsert per batch (default: 200).",
    )
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=0,
        help="Maximum raw relations to scan (0 = no limit).",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    namespace = _resolve_namespace(
        namespace=args.namespace, user=args.user, org=args.org
    )
    predicates = _normalise_list(args.predicate)
    languages = _normalise_list(args.language)
    concept_ids = _normalise_list(args.concept_id)

    updated_since = _parse_iso_datetime(args.since) if args.since else None

    page_size = max(int(args.page_size), 1)
    batch_size = max(int(args.batch_size), 1)
    scan_limit = max(int(args.scan_limit), 0)

    # Keep --all as a compatibility flag (it currently has no effect beyond intent).
    _ = bool(args.all)

    return ReindexArgs(
        namespace=namespace,
        predicates=predicates,
        languages=languages,
        concept_ids=concept_ids,
        updated_since=updated_since,
        dry_run=bool(args.dry_run),
        page_size=page_size,
        batch_size=batch_size,
        scan_limit=scan_limit,
    )


def _chunks(
    items: List[TextRelationRagDoc], n: int
) -> Iterable[List[TextRelationRagDoc]]:
    step = max(int(n), 1)
    for i in range(0, len(items), step):
        yield items[i : i + step]


def run_reindex(args: ReindexArgs) -> int:
    rel_filter: dict[str, Any] = {}
    if args.predicates:
        rel_filter["predicate"] = {"$in": list(args.predicates)}
    if args.updated_since is not None:
        rel_filter["updated_at"] = {"$gte": args.updated_since}
    if args.concept_ids:
        rel_filter["subject_concept_id"] = {"$in": list(args.concept_ids)}

    total_raw = TextRelationsRepository.count_documents(rel_filter)
    raw_to_scan = total_raw
    if args.scan_limit:
        raw_to_scan = min(raw_to_scan, args.scan_limit)

    logger.info(
        "Starting reindex: namespace=%s raw_total=%s raw_scan=%s dry_run=%s",
        args.namespace,
        total_raw,
        raw_to_scan,
        args.dry_run,
    )

    rag = None
    if not args.dry_run:
        try:
            rag = get_rag_service()
        except RAGBackendUnavailable as exc:
            logger.error("RAG service unavailable: %s", exc)
            return 2

    total_candidates = 0
    added = 0
    failed = 0

    raw_skip = 0
    sort = [("_id", 1)]

    while raw_skip < raw_to_scan:
        docs = collect_text_relation_docs_for_namespace(
            namespace=args.namespace,
            predicates=args.predicates,
            languages=args.languages,
            concept_ids=args.concept_ids,
            updated_since=args.updated_since,
            skip=raw_skip,
            sort=sort,
            limit=min(args.page_size, raw_to_scan - raw_skip),
        )

        total_candidates += len(docs)

        if rag is not None and docs:
            for batch in _chunks(docs, args.batch_size):
                payload = [
                    {"id": d.doc_id, "text": d.text, "metadata": d.metadata}
                    for d in batch
                ]
                ok, bad = rag.upsert_documents(payload, namespace=args.namespace)
                added += int(ok)
                failed += int(bad)

        raw_skip += args.page_size

        if raw_skip % max(args.page_size * 2, 1) == 0:
            logger.info(
                "Progress: scanned=%s/%s candidates=%s added=%s failed=%s",
                min(raw_skip, raw_to_scan),
                raw_to_scan,
                total_candidates,
                added,
                failed,
            )

    success = failed == 0
    logger.info(
        "Finished reindex: success=%s candidates=%s added=%s failed=%s",
        success,
        total_candidates,
        added,
        failed,
    )

    if args.dry_run:
        return 0

    return 0 if success else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO)

    try:
        args = parse_args(argv)
    except ValueError as exc:
        logger.error("Invalid arguments: %s", exc)
        return 2

    return run_reindex(args)


if __name__ == "__main__":
    raise SystemExit(main())
