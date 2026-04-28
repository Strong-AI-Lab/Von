"""Background rumination for multilingual concept names and descriptions.

This module provides reusable workflow support surfaces: candidate scanning,
grounded usage-profile collection, prompt invocation, conservative write guards,
and audit recording. Translation policy and wording live in the Vontology prompt
linked to the workflow.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import re
from typing import Any, Mapping, Sequence

from ...db.repositories.concepts_repository import ConceptsRepository
from ...languagemodels.llm_interface import get_llm_client
from ...services.concept_usage_profile_service import build_concept_usage_profile
from ...services.multilingual_concept_enrichment_vontology_service import (
    MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
    render_multilingual_concept_translation_prompt,
)
from ...services.text_value_service import (
    get_texts_for_concept,
    upsert_singleton_text_relation,
)
from ...vontology.utils_vontology import get_concept_display_name_with_names_fallback
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from ..workflow_registry import WorkflowRegistration

logger = logging.getLogger(__name__)

DEFAULT_TARGET_LANGUAGES = ("zh-Hans", "es", "fr")
DEFAULT_SCAN_LIMIT = 24
DEFAULT_SCAN_TEXT_LIMIT = 120
DEFAULT_USAGE_RELATION_LIMIT = 200
DEFAULT_MIN_DESCRIPTION_CHARS = 160
DEFAULT_MIN_TOTAL_USAGE = 4
DEFAULT_MAX_MUTATIONS_PER_RUN = 6
DEFAULT_MIN_CONFIDENCE = 0.86
DEFAULT_REANALYSE_AFTER_HOURS = 720
MULTILINGUAL_CONCEPT_ENRICHMENT_AUDIT_FIELD = (
    "multilingual_concept_enrichment_rumination_audit"
)

_NAME_PREDICATE = "#V#hasName"
_DESCRIPTION_PREDICATE = "#V#hasDescription"
_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(?P<body>\{.*\})\s*```",
    re.DOTALL | re.IGNORECASE,
)
_TARGET_LANGUAGE_ALIASES = {
    "zh": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh-hans": "zh-Hans",
    "chinese": "zh-Hans",
    "simplified chinese": "zh-Hans",
    "es": "es",
    "es-es": "es",
    "spanish": "es",
    "fr": "fr",
    "fr-fr": "fr",
    "french": "fr",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_float(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    text = _normalise_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalise_predicate(value: Any) -> str:
    text = _normalise_text(value)
    if text.lower().startswith("#v#"):
        text = text[3:]
    return text.lower()


def _normalise_lang(value: Any) -> str:
    return _normalise_text(value)


def _is_english_language(lang: Any) -> bool:
    text = _normalise_lang(lang).lower()
    return text == "en" or text.startswith("en-")


def _coerce_target_languages(value: Any) -> list[str]:
    if value is None:
        return list(DEFAULT_TARGET_LANGUAGES)
    if isinstance(value, str):
        raw_values: Sequence[Any] = value.split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw_values = value
    else:
        raw_values = ()

    output: list[str] = []
    for item in raw_values:
        text = _normalise_text(item)
        if not text:
            continue
        canonical = _TARGET_LANGUAGE_ALIASES.get(text.lower(), text)
        if canonical not in DEFAULT_TARGET_LANGUAGES:
            continue
        if canonical not in output:
            output.append(canonical)
    return output or list(DEFAULT_TARGET_LANGUAGES)


def _extract_json_payload(raw_text: str) -> tuple[Any, str]:
    text = str(raw_text or "").strip()
    if not text:
        return None, "empty_response"
    match = _JSON_FENCE_RE.search(text)
    if match:
        text = match.group("body").strip()
    try:
        return json.loads(text), "json"
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1]), "json_substring"
            except Exception:
                return None, "invalid_json_substring"
    return None, "invalid_json"


def _review_is_recent(
    concept_doc: Mapping[str, Any],
    *,
    reanalyse_after_hours: int,
) -> bool:
    audit = concept_doc.get(MULTILINGUAL_CONCEPT_ENRICHMENT_AUDIT_FIELD)
    if not isinstance(audit, Mapping):
        return False
    last_reviewed = _parse_datetime(audit.get("last_reviewed_at_utc"))
    if last_reviewed is None:
        return False
    if last_reviewed < datetime.now(timezone.utc) - timedelta(
        hours=reanalyse_after_hours
    ):
        return False
    updated_at = _parse_datetime(concept_doc.get("updated_at"))
    return not (updated_at and updated_at > last_reviewed)


def _text_rows_for_concept(concept_id: str, *, limit: int) -> list[dict[str, Any]]:
    try:
        return [
            row
            for row in (get_texts_for_concept(concept_id, limit=limit) or [])
            if isinstance(row, dict)
        ]
    except Exception as exc:
        logger.debug(
            "[multilingual_concept_enrichment] text scan failed for %s: %s",
            concept_id,
            exc,
        )
        return []


def _slot_exists(
    rows: Sequence[Mapping[str, Any]],
    *,
    predicate_name: str,
    lang: str,
) -> bool:
    target_predicate = _normalise_predicate(predicate_name)
    target_lang = _normalise_lang(lang).lower()
    for row in rows:
        if _normalise_predicate(row.get("predicate")) != target_predicate:
            continue
        if _normalise_lang(row.get("lang")).lower() != target_lang:
            continue
        if _normalise_text(row.get("text")):
            return True
    return False


def _longest_text(rows: Sequence[Mapping[str, Any]]) -> str:
    texts = [_normalise_text(row.get("text")) for row in rows]
    return max((text for text in texts if text), key=len, default="")


def _build_text_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    display_name: str,
    target_languages: Sequence[str],
) -> dict[str, Any]:
    english_names = [
        row
        for row in rows
        if _normalise_predicate(row.get("predicate")) == "hasname"
        and _is_english_language(row.get("lang"))
    ]
    english_descriptions = [
        row
        for row in rows
        if _normalise_predicate(row.get("predicate")) == "hasdescription"
        and _is_english_language(row.get("lang"))
    ]
    source_name = _longest_text(english_names) or display_name
    source_description = _longest_text(english_descriptions)

    existing: dict[str, dict[str, bool]] = {}
    missing: dict[str, list[str]] = {}
    for lang in target_languages:
        has_name = _slot_exists(rows, predicate_name="hasName", lang=lang)
        has_description = _slot_exists(
            rows,
            predicate_name="hasDescription",
            lang=lang,
        )
        existing[lang] = {
            "name": has_name,
            "description": has_description,
        }
        missing_slots: list[str] = []
        if not has_name:
            missing_slots.append("name")
        if not has_description:
            missing_slots.append("description")
        if missing_slots:
            missing[lang] = missing_slots

    return {
        "source_name": source_name,
        "source_description": source_description,
        "source_description_char_count": len(source_description),
        "existing_target_slots": existing,
        "missing_target_slots": missing,
    }


def _build_candidate_summary(
    concept_doc: Mapping[str, Any],
    *,
    target_languages: Sequence[str],
    scan_text_limit: int,
    usage_relation_limit: int,
    minimum_total_usage: int,
    min_description_chars: int,
    reanalyse_after_hours: int,
) -> dict[str, Any] | None:
    concept_id = _normalise_text(concept_doc.get("concept_id"))
    if not concept_id:
        return None
    if _review_is_recent(
        concept_doc,
        reanalyse_after_hours=reanalyse_after_hours,
    ):
        return None

    try:
        display_name = get_concept_display_name_with_names_fallback(dict(concept_doc))
    except Exception:
        display_name = _normalise_text(concept_doc.get("name")) or concept_id

    text_rows = _text_rows_for_concept(concept_id, limit=scan_text_limit)
    text_summary = _build_text_summary(
        text_rows,
        display_name=display_name,
        target_languages=target_languages,
    )
    if not text_summary["missing_target_slots"]:
        return None
    if int(text_summary["source_description_char_count"]) < min_description_chars:
        return None

    usage_profile = build_concept_usage_profile(
        concept_id=concept_id,
        concept_doc=concept_doc,
        relation_limit=usage_relation_limit,
        text_limit=scan_text_limit,
        minimum_total_usage=minimum_total_usage,
    )
    if usage_profile.get("meets_minimum_total_usage") is not True:
        return None

    usage_metrics = dict(usage_profile.get("usage_metrics") or {})
    return {
        "concept_id": concept_id,
        "display_name": display_name or concept_id,
        "source_name": text_summary["source_name"],
        "source_description": text_summary["source_description"],
        "source_description_char_count": text_summary[
            "source_description_char_count"
        ],
        "existing_target_slots": text_summary["existing_target_slots"],
        "missing_target_slots": text_summary["missing_target_slots"],
        "usage_profile": usage_profile,
        "usage_score": int(usage_metrics.get("total_usage_count") or 0),
        "minimum_total_usage": minimum_total_usage,
    }


def _normalise_translation_result(
    payload: Mapping[str, Any],
    *,
    concept_id: str,
    missing_slots: Mapping[str, Sequence[str]],
    target_languages: Sequence[str],
) -> dict[str, Any]:
    confidence = _coerce_float(
        payload.get("confidence"),
        default=0.0,
        minimum=0.0,
        maximum=1.0,
    )
    translations_payload = payload.get("translations")
    translations_source = (
        translations_payload if isinstance(translations_payload, Mapping) else {}
    )

    normalised_translations: dict[str, dict[str, str]] = {}
    for lang in target_languages:
        requested_slots = {
            _normalise_text(slot)
            for slot in (missing_slots.get(lang) or ())
            if _normalise_text(slot) in {"name", "description"}
        }
        if not requested_slots:
            continue
        lang_payload = translations_source.get(lang)
        if not isinstance(lang_payload, Mapping):
            continue
        lang_result: dict[str, str] = {}
        for slot in ("name", "description"):
            if slot not in requested_slots:
                continue
            text = _normalise_text(lang_payload.get(slot))
            if not text:
                continue
            if slot == "description" and len(text) < 20:
                continue
            lang_result[slot] = text
        if lang_result:
            normalised_translations[lang] = lang_result

    payload_concept_id = _normalise_text(payload.get("concept_id"))
    return {
        "schema_version": "multilingual_concept_translation_result.v1",
        "concept_id": concept_id,
        "payload_concept_id": payload_concept_id,
        "concept_id_matches": not payload_concept_id or payload_concept_id == concept_id,
        "confidence": confidence,
        "skip_reason": _normalise_text(payload.get("skip_reason")) or None,
        "translations": normalised_translations,
        "notes": [
            _normalise_text(item)
            for item in (payload.get("notes") or [])
            if _normalise_text(item)
        ][:8],
    }


def _record_audit(concept_id: str, payload: Mapping[str, Any]) -> None:
    audit = {
        "schema_version": "multilingual_concept_enrichment_audit.v1",
        "last_reviewed_at_utc": _utc_now_iso(),
        **dict(payload),
    }
    ConceptsRepository.update_one(
        {"concept_id": concept_id},
        {"$set": {MULTILINGUAL_CONCEPT_ENRICHMENT_AUDIT_FIELD: audit}},
    )


def build_multilingual_concept_enrichment_workflow_test_definition() -> WorkflowDefinition:
    assess = WorkflowStateSpec(
        state_id="assess",
        actions=(
            WorkflowActionInvocation(
                action_id="multilingual_concept_enrichment.assess_candidates",
                description=(
                    "Scan represented concepts for missing target-language name "
                    "or description slots."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="prepare_candidate",
                condition=lambda ctx: bool(ctx.get("multilingual_candidate_found")),
                reason="candidates_found",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="no_candidates",
            ),
        ),
    )
    prepare_candidate = WorkflowStateSpec(
        state_id="prepare_candidate",
        actions=(
            WorkflowActionInvocation(
                action_id="multilingual_concept_enrichment.prepare_candidate",
                description="Select the next multilingual enrichment candidate.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="generate_translations",
                condition=lambda ctx: bool(
                    ctx.get("multilingual_current_candidate_available")
                ),
                reason="candidate_selected",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="no_remaining_candidates",
            ),
        ),
    )
    generate_translations = WorkflowStateSpec(
        state_id="generate_translations",
        actions=(
            WorkflowActionInvocation(
                action_id="multilingual_concept_enrichment.generate_translations",
                description=(
                    "Ask the Vontology-governed translation prompt for missing "
                    "target-language slots."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="apply_translations",
                condition=lambda _ctx: True,
                reason="translation_generation_complete",
            ),
        ),
    )
    apply_translations = WorkflowStateSpec(
        state_id="apply_translations",
        actions=(
            WorkflowActionInvocation(
                action_id="multilingual_concept_enrichment.apply_translations",
                description="Fill only missing target-language text-relation slots.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="prepare_candidate",
                condition=lambda ctx: bool(
                    ctx.get("has_remaining_multilingual_candidates")
                ),
                reason="more_candidates",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="review_budget_exhausted",
            ),
        ),
    )
    complete = WorkflowStateSpec(
        state_id="complete",
        actions=(
            WorkflowActionInvocation(
                action_id="multilingual_concept_enrichment.finalise",
                description="Summarise multilingual concept-enrichment rumination.",
            ),
        ),
        terminal=True,
    )
    failed = WorkflowStateSpec(state_id="failed", terminal=True)
    return WorkflowDefinition(
        workflow_id=MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
        initial_state="assess",
        states={
            "assess": assess,
            "prepare_candidate": prepare_candidate,
            "generate_translations": generate_translations,
            "apply_translations": apply_translations,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose=(
            "Add missing Chinese, Spanish, and French concept names and "
            "descriptions for well-described, non-trivially used concepts."
        ),
    )


def _handle_assess_candidates(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    target_languages = _coerce_target_languages(context.get("target_languages"))
    scan_limit = _coerce_int(
        context.get("scan_limit"),
        default=DEFAULT_SCAN_LIMIT,
        minimum=1,
        maximum=200,
    )
    scan_text_limit = _coerce_int(
        context.get("scan_text_limit"),
        default=DEFAULT_SCAN_TEXT_LIMIT,
        minimum=20,
        maximum=500,
    )
    usage_relation_limit = _coerce_int(
        context.get("usage_relation_limit"),
        default=DEFAULT_USAGE_RELATION_LIMIT,
        minimum=20,
        maximum=1000,
    )
    minimum_total_usage = _coerce_int(
        context.get("minimum_total_usage"),
        default=DEFAULT_MIN_TOTAL_USAGE,
        minimum=1,
        maximum=100000,
    )
    min_description_chars = _coerce_int(
        context.get("min_description_chars"),
        default=DEFAULT_MIN_DESCRIPTION_CHARS,
        minimum=20,
        maximum=5000,
    )
    max_mutations = _coerce_int(
        context.get("max_mutations_per_run"),
        default=DEFAULT_MAX_MUTATIONS_PER_RUN,
        minimum=1,
        maximum=100,
    )
    reanalyse_after_hours = _coerce_int(
        context.get("reanalyse_after_hours"),
        default=DEFAULT_REANALYSE_AFTER_HOURS,
        minimum=1,
        maximum=24 * 365,
    )

    candidate_docs = ConceptsRepository.find(
        {
            "$or": [
                {"relationships.is_a_type_of": {"$exists": True, "$ne": []}},
                {"relationships.is_an_instance_of": {"$exists": True, "$ne": []}},
                {"relationships": {"$exists": True, "$ne": {}}},
            ]
        },
        projection={
            "concept_id": 1,
            "name": 1,
            "names": 1,
            "computed_kind": 1,
            "relationships": 1,
            "updated_at": 1,
            MULTILINGUAL_CONCEPT_ENRICHMENT_AUDIT_FIELD: 1,
        },
        sort=[("updated_at", -1), ("concept_id", 1)],
        limit=max(scan_limit * 8, scan_limit),
    )

    candidate_summaries: list[dict[str, Any]] = []
    scanned_count = 0
    for concept_doc in candidate_docs:
        if not isinstance(concept_doc, Mapping):
            continue
        scanned_count += 1
        summary = _build_candidate_summary(
            concept_doc,
            target_languages=target_languages,
            scan_text_limit=scan_text_limit,
            usage_relation_limit=usage_relation_limit,
            minimum_total_usage=minimum_total_usage,
            min_description_chars=min_description_chars,
            reanalyse_after_hours=reanalyse_after_hours,
        )
        if summary is not None:
            candidate_summaries.append(summary)

    candidate_summaries.sort(
        key=lambda item: (
            -int(item.get("usage_score") or 0),
            -int(item.get("source_description_char_count") or 0),
            str(item.get("concept_id") or ""),
        )
    )
    candidate_summaries = candidate_summaries[:scan_limit]
    candidate_ids = [str(item["concept_id"]) for item in candidate_summaries]
    return WorkflowActionResult(
        outputs={
            "multilingual_candidate_ids": candidate_ids,
            "multilingual_candidate_summaries": candidate_summaries,
            "multilingual_candidate_index": 0,
            "multilingual_candidate_found": bool(candidate_ids),
            "multilingual_target_languages": target_languages,
            "scan_limit_used": scan_limit,
            "scan_text_limit_used": scan_text_limit,
            "usage_relation_limit_used": usage_relation_limit,
            "minimum_total_usage": minimum_total_usage,
            "min_description_chars": min_description_chars,
            "max_mutations_per_run": max_mutations,
            "reanalyse_after_hours": reanalyse_after_hours,
            "scanned_count": scanned_count,
            "reviewed_count": 0,
            "applied_count": 0,
            "would_apply_count": 0,
            "failed_apply_count": 0,
            "no_change_count": 0,
            "translation_records": [],
        }
    )


def _handle_prepare_candidate(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    candidate_ids = [
        _normalise_text(item)
        for item in (context.get("multilingual_candidate_ids") or [])
        if _normalise_text(item)
    ]
    candidate_index = _coerce_int(
        context.get("multilingual_candidate_index"),
        default=0,
        minimum=0,
        maximum=max(len(candidate_ids), 0),
    )
    if candidate_index >= len(candidate_ids):
        return WorkflowActionResult(
            outputs={
                "current_multilingual_candidate_id": None,
                "current_multilingual_candidate_summary": None,
                "multilingual_current_candidate_available": False,
                "has_remaining_multilingual_candidates": False,
            }
        )

    current_candidate_id = candidate_ids[candidate_index]
    summaries = context.get("multilingual_candidate_summaries") or []
    current_summary = next(
        (
            item
            for item in summaries
            if isinstance(item, Mapping)
            and _normalise_text(item.get("concept_id")) == current_candidate_id
        ),
        None,
    )
    return WorkflowActionResult(
        outputs={
            "multilingual_candidate_index": candidate_index + 1,
            "current_multilingual_candidate_id": current_candidate_id,
            "current_multilingual_candidate_summary": dict(current_summary or {}),
            "multilingual_current_candidate_available": True,
            "has_remaining_multilingual_candidates": candidate_index + 1
            < len(candidate_ids),
            "multilingual_translation_result": None,
            "multilingual_translation_status": None,
            "multilingual_translation_actionable": False,
            "multilingual_translation_prompt_diagnostics": None,
            "multilingual_translation_parse_mode": None,
        }
    )


def _handle_generate_translations(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    context = request.data
    concept_id = _normalise_text(context.get("current_multilingual_candidate_id"))
    summary = (
        dict(context.get("current_multilingual_candidate_summary") or {})
        if isinstance(context.get("current_multilingual_candidate_summary"), Mapping)
        else {}
    )
    if not concept_id or not summary:
        return WorkflowActionResult(
            outputs={
                "multilingual_translation_status": "candidate_missing",
                "multilingual_translation_actionable": False,
                "multilingual_translation_result": {
                    "concept_id": concept_id,
                    "confidence": 0.0,
                    "translations": {},
                },
            }
        )

    target_languages = _coerce_target_languages(
        context.get("multilingual_target_languages")
    )
    min_confidence = _coerce_float(
        context.get("min_confidence"),
        default=DEFAULT_MIN_CONFIDENCE,
        minimum=0.5,
        maximum=0.999,
    )
    missing_slots = (
        summary.get("missing_target_slots")
        if isinstance(summary.get("missing_target_slots"), Mapping)
        else {}
    )
    prompt_payload = {
        "workflow_id": MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
        "concept_id": concept_id,
        "target_languages": target_languages,
        "minimum_apply_confidence": min_confidence,
        "candidate": summary,
        "mutation_budget_remaining": max(
            0,
            int(context.get("max_mutations_per_run", DEFAULT_MAX_MUTATIONS_PER_RUN))
            - int(context.get("applied_count", 0)),
        ),
    }
    rendered_prompt, prompt_diagnostics = render_multilingual_concept_translation_prompt(
        workflow_id=MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
        prompt_concept_id=_normalise_text(context.get("prompt_concept_id")) or None,
        variables={
            "translation_payload_json": json.dumps(
                prompt_payload,
                ensure_ascii=True,
                indent=2,
            )
        },
        max_chars=24000,
    )
    if rendered_prompt is None:
        return WorkflowActionResult(
            outputs={
                "multilingual_translation_status": "prompt_unavailable",
                "multilingual_translation_actionable": False,
                "multilingual_translation_prompt_diagnostics": prompt_diagnostics,
                "multilingual_translation_result": {
                    "concept_id": concept_id,
                    "confidence": 0.0,
                    "translations": {},
                    "skip_reason": "prompt unavailable",
                },
            }
        )

    llm = request.environment.llm_client or get_llm_client()
    raw_response = llm.generate(
        rendered_prompt.text,
        llm_params={"max_tokens": 1800},
    )
    parsed_payload, parse_mode = _extract_json_payload(str(raw_response or ""))
    if not isinstance(parsed_payload, Mapping):
        return WorkflowActionResult(
            outputs={
                "multilingual_translation_status": "invalid_json",
                "multilingual_translation_actionable": False,
                "multilingual_translation_prompt_diagnostics": prompt_diagnostics,
                "multilingual_translation_parse_mode": parse_mode,
                "multilingual_translation_result": {
                    "concept_id": concept_id,
                    "confidence": 0.0,
                    "translations": {},
                    "skip_reason": f"translation_json_parse_failed:{parse_mode}",
                },
            }
        )

    result = _normalise_translation_result(
        parsed_payload,
        concept_id=concept_id,
        missing_slots=missing_slots,
        target_languages=target_languages,
    )
    translations = dict(result.get("translations") or {})
    confidence = float(result.get("confidence") or 0.0)
    if not result.get("concept_id_matches"):
        status = "concept_id_mismatch"
        actionable = False
    elif confidence < min_confidence:
        status = "low_confidence"
        actionable = False
    elif not translations:
        status = "no_translations"
        actionable = False
    else:
        status = "actionable"
        actionable = True
    return WorkflowActionResult(
        outputs={
            "multilingual_translation_status": status,
            "multilingual_translation_actionable": actionable,
            "multilingual_translation_prompt_diagnostics": prompt_diagnostics,
            "multilingual_translation_parse_mode": parse_mode,
            "multilingual_translation_result": result,
        }
    )


def _append_record(
    records: Sequence[Mapping[str, Any]],
    record: Mapping[str, Any],
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    items = [dict(item) for item in records if isinstance(item, Mapping)]
    items.append(dict(record))
    return items[-limit:]


def _handle_apply_translations(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    concept_id = _normalise_text(context.get("current_multilingual_candidate_id"))
    translation_result = (
        dict(context.get("multilingual_translation_result") or {})
        if isinstance(context.get("multilingual_translation_result"), Mapping)
        else {}
    )
    status = _normalise_text(context.get("multilingual_translation_status")) or "no_change"
    actionable = bool(context.get("multilingual_translation_actionable"))
    dry_run = bool(context.get("dry_run", False))
    max_mutations = _coerce_int(
        context.get("max_mutations_per_run"),
        default=DEFAULT_MAX_MUTATIONS_PER_RUN,
        minimum=1,
        maximum=100,
    )
    applied_count = int(context.get("applied_count", 0) or 0)
    would_apply_count = int(context.get("would_apply_count", 0) or 0)
    failed_apply_count = int(context.get("failed_apply_count", 0) or 0)
    no_change_count = int(context.get("no_change_count", 0) or 0)
    reviewed_count = int(context.get("reviewed_count", 0) or 0) + 1
    records = list(context.get("translation_records") or [])

    confidence = float(translation_result.get("confidence") or 0.0)
    record: dict[str, Any] = {
        "concept_id": concept_id,
        "status": status,
        "confidence": confidence,
        "dry_run": dry_run,
        "applied_slots": [],
        "skipped_slots": [],
    }
    if not concept_id:
        record["status"] = "candidate_missing"
        no_change_count += 1
        return WorkflowActionResult(
            outputs={
                "reviewed_count": reviewed_count,
                "no_change_count": no_change_count,
                "translation_records": _append_record(records, record),
            }
        )

    translations = (
        translation_result.get("translations")
        if isinstance(translation_result.get("translations"), Mapping)
        else {}
    )
    if not actionable or not translations:
        no_change_count += 1
        record["status"] = status or "no_change"
        try:
            _record_audit(
                concept_id,
                {
                    "last_status": record["status"],
                    "last_confidence": confidence,
                    "target_languages": list(
                        _coerce_target_languages(
                            context.get("multilingual_target_languages")
                        )
                    ),
                },
            )
        except Exception as exc:
            record["audit_error"] = str(exc)
        return WorkflowActionResult(
            outputs={
                "reviewed_count": reviewed_count,
                "no_change_count": no_change_count,
                "translation_records": _append_record(records, record),
                "latest_multilingual_enrichment_action": record,
            }
        )

    if applied_count >= max_mutations:
        record["status"] = "mutation_budget_exhausted"
        no_change_count += 1
        return WorkflowActionResult(
            outputs={
                "reviewed_count": reviewed_count,
                "applied_count": applied_count,
                "would_apply_count": would_apply_count,
                "failed_apply_count": failed_apply_count,
                "no_change_count": no_change_count,
                "translation_records": _append_record(records, record),
                "latest_multilingual_enrichment_action": record,
            }
        )

    current_rows = _text_rows_for_concept(
        concept_id,
        limit=_coerce_int(
            context.get("scan_text_limit_used"),
            default=DEFAULT_SCAN_TEXT_LIMIT,
            minimum=20,
            maximum=500,
        ),
    )
    try:
        for lang, lang_payload in translations.items():
            if not isinstance(lang_payload, Mapping):
                continue
            if applied_count >= max_mutations:
                record["skipped_slots"].append(
                    {"lang": lang, "slot": "*", "reason": "mutation_budget_exhausted"}
                )
                break
            for slot, predicate in (
                ("name", _NAME_PREDICATE),
                ("description", _DESCRIPTION_PREDICATE),
            ):
                text = _normalise_text(lang_payload.get(slot))
                if not text:
                    continue
                if applied_count >= max_mutations:
                    record["skipped_slots"].append(
                        {
                            "lang": lang,
                            "slot": slot,
                            "reason": "mutation_budget_exhausted",
                        }
                    )
                    continue
                if _slot_exists(current_rows, predicate_name=predicate, lang=lang):
                    record["skipped_slots"].append(
                        {
                            "lang": lang,
                            "slot": slot,
                            "reason": "slot_already_exists",
                        }
                    )
                    continue
                if dry_run:
                    would_apply_count += 1
                    record["applied_slots"].append(
                        {"lang": lang, "slot": slot, "dry_run": True}
                    )
                    continue
                upsert_singleton_text_relation(
                    subject_concept_id=concept_id,
                    predicate=predicate,
                    text=text,
                    lang=lang,
                    context={
                        "source": "multilingual_concept_enrichment_rumination_workflow",
                        "workflow_id": MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
                    },
                    provenance={
                        "source": "multilingual_concept_enrichment_rumination_workflow",
                        "confidence": confidence,
                    },
                    garbage_collect=True,
                )
                current_rows.append({"predicate": predicate, "lang": lang, "text": text})
                applied_count += 1
                record["applied_slots"].append({"lang": lang, "slot": slot})
        if not record["applied_slots"]:
            no_change_count += 1
            record["status"] = "no_missing_slots_to_apply"
        else:
            record["status"] = "would_apply" if dry_run else "applied"
        _record_audit(
            concept_id,
            {
                "last_status": record["status"],
                "last_confidence": confidence,
                "applied_slots": record["applied_slots"],
                "skipped_slots": record["skipped_slots"],
            },
        )
    except Exception as exc:
        failed_apply_count += 1
        record["status"] = "apply_failed"
        record["error"] = str(exc)

    return WorkflowActionResult(
        outputs={
            "reviewed_count": reviewed_count,
            "applied_count": applied_count,
            "would_apply_count": would_apply_count,
            "failed_apply_count": failed_apply_count,
            "no_change_count": no_change_count,
            "translation_records": _append_record(records, record),
            "latest_multilingual_enrichment_action": record,
        }
    )


def _handle_finalise(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    result = {
        "success": int(context.get("failed_apply_count", 0) or 0) == 0,
        "candidate_count": len(context.get("multilingual_candidate_ids") or []),
        "scanned_count": int(context.get("scanned_count", 0) or 0),
        "reviewed_count": int(context.get("reviewed_count", 0) or 0),
        "applied_count": int(context.get("applied_count", 0) or 0),
        "would_apply_count": int(context.get("would_apply_count", 0) or 0),
        "failed_apply_count": int(context.get("failed_apply_count", 0) or 0),
        "no_change_count": int(context.get("no_change_count", 0) or 0),
        "target_languages": list(context.get("multilingual_target_languages") or []),
        "minimum_total_usage": context.get("minimum_total_usage"),
        "translation_records": list(context.get("translation_records") or []),
    }
    return WorkflowActionResult(
        outputs={"multilingual_concept_enrichment_result": result}
    )


def build_multilingual_concept_enrichment_workflow_test_registration() -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
        definition=build_multilingual_concept_enrichment_workflow_test_definition(),
        purpose=(
            "Add missing Chinese, Spanish, and French concept names and "
            "descriptions for well-described, non-trivially used concepts."
        ),
        source="built_in",
    )


def register_multilingual_concept_enrichment_actions(registry: ActionRegistry) -> None:
    actions = [
        ActionSpec(
            action_id="multilingual_concept_enrichment.assess_candidates",
            handler=_handle_assess_candidates,
            description=(
                "Scan concepts for missing target-language text slots and "
                "workflow-supplied usage thresholds."
            ),
            side_effects="read_only",
        ),
        ActionSpec(
            action_id="multilingual_concept_enrichment.prepare_candidate",
            handler=_handle_prepare_candidate,
            description="Select the next multilingual concept-enrichment candidate.",
            side_effects="none",
        ),
        ActionSpec(
            action_id="multilingual_concept_enrichment.generate_translations",
            handler=_handle_generate_translations,
            description="Generate candidate multilingual text through a Vontology prompt.",
            side_effects="none",
        ),
        ActionSpec(
            action_id="multilingual_concept_enrichment.apply_translations",
            handler=_handle_apply_translations,
            description="Upsert only missing multilingual concept text relations.",
            side_effects="write",
        ),
        ActionSpec(
            action_id="multilingual_concept_enrichment.finalise",
            handler=_handle_finalise,
            description="Summarise multilingual concept-enrichment rumination.",
            side_effects="none",
        ),
    ]
    for action in actions:
        registry.register_if_absent(action)


__all__ = [
    "DEFAULT_MAX_MUTATIONS_PER_RUN",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_MIN_DESCRIPTION_CHARS",
    "DEFAULT_MIN_TOTAL_USAGE",
    "DEFAULT_REANALYSE_AFTER_HOURS",
    "DEFAULT_SCAN_LIMIT",
    "DEFAULT_SCAN_TEXT_LIMIT",
    "DEFAULT_TARGET_LANGUAGES",
    "DEFAULT_USAGE_RELATION_LIMIT",
    "MULTILINGUAL_CONCEPT_ENRICHMENT_AUDIT_FIELD",
    "build_multilingual_concept_enrichment_workflow_test_definition",
    "build_multilingual_concept_enrichment_workflow_test_registration",
    "register_multilingual_concept_enrichment_actions",
]
