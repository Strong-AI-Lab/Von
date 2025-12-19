"""End-to-end smoke test: web search -> extract -> summarise -> create person concept.

This is intended for local developer verification that Von can:
- fetch academic background from the web (Tavily via tavily-mcp)
- summarise it using the configured LLM (OpenAI/Ollama/etc)
- create a concept instance in the Vontology

By default this runs in read-only mode and prints the payloads it would write.
Use --write to actually create the concept.

NZ English spelling.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

# Allow running as a script from the repo root.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.integrations.internal_mcp.search_proxy_mcp import get_search_proxy
from src.backend.languagemodels.llm_interface import get_llm_client
from src.backend.services.text_value_service import upsert_text_for_concept
from src.backend.vontology.utils_vontology import create_vontology_concept, simulate_or_delete_concept


@dataclass(frozen=True)
class RunResult:
    success: bool
    concept_id: str | None
    parent_id: str | None
    person_type_id: str | None
    top_urls: list[str]
    primary_extract: str | None
    error: str | None = None


def _first_existing_concept_id(candidates: Iterable[str]) -> str | None:
    for candidate in candidates:
        if ConceptsRepository.find_one({"concept_id": candidate}):
            return candidate
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def run(*, person_name: str, write: bool, cleanup: bool) -> RunResult:
    # We want a person instance to be an instance of a *type* concept.
    # In some developer DBs (e.g., test_von_db) the canonical #V#person may not exist,
    # so we create a minimal #V#person type under a known root type.
    person_type_id = "#V#person"
    created_person_type = False

    if not ConceptsRepository.find_one({"concept_id": person_type_id}):
        type_root = _first_existing_concept_id(["#V#type_root", "#V#thing", "#V#one"])
        if not type_root:
            return RunResult(
                success=False,
                concept_id=None,
                parent_id=None,
                person_type_id=None,
                top_urls=[],
                primary_extract=None,
                error="No suitable root type found to create #V#person (tried #V#type_root, #V#thing, #V#one)",
            )

        if write:
            created = create_vontology_concept(
                parent_id=type_root,
                new_concept_name="person",
                create_as_instance=False,
                description="A person (type) created by the academic background smoke test.",
                notes=f"Auto-created for smoke test at {_utc_now_iso()} under parent {type_root}",
            )
            if not created.get("success"):
                return RunResult(
                    success=False,
                    concept_id=None,
                    parent_id=type_root,
                    person_type_id=None,
                    top_urls=[],
                    primary_extract=None,
                    error=f"Failed to create #V#person type: {created}",
                )
            created_person_type = True
        else:
            # Dry-run mode: we can still proceed, but we cannot create the parent type.
            return RunResult(
                success=False,
                concept_id=None,
                parent_id=type_root,
                person_type_id=None,
                top_urls=[],
                primary_extract=None,
                error="Dry-run cannot proceed because #V#person type does not exist. Re-run with --write to allow creating the parent type.",
            )

    proxy = await get_search_proxy()
    query = f"{person_name} academic background education professor"
    search = await proxy.search(query=query, max_results=5, search_depth="basic")
    results = (search or {}).get("results") or []
    if not results:
        return RunResult(
            success=False,
            concept_id=None,
            parent_id=person_type_id,
            person_type_id=person_type_id,
            top_urls=[],
            primary_extract=None,
            error="No web search results returned",
        )

    top_urls = [r.get("url") for r in results if isinstance(r, dict) and r.get("url")][:3]
    primary_url = top_urls[0] if top_urls else None
    if not primary_url:
        return RunResult(
            success=False,
            concept_id=None,
            parent_id=person_type_id,
            person_type_id=person_type_id,
            top_urls=[],
            primary_extract=None,
            error="Search returned results but no URLs",
        )

    extract = await proxy.extract(url=primary_url)
    if not extract.get("success"):
        return RunResult(
            success=False,
            concept_id=None,
            parent_id=person_type_id,
            person_type_id=person_type_id,
            top_urls=top_urls,
            primary_extract=primary_url,
            error=f"Extract failed: {extract.get('error')}",
        )

    extracted_text = (extract.get("content") or "")
    extracted_text = extracted_text[:12000]

    llm = get_llm_client("openai")
    summary_prompt = (
        "Write a concise academic background summary (6-10 sentences) for the person below. "
        "Include education (degrees + institutions if present), current roles, affiliations, and 1-2 major contributions. "
        "Do not invent details; if not in the source, say 'not specified'.\n\n"
        f"Person: {person_name}\n"
        f"Source URL: {primary_url}\n\n"
        "SOURCE TEXT (may be truncated):\n"
        f"{extracted_text}"
    )
    summary = llm.generate(summary_prompt, context=None, model=None)

    notes = "\n".join(
        [
            f"Academic background test run at {_utc_now_iso()}",
            f"Search query: {query}",
            "Top URLs:",
            *[f"- {u}" for u in top_urls],
            f"Primary extract: {primary_url}",
        ]
    )

    proposed_create_payload: dict[str, Any] = {
        "parent_id": person_type_id,
        "new_concept_name": person_name,
        "create_as_instance": True,
        "description": summary,
        "notes": notes,
    }

    proposed_names = [person_name, "Y. Bengio", "Bengio, Yoshua"] if person_name.lower() == "yoshua bengio" else [person_name]

    if not write:
        print("DRY RUN (no writes). Would create concept with:")
        print(proposed_create_payload)
        print("DRY RUN (no writes). Would add names:")
        print(proposed_names)
        return RunResult(
            success=True,
            concept_id=None,
            parent_id=person_type_id,
            person_type_id=person_type_id,
            top_urls=top_urls,
            primary_extract=primary_url,
        )

    create_result = create_vontology_concept(
        parent_id=person_type_id,
        new_concept_name=person_name,
        create_as_instance=True,
        description=summary,
        notes=notes,
    )

    if not create_result.get("success"):
        return RunResult(
            success=False,
            concept_id=None,
            parent_id=person_type_id,
            person_type_id=person_type_id,
            top_urls=top_urls,
            primary_extract=primary_url,
            error=f"Concept creation failed: {create_result}",
        )

    concept = create_result.get("concept") or {}
    concept_id = concept.get("concept_id")

    for name in proposed_names:
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasName",
            text=name,
            lang="en-NZ",
            context={"name_type": "NL"},
        )

    if cleanup and concept_id:
        simulate_or_delete_concept(concept_id, execute=True)
        if created_person_type:
            simulate_or_delete_concept(person_type_id, execute=True)

    return RunResult(
        success=True,
        concept_id=concept_id,
        parent_id=person_type_id,
        person_type_id=person_type_id,
        top_urls=top_urls,
        primary_extract=primary_url,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--person", required=False, default="Yoshua Bengio")
    parser.add_argument("--write", action="store_true", help="Actually create the concept (writes to DB).")
    parser.add_argument("--cleanup", action="store_true", help="After creation, delete the created concept.")
    args = parser.parse_args()

    result = asyncio.run(run(person_name=args.person, write=args.write, cleanup=args.cleanup))

    if result.success:
        print("SUCCESS")
        print(f"parent_id={result.parent_id}")
        print(f"person_type_id={result.person_type_id}")
        print(f"concept_id={result.concept_id}")
        print(f"primary_extract={result.primary_extract}")
        print("top_urls=")
        for url in result.top_urls:
            print(f"- {url}")
        return 0

    print("FAILED")
    print(result.error)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
