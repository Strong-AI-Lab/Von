"""Dedicated workflow capability index for RAG-first workflow routing.

JVNAUTOSCI-1424 Phase 2: Provides a purpose-built search index for workflow
capabilities, separate from the general concept embedding namespace.  All
registered workflows (built-in + Vontology) are indexed here with rich
capability descriptions so that routing can find the best workflow for any
user request via semantic-like retrieval.

Architecture:
    - ``WorkflowCapabilityIndex``: In-memory BM25-scored index of workflow
      capability documents.  Fast to build, zero external dependencies,
      supports hybrid keyword + relevance matching.
    - ``BUILTIN_WORKFLOW_CAPABILITIES``: Rich descriptions for Python-defined
      workflows that have no Vontology-stored text.  These descriptions are
      keyword-dense for retrieval quality.
    - ``index_from_registry()``: Indexes all workflows from a registry
      (both eager and lazy) using purpose metadata and built-in overrides.

The dedicated namespace isolates workflow routing from general concept search,
enabling independent tuning and scaling as the workflow catalogue grows.
Phase 3 can upgrade to dense vector embeddings while preserving the same API.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

WORKFLOW_CAPABILITY_NAMESPACE = "workflow_capabilities"

# -------------------------------------------------------------------------
# Rich capability descriptions for built-in workflows.
#
# These are deliberately keyword-dense so that BM25 retrieval can match
# diverse user queries.  Each description catalogues: purpose, typical
# triggers, input signals, and output expectations.
# -------------------------------------------------------------------------

BUILTIN_WORKFLOW_CAPABILITIES: Dict[str, str] = {
    "#V#chat_assistant_workflow": (
        "Direct conversational response without tools. "
        "Use for greetings, acknowledgements, simple questions, general chat, "
        "opinions, explanations from existing knowledge, clarifications, "
        "social interactions, follow-up questions, thank-you messages, "
        "and any exchange that does not require external data retrieval, "
        "file operations, API calls, or knowledge-base mutations. "
        "Produces a plain text response without invoking any tools or "
        "performing any side effects."
    ),
    "#V#tool_calling_workflow": (
        "General-purpose tool-calling pipeline for tasks requiring external actions. "
        "Use for MCP tool invocations, knowledge-base queries, web searches, "
        "file operations, downloads, uploads, data mutations, API calls, "
        "concept creation, relationship management, scholarly article processing, "
        "arXiv paper retrieval, RAG synchronisation, code execution, "
        "Jira operations, email interactions, and any task that needs "
        "to read from or write to external systems. Includes plan, validate, "
        "execute, and backfill stages with critic evaluation. "
        "Handles tool_seeking, summarisation, and general tool-dependent tasks."
    ),
    "#V#chat_narration_workflow": (
        "Narrative generation and storytelling pipeline. "
        "Use for generating prose, narrating content, summarising documents, "
        "composing extended text, creating narratives from knowledge, "
        "rendering contextualised descriptions, long-form text generation, "
        "and storytelling. Produces narration output suitable for rendering."
    ),
    "#V#missing_tool_call_workflow": (
        "Recovery workflow for missing tool-call outputs. "
        "Handles retry and recovery when assistant responses lack expected "
        "tool-call results. Internal orchestration recovery mechanism."
    ),
    "#V#chat_buttonify_workflow": (
        "Quick-reply action options output transformation. "
        "Produces interactive button suggestions for chat responses."
    ),
    "#V#todo_refresh_workflow": (
        "Refresh to-do list from Gmail inbox and cached knowledge-base data. "
        "Synchronises task lists and pending items."
    ),
    "#V#write_tool_policy_workflow": (
        "Write-tool policy decision pipeline. "
        "Determines which write tools are permitted for a given chat prompt."
    ),
    "#V#concept_suggestion_preflight_workflow": (
        "Concept suggestion preflight evaluation. "
        "Evaluates specialised fallback concept suggestions for ontology preflight."
    ),
    "#V#kb_mutation_postcondition_critic_workflow": (
        "Knowledge-base mutation postcondition validation. "
        "Evaluates whether implicit KB mutation effects were executed and verified."
    ),
    "#V#turn_completion_gate_workflow": (
        "Turn completion gate policy workflow. "
        "Determines whether it is safe to claim a conversation turn is complete."
    ),
    "#V#conversation_turn_execution_workflow": (
        "Canonical conversation turn execution with critic and completion gate. "
        "Orchestrates the full turn lifecycle including tool calls, "
        "response generation, and completion validation."
    ),
}


# -------------------------------------------------------------------------
# BM25 index implementation
# -------------------------------------------------------------------------

_TOKENISE_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "is", "was", "are", "were", "be", "been", "being",
    "it", "its", "this", "that", "from", "as", "not", "no", "do", "does",
})


def _tokenise(text: str) -> List[str]:
    """Tokenise text into lowercase terms, filtering stop words."""
    return [
        tok for tok in _TOKENISE_RE.findall(text.lower())
        if tok not in _STOP_WORDS and len(tok) > 1
    ]


@dataclass
class _CapabilityEntry:
    workflow_id: str
    text: str
    tokens: List[str]
    token_freqs: Dict[str, int]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowCapabilityMatch:
    """A workflow matched from the capability index."""

    workflow_id: str
    name: str
    description: str
    relevance_score: float
    source: str = "capability_index"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_discovery_dict(self) -> Dict[str, Any]:
        """Convert to the dict format expected by discovery/selector."""
        return {
            "concept_id": self.workflow_id,
            "name": self.name,
            "description": self.description,
            "relevance_score": round(self.relevance_score, 4),
            "match_source": self.source,
        }


class WorkflowCapabilityIndex:
    """In-memory BM25-scored index of workflow capability documents.

    Thread-safe.  Supports ``index_workflow()`` to add entries and
    ``search()`` to retrieve ranked matches for a query string.
    """

    # BM25 tuning parameters.
    _K1 = 1.5
    _B = 0.75

    def __init__(self) -> None:
        self._entries: Dict[str, _CapabilityEntry] = {}
        self._idf: Dict[str, float] = {}
        self._avg_dl: float = 0.0
        self._lock = Lock()

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def index_workflow(
        self,
        workflow_id: str,
        capability_text: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add or replace a workflow capability document."""
        tokens = _tokenise(capability_text)
        entry = _CapabilityEntry(
            workflow_id=workflow_id,
            text=capability_text,
            tokens=tokens,
            token_freqs=dict(Counter(tokens)),
            metadata=metadata or {},
        )
        with self._lock:
            self._entries[workflow_id] = entry
            self._rebuild_idf_unlocked()

    def index_from_registry(self, registry: Any) -> int:
        """Index all workflows from a ``WorkflowRegistry``.

        Uses ``BUILTIN_WORKFLOW_CAPABILITIES`` overrides for built-in
        workflows and falls back to the registration ``purpose`` field.

        Returns the number of workflows indexed.
        """
        count = 0
        # Eager registrations.
        for wid in list(registry.eager_workflow_ids()):
            reg = registry._workflows.get(wid)  # type: ignore[attr-defined]
            purpose = (reg.purpose if reg else None) or ""
            text = BUILTIN_WORKFLOW_CAPABILITIES.get(wid) or purpose
            if not text:
                text = _workflow_id_to_description(wid)
            name = _workflow_id_to_name(wid)
            source = (reg.source if reg else None) or "unknown"
            self.index_workflow(
                wid, text,
                metadata={"name": name, "source": source, "purpose": purpose},
            )
            count += 1

        # Lazy registrations (metadata only, no definition load).
        for wid in list(registry.lazy_workflow_ids()):
            lazy = registry._lazy.get(wid)  # type: ignore[attr-defined]
            purpose = (lazy.purpose if lazy else None) or ""
            text = BUILTIN_WORKFLOW_CAPABILITIES.get(wid) or purpose
            if not text:
                text = _workflow_id_to_description(wid)
            name = _workflow_id_to_name(wid)
            source = (lazy.source if lazy else None) or "unknown"
            self.index_workflow(
                wid, text,
                metadata={"name": name, "source": source, "purpose": purpose},
            )
            count += 1

        logger.info(
            "[workflow_capability_index] Indexed %d workflows "
            "(%d eager, %d lazy)",
            count,
            len(list(registry.eager_workflow_ids())),
            len(list(registry.lazy_workflow_ids())),
        )
        return count

    @property
    def size(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        min_score: float = 0.0,
        exclude_ids: Optional[set[str]] = None,
    ) -> List[WorkflowCapabilityMatch]:
        """Search for workflows matching *query*.

        Returns up to *max_results* matches sorted by BM25 relevance
        score descending.
        """
        query_tokens = _tokenise(query)
        if not query_tokens:
            return []

        with self._lock:
            entries = list(self._entries.values())
            idf = dict(self._idf)
            avg_dl = self._avg_dl

        scored: List[Tuple[float, _CapabilityEntry]] = []
        for entry in entries:
            if exclude_ids and entry.workflow_id in exclude_ids:
                continue
            score = self._bm25_score(query_tokens, entry, idf, avg_dl)
            if score > min_score:
                scored.append((score, entry))

        scored.sort(key=lambda x: -x[0])

        # Normalise scores to 0-1 range for compatibility with discovery.
        max_score = scored[0][0] if scored else 1.0
        if max_score <= 0:
            max_score = 1.0

        results: List[WorkflowCapabilityMatch] = []
        for score, entry in scored[:max_results]:
            normalised = min(1.0, score / max_score)
            name = entry.metadata.get("name") or _workflow_id_to_name(entry.workflow_id)
            results.append(WorkflowCapabilityMatch(
                workflow_id=entry.workflow_id,
                name=name,
                description=entry.text[:300],
                relevance_score=round(normalised, 4),
                source="capability_index",
                metadata=entry.metadata,
            ))
        return results

    # ------------------------------------------------------------------
    # BM25 scoring
    # ------------------------------------------------------------------

    def _rebuild_idf_unlocked(self) -> None:
        """Rebuild IDF table and average document length.  Caller holds lock."""
        n = len(self._entries)
        if n == 0:
            self._idf = {}
            self._avg_dl = 0.0
            return

        doc_freq: Counter[str] = Counter()
        total_tokens = 0
        for entry in self._entries.values():
            total_tokens += len(entry.tokens)
            for tok in set(entry.tokens):
                doc_freq[tok] += 1

        self._avg_dl = total_tokens / n
        self._idf = {
            tok: math.log((n - df + 0.5) / (df + 0.5) + 1.0)
            for tok, df in doc_freq.items()
        }

    @classmethod
    def _bm25_score(
        cls,
        query_tokens: List[str],
        entry: _CapabilityEntry,
        idf: Dict[str, float],
        avg_dl: float,
    ) -> float:
        k1 = cls._K1
        b = cls._B
        dl = len(entry.tokens)
        if avg_dl <= 0:
            avg_dl = 1.0

        score = 0.0
        for tok in set(query_tokens):
            tf = entry.token_freqs.get(tok, 0)
            if tf == 0:
                continue
            tok_idf = idf.get(tok, 0.0)
            numerator = tf * (k1 + 1)
            denominator = tf + k1 * (1 - b + b * dl / avg_dl)
            score += tok_idf * numerator / denominator
        return score


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

def _workflow_id_to_name(workflow_id: str) -> str:
    """Derive a human-readable name from a workflow ID."""
    clean = workflow_id
    if clean.startswith("#V#"):
        clean = clean[3:]
    return clean.replace("_", " ").strip().title()


def _workflow_id_to_description(workflow_id: str) -> str:
    """Derive a minimal fallback description from a workflow ID."""
    name = _workflow_id_to_name(workflow_id)
    return f"{name} workflow."


def build_workflow_capability_text(
    workflow_id: str,
    *,
    purpose: Optional[str] = None,
    description: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    """Build rich searchable text for a workflow capability document.

    Combines built-in overrides, purpose, description, and metadata
    into a single searchable text block.
    """
    # Start with built-in override if available.
    parts: list[str] = []
    builtin_text = BUILTIN_WORKFLOW_CAPABILITIES.get(workflow_id)
    if builtin_text:
        parts.append(builtin_text)

    if purpose and purpose not in (builtin_text or ""):
        parts.append(purpose)
    if description and description not in " ".join(parts):
        parts.append(description)

    if isinstance(metadata, Mapping):
        domain = metadata.get("domain")
        if isinstance(domain, str) and domain.strip():
            parts.append(f"Domain: {domain.strip()}")
        tags = metadata.get("tags")
        if isinstance(tags, (list, tuple)):
            tag_text = ", ".join(
                str(t).strip() for t in tags if isinstance(t, str) and t.strip()
            )
            if tag_text:
                parts.append(f"Tags: {tag_text}")

    if not parts:
        parts.append(_workflow_id_to_description(workflow_id))

    return " ".join(parts)


# -------------------------------------------------------------------------
# Global singleton
# -------------------------------------------------------------------------

_global_index: Optional[WorkflowCapabilityIndex] = None
_global_index_lock = Lock()


def get_workflow_capability_index() -> WorkflowCapabilityIndex:
    """Return the shared workflow capability index singleton.

    Creates an empty index on first call.  Use ``index_from_registry()``
    to populate it after the workflow registry is built.
    """
    global _global_index
    if _global_index is not None:
        return _global_index
    with _global_index_lock:
        if _global_index is not None:
            return _global_index
        _global_index = WorkflowCapabilityIndex()
        return _global_index


def reset_workflow_capability_index() -> None:
    """Reset the global index.  Intended for tests."""
    global _global_index
    with _global_index_lock:
        _global_index = None
