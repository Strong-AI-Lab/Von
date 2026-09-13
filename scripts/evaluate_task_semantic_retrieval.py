"""Evaluate synthetic task retrieval without reading or writing canonical tasks.

The CLI requires explicit opt-in before invoking the configured embedding
provider. Imported evaluate() supports an isolated local/fixture embedder.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.backend.services.task_semantic_index_service import (  # noqa: E402
    _normalise_vector,
    build_task_document,
)


def _metrics(ranked, relevant, k):
    hits = [identity in relevant for identity in ranked[:k]]
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(relevant))))
    return {
        "recall_at_k": sum(hits) / len(relevant),
        "reciprocal_rank": next((1 / (i + 1) for i, hit in enumerate(hits) if hit), 0),
        "ndcg_at_k": sum(hit / math.log2(i + 2) for i, hit in enumerate(hits)) / ideal,
    }


def evaluate(cases, embedder, *, k=3):
    if k < 1:
        raise ValueError("k must be positive")
    started = time.monotonic()
    documents = [
        build_task_document(
            {
                "task_concept_id": task["id"],
                "title": task["title"],
                "description": task["description"],
            }
        )
        for task in cases["tasks"]
    ]
    vectors = [
        _normalise_vector(value)
        for value in embedder.get_text_embedding_batch(
            [document["text"] for document in documents]
        )
    ]
    if len(vectors) != len(documents):
        raise ValueError("Incomplete embeddings")
    build_ms = (time.monotonic() - started) * 1000
    observations = []
    for case in cases["queries"]:
        query_start = time.monotonic()
        vector = _normalise_vector(embedder.get_query_embedding(case["query"]))
        if any(len(candidate) != len(vector) for candidate in vectors):
            raise ValueError("Embedding dimension mismatch")
        ranked = sorted(
            zip(documents, vectors),
            key=lambda item: (
                -sum(a * b for a, b in zip(vector, item[1])),
                item[0]["id"],
            ),
        )
        semantic = [document["id"] for document, _ in ranked]
        # Fair current task-search baseline: case-insensitive substring match.
        lexical = [
            task["id"]
            for task in cases["tasks"]
            if any(
                case["query"].lower() in task[field].lower()
                for field in ("title", "description")
            )
        ]
        observations.append(
            {
                "semantic": _metrics(semantic, set(case["relevant"]), k),
                "lexical": _metrics(lexical, set(case["relevant"]), k),
                "query_elapsed_ms": round((time.monotonic() - query_start) * 1000, 2),
            }
        )
    if not observations:
        raise ValueError("Evaluation queries are required")
    aggregate = {
        mode: {
            key: sum(row[mode][key] for row in observations) / len(observations)
            for key in observations[0][mode]
        }
        for mode in ("semantic", "lexical")
    }
    return {
        "k": k,
        "task_count": len(documents),
        "query_count": len(observations),
        "index_build_ms": round(build_ms, 2),
        "aggregate": aggregate,
        "observations": observations,
        "boundary": "Synthetic relevance evaluation; access correctness is tested separately through canonical task retrieval.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--use-configured-embedder", action="store_true")
    parser.add_argument(
        "--cases",
        type=Path,
        default=REPO_ROOT / "tests/fixtures/task_retrieval/cases.json",
    )
    parser.add_argument("--k", type=int, default=3)
    args = parser.parse_args()
    if not args.use_configured_embedder:
        parser.error(
            "--use-configured-embedder is required to authorise embedding requests"
        )
    from src.backend.services.rag_service import get_rag_service

    summary, model, _ = get_rag_service()._capture_embedding_runtime(
        "task_retrieval_evaluation"
    )
    receipt = evaluate(json.loads(args.cases.read_text()), model, k=args.k)
    receipt["embedding_signature"] = summary["embedding_signature"]
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
