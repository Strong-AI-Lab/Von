import os
import json
import re
import threading
import time
from typing import List, Dict, Any, Optional
from ..services import concept_service
from ..prompt.annotation_prompt import AnnotationPromptBuilder
import logging


def _get_llm_client(*args, **kwargs):
    """Load LLM client lazily to avoid circular imports during module load."""
    from ..languagemodels.llm_interface import get_llm_client

    return get_llm_client(*args, **kwargs)


def get_llm_client(*args, **kwargs):
    """Backward-compatible alias so older code/tests can patch module-level factory."""
    return _get_llm_client(*args, **kwargs)


logger = logging.getLogger(__name__)

try:
    from ..db.repositories.concepts_repository import ConceptsRepository
except Exception:  # pragma: no cover - safety import
    ConceptsRepository = None  # type: ignore

# Dynamic phrase candidate cache (names aggregated from concepts visible to one
# actor scope).  A single process-global entry can otherwise retain names read
# under one actor and expose them as matches to the next actor.
_PHRASE_CACHE: Dict[str, Dict[str, Any]] = {}
_PHRASE_CACHE_TTL = 300  # seconds
_PHRASE_MAX = 5000  # safety cap
_PHRASE_CACHE_MAX_SCOPES = 32

# Cached LLM prompt instruction (sourced from special concept description)
_LLM_PROMPT_CACHE: Dict[str, Any] = {"text": None, "ts": 0.0, "source_predicate": None}
_LLM_PROMPT_TTL = 300  # seconds

PROMPT_CONCEPT_ID = "#V#find_concepts_in_text_prompt"
_DEFAULT_PROMPT_PREFIX = "Extract potential entity/concept spans"

# Fallback (non-JSON) LLM span extraction metrics
_FALLBACK_NONJSON_METRIC: Dict[str, Any] = {
    "count": 0,  # total times fallback path used
    "last_time": None,  # iso8601 timestamp of last usage
    "last_prompt_len": None,
    "last_response_len": None,
    "last_concept_id": None,  # prompt concept id used when building prompt
}

# Last LLM prompt/output used for span generation, scoped to the authenticated
# actor that caused the call. Anonymous or unbound calls are deliberately not
# retained because a process-global transparency cache disclosed one actor's
# prompt and output to the next.
_LAST_LLM_IO_BY_ACTOR: Dict[str, Dict[str, Any]] = {}
_LAST_LLM_IO_LOCK = threading.Lock()
_LAST_LLM_IO_MAX_ACTORS = 32


def _llm_io_actor_key() -> str | None:
    from ..security.access_control import (
        get_effective_organisation_concept_id,
        get_effective_user_concept_id,
    )

    user_id = get_effective_user_concept_id()
    if not isinstance(user_id, str) or not user_id.strip():
        return None
    org_id = get_effective_organisation_concept_id()
    return f"user:{user_id.strip()}|org:{str(org_id or 'none').strip()}"


def get_last_llm_io() -> Dict[str, Any]:
    """Return this authenticated actor's last annotation LLM prompt/output."""

    actor_key = _llm_io_actor_key()
    if actor_key is None:
        return {}
    try:  # pragma: no cover - defensive simplicity
        with _LAST_LLM_IO_LOCK:
            entry = _LAST_LLM_IO_BY_ACTOR.get(actor_key)
            return dict(entry) if entry else {}
    except Exception:
        return {}


def _record_last_llm_io(*, prompt: str, output: Any) -> None:
    actor_key = _llm_io_actor_key()
    if actor_key is None:
        return
    entry = {
        "prompt": prompt,
        "output": output if isinstance(output, str) else str(output),
        "ts": int(time.time() * 1000),
    }
    with _LAST_LLM_IO_LOCK:
        if (
            actor_key not in _LAST_LLM_IO_BY_ACTOR
            and len(_LAST_LLM_IO_BY_ACTOR) >= _LAST_LLM_IO_MAX_ACTORS
        ):
            oldest_actor_key = min(
                _LAST_LLM_IO_BY_ACTOR,
                key=lambda key: int(
                    _LAST_LLM_IO_BY_ACTOR[key].get("ts") or 0
                ),
            )
            _LAST_LLM_IO_BY_ACTOR.pop(oldest_actor_key, None)
        _LAST_LLM_IO_BY_ACTOR[actor_key] = entry


def fallback_nonjson_metric_stats() -> Dict[str, Any]:
    try:
        return dict(_FALLBACK_NONJSON_METRIC)
    except Exception:  # pragma: no cover - defensive
        return {"error": "unavailable"}


_PROMPT_BUILDER = AnnotationPromptBuilder(PROMPT_CONCEPT_ID, ttl_sec=_LLM_PROMPT_TTL)


def _map_label_to_concept_id(label: str) -> Optional[str]:
    """Attempt to map a raw type label (from LLM or heuristic) to a Vontology concept id.

    Strategy (lightweight, exact only):
      * Case-insensitive exact match against concept_id (normalised) if user already provided '#V#..'.
      * Case-insensitive exact match against any concept display name / legacy name via suggest_concepts_for_text.
    NOTE: We intentionally avoid fuzzy matching here; add later if needed.
    Returns concept_id or None.
    """
    try:
        if not label or not isinstance(label, str):
            return None
        raw = label.strip()
        if not raw:
            return None
        # If already looks like ontology id pattern, trust it (but normalise case of '#V#').
        if raw.startswith("#V#") and len(raw) > 3:
            return raw  # assume valid; deeper validation could query repository
        # Use suggestion service (prefix + substring) and check for exact name match ignoring case.
        cands = concept_service.suggest_concepts_for_text(raw, limit=12) or []
        low = raw.lower()
        for c in cands:
            nm = c.get("name")
            cid = c.get("concept_id")
            if isinstance(nm, str) and nm.lower() == low and isinstance(cid, str):
                return cid
        # As a fallback, try if any candidate concept_id (stripped '#V#') equals label ignoring case.
        for c in cands:
            cid = c.get("concept_id")
            if isinstance(cid, str):
                core = cid[3:] if cid.startswith("#V#") else cid
                if core.lower() == low:
                    return cid
        return None
    except Exception:
        return None


def _get_llm_prompt_instruction() -> str:
    """Fetch instruction text via AnnotationPromptBuilder (relation-only).

    Eliminates legacy top-level/preserved field fallbacks. Precedence is
    hasContent then hasDescription. Placeholder instructs creation of
    canonical relation text.
    """
    now = time.time()
    if _LLM_PROMPT_CACHE["text"] and now - _LLM_PROMPT_CACHE["ts"] < _LLM_PROMPT_TTL:
        return _LLM_PROMPT_CACHE["text"]  # type: ignore
    instruction = _PROMPT_BUILDER.get_instruction()
    _LLM_PROMPT_CACHE["text"] = instruction
    _LLM_PROMPT_CACHE["ts"] = now
    # builder stats optionally keep source predicate
    try:
        _LLM_PROMPT_CACHE["source_predicate"] = _PROMPT_BUILDER._cache.get("source_predicate")  # type: ignore
    except Exception:
        pass
    return instruction


def prompt_concept_health_status() -> Dict[str, Any]:
    """Return simplified health status using relation-only prompt builder.

    Reasons:
      ok: hasContent/hasDescription present
      missing_text: no canonical relation found
    """
    # Force rebuild
    _PROMPT_BUILDER.invalidate()
    _LLM_PROMPT_CACHE["text"] = None
    _LLM_PROMPT_CACHE["ts"] = 0.0
    instruction = _PROMPT_BUILDER.get_instruction()
    source_pred = _PROMPT_BUILDER._cache.get("source_predicate")  # type: ignore
    available = source_pred is not None
    reason = "ok" if available else "missing_text"
    status = {
        "concept_id": PROMPT_CONCEPT_ID,
        "available": available,
        "source_predicate": source_pred,
        "reason": reason,
        "error": None,
    }
    try:
        logger.info(
            "Prompt concept health: concept_id=%s available=%s source_predicate=%s reason=%s length=%s",
            PROMPT_CONCEPT_ID,
            available,
            source_pred,
            reason,
            len(instruction) if isinstance(instruction, str) else None,
        )
    except Exception:
        pass
    return status


def invalidate_phrase_cache():
    """Invalidate the dynamic phrase cache (call after concept mutations)."""
    _PHRASE_CACHE.clear()


def _phrase_cache_scope_key() -> str:
    from ..security.access_control import cache_scope_key

    return cache_scope_key()


def _phrase_cache_entry(*, now: float) -> Dict[str, Any]:
    scope_key = _phrase_cache_scope_key()
    stale_scope_keys = [
        key
        for key, entry in _PHRASE_CACHE.items()
        if now - float(entry.get("ts") or 0.0) >= _PHRASE_CACHE_TTL
    ]
    for key in stale_scope_keys:
        _PHRASE_CACHE.pop(key, None)

    entry = _PHRASE_CACHE.get(scope_key)
    if entry is not None:
        return entry

    while len(_PHRASE_CACHE) >= _PHRASE_CACHE_MAX_SCOPES:
        oldest_scope_key = min(
            _PHRASE_CACHE,
            key=lambda key: float(_PHRASE_CACHE[key].get("ts") or 0.0),
        )
        _PHRASE_CACHE.pop(oldest_scope_key, None)
    entry = {"phrases": [], "ts": 0.0}
    _PHRASE_CACHE[scope_key] = entry
    return entry


def phrase_cache_stats() -> Dict[str, Any]:
    """Return lightweight stats about the phrase candidate cache.

    Provided for diagnostics endpoint consumption. Does not force rebuild.
    """
    now = time.time()
    entry = _PHRASE_CACHE.get(_phrase_cache_scope_key()) or {}
    phrases = entry.get("phrases") or []
    ts = entry.get("ts", 0.0)
    age = None
    if ts:
        try:
            age = round(now - ts, 2)
        except Exception:
            age = None
    return {
        "size": len(phrases),
        "age_sec": age,
        "ttl_sec": _PHRASE_CACHE_TTL,
        "max": _PHRASE_MAX,
        "warm": bool(phrases) and (age is not None) and (age < _PHRASE_CACHE_TTL),
    }


def get_phrase_candidates() -> List[str]:
    """Aggregate phrase candidates from concept names / names list.

    Returns a cached, lowercased list of multi-token names (contains space) or hyphenated terms.
    Falls back to an empty list if repository unavailable; caller should handle absence.
    """
    now = time.time()
    cache_entry = _phrase_cache_entry(now=now)
    if (
        cache_entry["phrases"]
        and now - cache_entry["ts"] < _PHRASE_CACHE_TTL
    ):
        return cache_entry["phrases"]
    phrases: List[str] = []
    seen = set()
    try:
        if ConceptsRepository is None:
            raise RuntimeError("ConceptsRepository unavailable")
        # Only fetch required fields; avoid large payloads
        cursor = ConceptsRepository.find(
            {}, {"name": 1, "names.name": 1}, limit=_PHRASE_MAX
        )
        for doc in cursor:
            # top-level legacy name
            raw_names: List[Optional[str]] = []
            nm = doc.get("name")
            if isinstance(nm, str):
                raw_names.append(nm)
            # names array
            for entry in doc.get("names", []) or []:
                if isinstance(entry, dict):
                    val = entry.get("name")
                    if isinstance(val, str):
                        raw_names.append(val)
            for name in raw_names:
                if not isinstance(name, str):
                    continue
                norm = name.strip().lower()
                if not norm:
                    continue
                # Only multi-word or hyphenated or length > 6 to reduce noise
                if (" " in norm) or ("-" in norm) or len(norm) > 6:
                    if norm not in seen:
                        seen.add(norm)
                        phrases.append(norm)
                if len(phrases) >= _PHRASE_MAX:
                    break
            if len(phrases) >= _PHRASE_MAX:
                break
    except Exception as e:
        logger.debug(f"Phrase aggregation failed; using empty list: {e}")
        phrases = []
    cache_entry["phrases"] = phrases
    cache_entry["ts"] = now
    return phrases


def match_extract_spans(text: str, max_spans: int = 50) -> List[Dict]:
    """Extract spans by exact concept name (case-insensitive) boundary matching.

    Improvements over the original implementation:
      * Deterministic prioritisation of phrases (longer & multi-word first) to avoid early saturation by generic short terms.
      * Per-phrase match cap so a single frequent phrase cannot monopolise the quota.
      * Collect all raw matches (subject to safety limits) then apply longest-first overlap resolution.
      * Optional debug logging gated by env var `VON_ANNOT_MATCH_DEBUG`.
    """
    if not text:
        return []

    lower_text = text.lower()
    phrases = get_phrase_candidates()
    if not phrases:
        return []

    # Sort: prefer more tokens, then hyphenated, then longer length, then lexical for determinism
    phrases.sort(key=lambda p: (-p.count(" ") - (1 if "-" in p else 0), -len(p), p))

    per_phrase_cap = 3
    try:
        cap_env = os.getenv("VON_ANNOT_PER_PHRASE_CAP")
        if cap_env:
            v = int(cap_env)
            if v > 0:
                per_phrase_cap = min(v, 20)
    except Exception:  # pragma: no cover - defensive
        pass

    debug_enabled = os.getenv("VON_ANNOT_MATCH_DEBUG", "0") in ("1", "true", "True")
    if debug_enabled:
        try:
            logger.info(
                "[annot_match] start text_len=%s phrases=%s max_spans=%s per_phrase_cap=%s first_phrases=%s",
                len(text),
                len(phrases),
                max_spans,
                per_phrase_cap,
                phrases[:15],
            )
        except Exception:
            pass

    raw_spans: List[Dict[str, Any]] = []
    total_scan_limit = max_spans * 20  # safety to avoid pathological explosion

    for phrase in phrases:
        if len(raw_spans) >= total_scan_limit:
            break
        safe = re.escape(phrase)
        pattern = re.compile(rf"(?<![A-Za-z]){safe}(?![A-Za-z])")
        matches_for_phrase = 0
        for m in pattern.finditer(lower_text):
            raw_spans.append(
                {
                    "start": m.start(),
                    "end": m.end(),
                    "text": text[m.start() : m.end()],
                    "phrase": phrase,
                }
            )
            matches_for_phrase += 1
            if (
                matches_for_phrase >= per_phrase_cap
                or len(raw_spans) >= total_scan_limit
            ):
                break

    if not raw_spans:
        return []

    # De-duplicate exact duplicates quickly
    uniq_index = {}
    for sp in raw_spans:
        k = (sp["start"], sp["end"], sp["text"].lower())
        if k not in uniq_index:
            uniq_index[k] = sp
    dedup_spans = list(uniq_index.values())

    # Longest-first overlap resolution: prefer longer text (then earlier start)
    dedup_spans.sort(key=lambda s: (-(s["end"] - s["start"]), s["start"]))
    selected: List[Dict[str, Any]] = []
    occupied: List[tuple] = []
    for sp in dedup_spans:
        s, e = sp["start"], sp["end"]
        overlap = False
        for os_, oe in occupied:
            if not (e <= os_ or s >= oe):
                overlap = True
                break
        if overlap:
            continue
        selected.append({"start": s, "end": e, "text": sp["text"]})
        occupied.append((s, e))
        if len(selected) >= max_spans:
            break

    # Sort final spans in document order
    selected.sort(key=lambda s: (s["start"], s["end"]))

    if debug_enabled:
        try:
            logger.info(
                "[annot_match] done raw=%s dedup=%s final=%s first_final=%s",
                len(raw_spans),
                len(dedup_spans),
                len(selected),
                selected[:10],
            )
        except Exception:
            pass

    return selected


def enrich_spans_with_candidates(spans: List[Dict]) -> List[Dict]:
    enriched = []
    for span in spans:
        text = span["text"]
        candidates = []
        if hasattr(concept_service, "suggest_concepts_for_text"):
            try:
                candidates = concept_service.suggest_concepts_for_text(text) or []
            except Exception:
                candidates = []
        rec = {"span": span, "candidates": candidates}
        # Surface upstream LLM provided type label (raw) as suggested_type_id so UI can show it (JVNAUTOSCI-614)
        t = span.get("type") if isinstance(span, dict) else None
        if isinstance(t, str) and t.strip():
            rec["suggested_type_id"] = t.strip()
        enriched.append(rec)
    return enriched


def _build_llm_prompt(text: str) -> str:
    instruction = _get_llm_prompt_instruction()
    prompt = f"{instruction}\nText:\n" + text
    try:
        # Truncate long text for logging to avoid massive log lines
        display = prompt if len(prompt) < 1200 else prompt[:1200] + "... [truncated]"
        logger.debug(f"LLM annotation prompt: {display}")
    except Exception:
        pass
    return prompt


def build_prompt_preview(text: str) -> Dict[str, Any]:
    """Return structured prompt preview for UI without triggering LLM call.

    Always succeeds; if the instruction concept is missing a placeholder is used.
    """
    instruction = _get_llm_prompt_instruction()
    prompt = f"{instruction}\nText:\n" + (text or "")
    return {
        "concept_id": PROMPT_CONCEPT_ID,
        "instruction_length": len(instruction),
        "text_length": len(text or ""),
        "prompt_length": len(prompt),
        "instruction": instruction,
        "prompt": prompt,
    }


def build_prompt_preview_with_output(text: str) -> Dict[str, Any]:
    """Return prompt preview plus one-shot LLM raw output for transparency.

    This is intended for UI inspection/debugging, not for span extraction.
    Errors producing output are captured and returned instead of raising.
    """
    preview = build_prompt_preview(text)
    output: Dict[str, Any] = {
        "raw": None,
        "length": 0,
        "truncated": False,
        "timing_ms": None,
        "model": None,
        "error": None,
    }
    start = time.time()
    try:
        client = get_llm_client()
        # Try to expose model name if available
        model_name = getattr(client, "model", None) or getattr(
            client, "model_name", None
        )
        if isinstance(model_name, str):
            output["model"] = model_name
        raw = client.generate(preview["prompt"])
        elapsed = (time.time() - start) * 1000.0
        output["timing_ms"] = round(elapsed, 2)
        if isinstance(raw, str):
            truncated = False
            display = raw
            # Safety truncate extremely long outputs for transport/UI (hard cap 12000 chars)
            if len(display) > 12000:
                display = display[:12000] + "... [truncated]"
                truncated = True
            output["raw"] = display
            output["length"] = len(raw)
            output["truncated"] = truncated
        else:
            output["error"] = "non-string output from LLM"
    except Exception as e:  # pragma: no cover - defensive path
        output["error"] = str(e)
        try:
            elapsed = (time.time() - start) * 1000.0
            output["timing_ms"] = round(elapsed, 2)
        except Exception:
            pass
    return {"preview": preview, "output": output}


def _candidate_variants_for_structural_fallback(candidate: str) -> list[str]:
    cleaned = str(candidate or "").strip().strip('"').strip("'").strip()
    if not cleaned:
        return []

    variants: list[str] = [cleaned]
    without_paren = re.sub(r"\s*\([^)]*\)\s*$", "", cleaned).strip()
    if without_paren and without_paren not in variants:
        variants.append(without_paren)

    for pattern in (r"\s*:\s+", r"\s+-\s+", r"\s+–\s+", r"\s+—\s+"):
        parts = [part.strip() for part in re.split(pattern, cleaned, maxsplit=1) if part.strip()]
        if len(parts) < 2:
            continue
        for part in parts:
            if part not in variants:
                variants.append(part)

    variants.sort(key=len, reverse=True)
    return variants


def _find_structural_fallback_span(
    *,
    text: str,
    candidate: str,
) -> tuple[int, int, str] | None:
    if not isinstance(text, str) or not text:
        return None
    lowered_text = text.lower()
    for variant in _candidate_variants_for_structural_fallback(candidate):
        lowered_variant = variant.lower()
        if not lowered_variant:
            continue
        start = lowered_text.find(lowered_variant)
        if start == -1:
            continue
        end = start + len(variant)
        if 0 <= start < end <= len(text):
            return start, end, text[start:end]
    return None


def llm_generate_spans(text: str, max_spans: int = 40) -> List[Dict[str, Any]]:
    try:
        client = get_llm_client()
    except Exception as e:
        logger.debug(f"LLM client unavailable for annotation spans: {e}")
        return []
    prompt = _build_llm_prompt(text)
    try:
        raw = client.generate(prompt)
    except Exception as e:
        logger.debug(f"LLM span generation error: {e}")
        return []
    if not raw:
        return []
    # Record the raw prompt & output for later retrieval (UI transparency)
    try:
        _record_last_llm_io(prompt=prompt, output=raw)
    except Exception:  # pragma: no cover - defensive
        pass
    json_text = raw.strip()
    if "```" in json_text:
        parts = [p for p in json_text.split("```") if "{" in p and "}" in p]
        if parts:
            json_text = parts[0]
    first_brace = json_text.find("{")
    if first_brace > 0:
        json_text = json_text[first_brace:]
    try:
        parsed = json.loads(json_text)
    except Exception as e:
        logger.debug(f"LLM span JSON parse failure: {e}")
        # Fallback: attempt to parse numbered / bulleted list items without
        # introducing language-specific heading or stopword semantics.
        try:
            fallback_lines = []
            raw_lines = raw.splitlines() if isinstance(raw, str) else []
            spans: List[Dict[str, Any]] = []
            item_pattern = re.compile(r"^\s*(\d+\s*[\).:-]|[-*+])\s*(.+)$")
            for line in raw_lines:
                stripped = line.strip()
                if not stripped:
                    continue
                m = item_pattern.match(stripped)
                if m:
                    candidate = m.group(2).strip().strip('"').strip()
                    if candidate and len(candidate) <= 200:
                        fallback_lines.append(candidate)
            # Deduplicate preserving first occurrence (case-insensitive)
            seen_lower = set()
            dedup = []
            for entry in fallback_lines:
                low = entry.lower()
                if low in seen_lower:
                    continue
                seen_lower.add(low)
                dedup.append(entry)

            for entry in dedup:
                located = _find_structural_fallback_span(text=text, candidate=entry)
                if located is None:
                    continue
                start_idx, end_idx, matched_text = located
                spans.append(
                    {
                        "text": matched_text,
                        "start": start_idx,
                        "end": end_idx,
                        "type": None,
                        "source": ["llm", "fallback"],
                    }
                )
                if len(spans) >= max_spans:
                    break
            if spans:
                # Update metrics atomically (best-effort, no synchronisation needed in single-threaded Flask dev env)
                try:
                    from datetime import datetime, timezone as _tz

                    _FALLBACK_NONJSON_METRIC["count"] = (
                        int(_FALLBACK_NONJSON_METRIC.get("count", 0)) + 1
                    )
                    _FALLBACK_NONJSON_METRIC["last_time"] = (
                        datetime.now(_tz.utc).isoformat().replace("+00:00", "Z")
                    )
                    _FALLBACK_NONJSON_METRIC["last_concept_id"] = PROMPT_CONCEPT_ID
                    _FALLBACK_NONJSON_METRIC["last_prompt_len"] = len(prompt)
                    _FALLBACK_NONJSON_METRIC["last_response_len"] = (
                        len(raw) if isinstance(raw, str) else None
                    )
                except Exception:
                    pass
                try:
                    # Retain aggregate diagnostics without copying extracted text.
                    logger.info(
                        "[tag_fallback] using numbered list extraction (no JSON) count=%s prompt_concept=%s prompt_len=%s response_len=%s",
                        len(spans),
                        PROMPT_CONCEPT_ID,
                        len(prompt),
                        len(raw) if isinstance(raw, str) else None,
                    )
                except Exception:
                    pass
                return spans
        except Exception as fe:  # pragma: no cover - defensive fallback path
            logger.debug(f"Fallback list parsing failed: {fe}")
        return []
    spans = []

    def _realign_span(original: str, start: int, txt: str, search_radius: int = 80):
        """If the substring at (start, start+len(txt)) does not match txt (case-insensitive),
        search a local window for the nearest exact occurrence of txt and return adjusted (s,e).
        Returns (start, start+len(txt)) if already aligned or no better match found.
        """
        if not isinstance(original, str) or not isinstance(txt, str) or not txt:
            return start, start + len(txt)
        end = start + len(txt)
        if start >= 0 and end <= len(original):
            if original[start:end].lower() == txt.lower():
                return start, end
        # Local search window
        lo = max(0, start - search_radius)
        hi = min(len(original), start + search_radius + len(txt) + 2)
        window = original[lo:hi]
        candidates = []
        needle_low = txt.lower()
        idx = window.lower().find(needle_low)
        while idx != -1:
            cand_start = lo + idx
            candidates.append(cand_start)
            idx = window.lower().find(needle_low, idx + 1)
            if len(candidates) > 40:  # safety cap
                break
        if not candidates:
            return start, start + len(txt)
        # Choose candidate with minimal absolute distance to provided start
        best = min(candidates, key=lambda cs: abs(cs - start))
        return best, best + len(txt)

    raw_items = parsed.get("spans", []) if isinstance(parsed, dict) else []
    # First pass: individual local realignment
    for item in raw_items:
        try:
            t = item.get("text")
            s = int(item.get("start"))
            e_raw = int(item.get("end"))
            # Normalize end based on text length when possible to reduce minor boundary drift
            if isinstance(t, str) and t:
                e = s + len(t)
                # Guard: if provided end is larger (likely trailing space) but normalization shortens span,
                # trust normalization only when within small delta.
                if e_raw - e > 2:  # large discrepancy; fall back to provided end
                    e = e_raw
            else:
                e = e_raw
            if not t or s < 0 or e <= s or e - s > 200:
                continue
            # Realign if mismatch with original text
            if text:
                adj_start, adj_end = _realign_span(text, s, t)
                s, e = adj_start, adj_end
            spans.append(
                {
                    "text": t,
                    "start": s,
                    "end": e,
                    "type": item.get("type"),
                    "source": ["llm"],
                }
            )
            if len(spans) >= max_spans:
                break
        except Exception:
            continue
    # Second pass: sequential remap to avoid cumulative drift & overlap.
    if spans and text:
        seq = []
        cursor = 0
        for sp in sorted(spans, key=lambda x: (x["start"], x["end"])):
            t = sp["text"]
            if not t:
                continue
            # Search for next occurrence of t at/after cursor (case-insensitive)
            search_region = text[cursor:]
            idx = search_region.lower().find(t.lower())
            if idx == -1:
                # fallback: keep original alignment if it doesn't overlap cursor
                if sp["end"] <= cursor:
                    continue  # already consumed region -> drop duplicate
                start = max(sp["start"], cursor)
                end = min(sp["end"], len(text))
            else:
                start = cursor + idx
                end = start + len(t)
            # Enforce non-overlap and monotonicity
            if end <= start or start < cursor:
                continue
            seq.append(
                {
                    "text": t,
                    "start": start,
                    "end": end,
                    "type": sp.get("type"),
                    "source": sp.get("source", ["llm"]),
                }
            )
            cursor = end
            if len(seq) >= max_spans:
                break
        # If sequential pass produced sensible count (at least half of original), adopt it
        if len(seq) >= max(2, len(spans) // 2):
            spans = seq
    return spans


def _merge_spans(match_spans: List[Dict], llm: List[Dict]) -> List[Dict]:
    index: Dict[tuple, Dict] = {}

    def key(s):
        return (s["start"], s["end"], s["text"].lower())

    def find_near_duplicate(s):
        # Allow slight boundary drift (±1 char) if text matches (case-insensitive)
        txt = s["text"].lower()
        for (st, en, t), val in index.items():
            if t == txt and abs(st - s["start"]) <= 1 and abs(en - s["end"]) <= 1:
                return (st, en, t)
        return None

    for s in match_spans:
        k = key(s)
        s_copy = dict(s)
        s_copy["source"] = ["match"]
        index[k] = s_copy
    for s in llm:
        # If this is a fallback llm span and it overlaps an existing match span, skip to prefer ontology match.
        if "fallback" in (s.get("source") or []):
            s_start, s_end = s.get("start"), s.get("end")
            try:
                if isinstance(s_start, int) and isinstance(s_end, int):
                    overlap_with_match = False
                    for (st, en, t), existing in index.items():
                        if "match" in (existing.get("source") or []):
                            if not (s_end <= st or s_start >= en):
                                overlap_with_match = True
                                break
                    if overlap_with_match:
                        continue
            except Exception:
                pass
        k = key(s)
        existing_key = k if k in index else find_near_duplicate(s)
        if existing_key:
            existing = index[existing_key]
            src = set(existing.get("source", [])) | set(s.get("source", []))
            existing["source"] = list(src)
        else:
            index[k] = dict(s)
    merged = list(index.values())
    merged.sort(key=lambda x: (x["start"], x["end"]))
    return merged


def extract_annotations(
    text: str,
    use_llm: bool | None = None,
    use_match: bool = True,
    return_timings: bool = False,
):
    """Return enriched annotation suggestions.

    Args:
        text: input text.
        use_llm: explicit override for LLM usage; if None, env var governs.
        use_match: if False, skip concept-name match extraction entirely.
    """
    if use_llm is None:
        use_llm = os.getenv("VON_ENABLE_LLM_ANNOTATIONS", "0") in ("1", "true", "True")

    # Determine max spans (env override)
    max_spans_env = os.getenv("VON_ANNOT_MAX_SPANS")
    max_spans = 50  # new default
    if max_spans_env:
        try:
            mv = int(max_spans_env)
            if mv > 0:
                max_spans = min(mv, 500)  # safety upper bound
        except Exception:  # pragma: no cover - defensive
            pass

    timings: Dict[str, float] = {}
    start_total = time.time()

    match_spans: List[Dict] = []
    if use_match and text:
        t0 = time.time()
        match_spans = match_extract_spans(text, max_spans=max_spans)
        timings["match_ms"] = (time.time() - t0) * 1000.0

    llm_spans: List[Dict] = []
    if use_llm and text:
        t0 = time.time()
        llm_spans = llm_generate_spans(text, max_spans=max_spans)
        timings["llm_ms"] = (time.time() - t0) * 1000.0

    # Merge according to enabled sources
    if use_match or use_llm:
        spans = _merge_spans(match_spans, llm_spans)
    else:
        spans = []

    for s in spans:
        try:
            # If we have a raw non-ontology type label, attempt mapping to concept id.
            tval = s.get("type")
            if isinstance(tval, str) and tval and not tval.startswith("#V#"):
                mapped = _map_label_to_concept_id(tval)
                if mapped:
                    s["type"] = mapped
                    s["_mapped_type"] = True
        except Exception:
            continue

    base_spans = []
    for s in spans:
        entry = {"start": s["start"], "end": s["end"], "text": s["text"]}
        # Preserve LLM raw type for downstream enrichment
        tval = s.get("type")
        if isinstance(tval, str):
            tval_stripped = tval.strip()
            if tval_stripped:
                entry["type"] = tval_stripped
        base_spans.append(entry)
    t0_enrich = time.time()
    enriched = enrich_spans_with_candidates(base_spans)
    timings["enrich_ms"] = (time.time() - t0_enrich) * 1000.0
    source_map = {
        (s["start"], s["end"], s["text"].lower()): s.get("source") for s in spans
    }
    for e in enriched:
        span = e.get("span")
        if span:
            key = (span["start"], span["end"], span["text"].lower())
            src = source_map.get(key)
            if src:
                span["source"] = src
    timings["total_ms"] = (time.time() - start_total) * 1000.0
    if return_timings:
        return enriched, timings
    return enriched
