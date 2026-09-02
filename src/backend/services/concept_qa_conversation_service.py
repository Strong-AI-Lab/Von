"""Canonical actor-scoped conversation carrier for concept Q&A.

The historical concept interaction store remains readable through its existing
service and routes.  New Q&A sessions are ordinary ``chat_history`` sessions
with a focal concept, typed mode/origin metadata, explicit lifecycle state and
per-turn representation receipts.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

from ..vontology.utils_vontology import get_concept_display_name_with_names_fallback
from . import (
    chat_history_service,
    concept_resolution_service,
    concept_service,
    scoped_assertion_service,
)
from .conversation_management_service import build_canonical_conversation_reference

SESSION_SCHEMA_VERSION = "concept_q_and_a_session.v1"
TURN_RECEIPT_SCHEMA_VERSION = "concept_q_and_a_turn_receipt.v1"
EXACT_INPUT_RECEIPT_SCHEMA_VERSION = "concept_q_and_a_exact_input_receipt.v1"
FORMALISATION_RECEIPT_SCHEMA_VERSION = "concept_q_and_a_formalisation_receipt.v1"
NOTES_RECEIPT_SCHEMA_VERSION = "concept_q_and_a_notes_receipt.v1"
MODE = chat_history_service.CHAT_SESSION_MODE_CONCEPT_Q_AND_A
ORIGIN_KIND = chat_history_service.CHAT_SESSION_ORIGIN_KIND_CONCEPT_Q_AND_A
ACTIVE = "active"
FINISHED = "finished"
CANCELLED = "cancelled"
TERMINAL_STATUSES = frozenset({FINISHED, CANCELLED})
_MAX_SESSIONS = 20


class ConceptQAConversationError(RuntimeError):
    """Base error carrying a stable HTTP-facing reason code."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class ConceptQAConversationNotFound(ConceptQAConversationError):
    """Raised when the actor-scoped session or concept does not exist."""


class ConceptQAConversationConflict(ConceptQAConversationError):
    """Raised for terminal or compare-and-set lifecycle conflicts."""


class InvalidConceptQAConversationInput(ConceptQAConversationError):
    """Raised when a request cannot be admitted to the carrier."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _iso(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_iso(item) for item in value]
    return value


def _normalise_turn_llm_debug_data(value: Any) -> dict[str, Any] | None:
    """Retain rich identities while supporting the generic LLM-link affordance."""

    if not isinstance(value, Mapping):
        return None
    debug = dict(value)
    selected = debug.get("selected")
    if isinstance(selected, Mapping):
        if not debug.get("provider") and selected.get("provider"):
            debug["provider"] = selected.get("provider")
        if not debug.get("model") and selected.get("model"):
            debug["model"] = selected.get("model")
    return debug


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidConceptQAConversationInput(
            f"{field} is required", error_code=f"{field}_required"
        )
    return value.strip()


def _normalise_actor_id(value: Any, *, field: str) -> str:
    actor_id = _required_text(value, field=field)
    if not actor_id.startswith("#V#"):
        raise InvalidConceptQAConversationInput(
            f"{field} must be a trusted Vontology concept ID",
            error_code="authenticated_actor_context_required",
        )
    return actor_id


def _normalise_org_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    return cleaned if cleaned.startswith("#V#") else f"#V#{cleaned.lstrip('#')}"


def _resolve_namespace(
    *, user_id: str, namespace: Any, organisation_concept_id: str | None
) -> str:
    if isinstance(namespace, str) and namespace.strip():
        return namespace.strip()
    from .namespace_service import derive_namespace_for_actor

    derived = derive_namespace_for_actor(user_id, organisation_concept_id)
    if not isinstance(derived, str) or not derived.strip():
        raise ConceptQAConversationError(
            "Could not derive the Q&A conversation namespace",
            error_code="conversation_namespace_unavailable",
        )
    return derived.strip()


def _load_focal_concept(
    concept_id: str,
    *,
    user_id: str,
    organisation_concept_id: str | None,
) -> dict[str, Any]:
    requested = _required_text(concept_id, field="concept_id")
    try:
        if requested.startswith("#V#"):
            concept = concept_service.get_concept_by_concept_id(requested)
        else:
            concept = concept_service.get_concept_by_id(requested)
            if concept is None:
                concept = concept_service.get_concept_by_concept_id(f"#V#{requested}")
    except Exception as exc:
        raise ConceptQAConversationNotFound(
            "Focal concept was not found", error_code="concept_not_found"
        ) from exc
    if not isinstance(concept, dict):
        raise ConceptQAConversationNotFound(
            "Focal concept was not found", error_code="concept_not_found"
        )
    canonical_id = str(concept.get("concept_id") or "").strip()
    if not canonical_id.startswith("#V#"):
        raise ConceptQAConversationError(
            "Focal concept has no canonical concept ID",
            error_code="canonical_concept_id_unavailable",
        )
    try:
        from ..security.access_control import can_access_concept, override_current_actor

        with override_current_actor(user_id, organisation_concept_id):
            visible = can_access_concept(canonical_id)
    except Exception:  # noqa: BLE001 - inaccessible and absent must be indistinguishable
        visible = False
    if not visible:
        # Do not distinguish a private concept from an absent one.
        raise ConceptQAConversationNotFound(
            "Focal concept was not found", error_code="concept_not_found"
        )
    return concept


def _active_key(*, user_id: str, namespace: str, concept_id: str) -> str:
    digest = hashlib.sha256(
        f"{user_id}\x00{namespace}\x00{concept_id}\x00{MODE}".encode()
    ).hexdigest()
    return f"concept_q_and_a:{digest}"


def _collection(*, read_only: bool = False):
    collection = chat_history_service.get_chat_history_collection_service(
        read_only=read_only
    )
    if collection is None:
        raise ConceptQAConversationError(
            "Conversation store is unavailable",
            error_code="conversation_store_unavailable",
        )
    return collection


def _actor_query(
    *, user_id: str, namespace: str, session_id: str | None = None
) -> dict[str, Any]:
    query = chat_history_service.build_chat_history_query(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_legacy=True,
    )
    query.update({"mode": MODE, "origin_kind": ORIGIN_KIND})
    return query


def _find_session(
    *, user_id: str, namespace: str, session_id: str
) -> dict[str, Any] | None:
    try:
        return _collection(read_only=True).find_one(
            _actor_query(
                user_id=user_id,
                namespace=namespace,
                session_id=session_id,
            )
        )
    except PyMongoError as exc:
        raise ConceptQAConversationError(
            "Could not read the Q&A conversation",
            error_code="conversation_read_failed",
        ) from exc


def _session_lifecycle(doc: Mapping[str, Any]) -> dict[str, Any]:
    qa_state = doc.get("concept_q_and_a")
    lifecycle = qa_state.get("lifecycle") if isinstance(qa_state, Mapping) else None
    if not isinstance(lifecycle, Mapping):
        return {"status": None, "revision": None}
    return _iso(dict(lifecycle))


def _session_concept(doc: Mapping[str, Any]) -> dict[str, Any]:
    qa_state = doc.get("concept_q_and_a")
    concept = qa_state.get("concept") if isinstance(qa_state, Mapping) else None
    return _iso(dict(concept)) if isinstance(concept, Mapping) else {}


def _structured_turns(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    history = doc.get("history") if isinstance(doc.get("history"), list) else []
    qa_state = doc.get("concept_q_and_a")
    receipt_map = (
        qa_state.get("turn_receipts") if isinstance(qa_state, Mapping) else None
    )
    receipts = receipt_map if isinstance(receipt_map, Mapping) else {}
    turns: list[dict[str, Any]] = []
    for history_index, raw_entry in enumerate(history):
        if not isinstance(raw_entry, Mapping):
            continue
        entry = _iso(dict(raw_entry))
        turn_id = entry.get("turn_id")
        projected = {
            **entry,
            "history_location": {
                "session_id": doc.get("session_id"),
                "history_index": history_index,
            },
        }
        if isinstance(turn_id, str) and isinstance(receipts.get(turn_id), Mapping):
            projected["receipts"] = _iso(dict(receipts[turn_id]))
        turns.append(projected)
    return turns


def _project_session(doc: Mapping[str, Any]) -> dict[str, Any]:
    user_id = str(doc.get("user_id") or "")
    session_id = str(doc.get("session_id") or "")
    namespace = doc.get("namespace")
    organisation_concept_id = doc.get("organisation_concept_id")
    qa_state = doc.get("concept_q_and_a")
    turn_receipts = (
        qa_state.get("turn_receipts") if isinstance(qa_state, Mapping) else {}
    )
    canonical_reference = build_canonical_conversation_reference(
        owner_user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        organisation_concept_id=organisation_concept_id,
    )
    return {
        "schema_version": SESSION_SCHEMA_VERSION,
        "session_id": session_id,
        "interaction_id": session_id,
        "session_name": doc.get("session_name"),
        "mode": doc.get("mode"),
        "origin_kind": doc.get("origin_kind"),
        "namespace": namespace,
        "organisation_concept_id": organisation_concept_id,
        "focal_concept_ids": list(doc.get("focal_concept_ids") or []),
        "concept": _session_concept(doc),
        "lifecycle": _session_lifecycle(doc),
        "llm_selection": _iso(qa_state.get("llm_selection"))
        if isinstance(qa_state, Mapping)
        else None,
        "suppressed_predicate_ids": list(qa_state.get("suppressed_predicate_ids") or [])
        if isinstance(qa_state, Mapping)
        else [],
        "suppressed_requirement_keys": list(
            qa_state.get("suppressed_requirement_keys") or []
        )
        if isinstance(qa_state, Mapping)
        else [],
        "suppressed_requirements": _iso(
            list(qa_state.get("suppressed_requirements") or [])
        )
        if isinstance(qa_state, Mapping)
        else [],
        "addressed_requirement_keys": list(
            qa_state.get("addressed_requirement_keys") or []
        )
        if isinstance(qa_state, Mapping)
        else [],
        "requirement_dispositions": _iso(
            list(qa_state.get("requirement_dispositions") or [])
        )
        if isinstance(qa_state, Mapping)
        else [],
        "created_at": _iso(doc.get("created_at")),
        "updated_at": _iso(doc.get("updated_at")),
        "conversation_reference": canonical_reference,
        "conversation_ref": {
            "kind": "von_conversation_ref",
            "schema_version": "conversation_reference.v1",
            "conversation_ref": canonical_reference,
        },
        "turns": _structured_turns(doc),
        "turn_receipts": _iso(dict(turn_receipts))
        if isinstance(turn_receipts, Mapping)
        else {},
    }


_QUESTION_PREFIXES = (
    "who ",
    "what ",
    "when ",
    "where ",
    "why ",
    "how ",
    "which ",
    "is ",
    "are ",
    "was ",
    "were ",
    "do ",
    "does ",
    "did ",
    "can ",
    "could ",
    "would ",
    "should ",
    "have ",
    "has ",
)
_CONTROL_OR_META_PREFIXES = (
    "please stop",
    "stop ",
    "finish ",
    "cancel ",
    "skip ",
    "next question",
    "go back",
    "i don't know",
    "i do not know",
    "no idea",
    "did you represent",
    "have you represented",
    "record that",
    "save that",
)


def classify_user_input(text: Any) -> dict[str, Any]:
    """Conservatively distinguish asserted knowledge from conversational control."""

    source_text = _required_text(text, field="input_text")
    normalised = " ".join(source_text.casefold().split())
    if normalised.endswith("?") or normalised.startswith(_QUESTION_PREFIXES):
        return {
            "kind": "user_question",
            "asserted_knowledge": False,
            "reason_code": "user_question_is_not_asserted_knowledge",
        }
    if normalised.startswith(_CONTROL_OR_META_PREFIXES):
        return {
            "kind": "meta_or_control",
            "asserted_knowledge": False,
            "reason_code": "meta_or_control_is_not_asserted_knowledge",
        }
    return {
        "kind": "asserted_knowledge",
        "asserted_knowledge": True,
        "reason_code": "user_presented_content_as_knowledge",
    }


def _is_representation_status_query(text: str) -> bool:
    """Recognise narrow meta questions that must be answered from receipts."""

    normalised = " ".join(str(text or "").casefold().split())
    return normalised.startswith(
        (
            "did you represent",
            "have you represented",
            "did you record that",
            "did you store that",
            "did you save that",
            "is that represented",
            "is this represented",
            "was that represented",
            "was this represented",
        )
    )


def _latest_knowledge_turn_receipt(
    doc: Mapping[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """Find the latest prior turn with a material knowledge-write receipt."""

    qa_state = doc.get("concept_q_and_a")
    raw_receipts = (
        qa_state.get("turn_receipts") if isinstance(qa_state, Mapping) else None
    )
    if not isinstance(raw_receipts, Mapping):
        return None, None

    def _is_material(receipt: Mapping[str, Any]) -> bool:
        exact = receipt.get("exact_input")
        if isinstance(exact, Mapping) and exact.get("status") in {
            "stored",
            "partial_or_failed",
        }:
            return True
        formalisation = receipt.get("formalisation")
        return isinstance(formalisation, Mapping) and formalisation.get("status") in {
            "tentative",
            "asserted",
            "deferred",
            "rejected",
        }

    history = doc.get("history") if isinstance(doc.get("history"), list) else []
    for entry in reversed(history):
        if not isinstance(entry, Mapping) or entry.get("role") != "user":
            continue
        turn_id = str(entry.get("turn_id") or "").strip()
        receipt = raw_receipts.get(turn_id) if turn_id else None
        if isinstance(receipt, Mapping) and _is_material(receipt):
            return turn_id, dict(receipt)
    for turn_id, receipt in reversed(list(raw_receipts.items())):
        if isinstance(receipt, Mapping) and _is_material(receipt):
            return str(turn_id), dict(receipt)
    return None, None


def _representation_status_from_receipts(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Produce a deterministic, qualified answer about prior representation."""

    source_turn_id, receipt = _latest_knowledge_turn_receipt(doc)
    if receipt is None:
        return {
            "schema_version": "concept_q_and_a_representation_status.v1",
            "status": "no_receipt",
            "source_turn_id": None,
            "message": (
                "I do not have a durable representation receipt for an earlier "
                "factual input in this Q&A, so I cannot claim that it was "
                "represented as knowledge."
            ),
        }

    exact = receipt.get("exact_input")
    exact = dict(exact) if isinstance(exact, Mapping) else {}
    formalisation = receipt.get("formalisation")
    formalisation = dict(formalisation) if isinstance(formalisation, Mapping) else {}
    notes = receipt.get("notes")
    notes = dict(notes) if isinstance(notes, Mapping) else {}
    assertion_ids = [
        str(value)
        for value in exact.get("assertion_ids") or []
        if isinstance(value, str) and value
    ]
    exact_status = str(exact.get("status") or "unknown")
    formalisation_status = str(formalisation.get("status") or "not_attempted")
    predicate_id = str(formalisation.get("predicate_concept_id") or "").strip()
    activation_effect = str(formalisation.get("activation_effect") or "").strip()

    if exact_status == "stored":
        parts = [
            (
                "Your exact wording was stored as an actor-scoped text claim with "
                "provenance."
            )
        ]
    else:
        parts = [
            (
                "Your words remain in the conversation transcript, but the exact "
                "scoped text-claim write did not succeed; I therefore cannot say "
                "they were represented as knowledge."
            )
        ]

    if formalisation_status == "tentative":
        relation = f" {predicate_id}" if predicate_id else ""
        parts.append(
            f"A typed{relation} relation was also stored as tentative; it is "
            "not confirmed or available to asserted-only consumers."
        )
        if activation_effect == "organisation_membership":
            parts.append("It does not activate organisation membership.")
    elif formalisation_status == "asserted":
        relation = f" {predicate_id}" if predicate_id else ""
        parts.append(
            f"The actor-scoped typed{relation} relation was confirmed and read "
            "back as asserted; it was not published as a canonical concept relation."
        )
    elif formalisation_status == "rejected":
        parts.append("The tentative typed relation was rejected.")
    else:
        reason_code = str(formalisation.get("reason_code") or "").strip()
        reason_suffix = f" ({reason_code})" if reason_code else ""
        parts.append(
            "It was not autoformalised as a typed relation" + reason_suffix + "."
        )

    if notes.get("notes_updated") is True:
        parts.append("The receipt says the canonical concept notes were also updated.")
    else:
        parts.append("The canonical concept notes were not changed by that turn.")

    return {
        "schema_version": "concept_q_and_a_representation_status.v1",
        "status": "receipt_found",
        "source_turn_id": source_turn_id,
        "exact_input_status": exact_status,
        "exact_assertion_ids": assertion_ids,
        "formalisation_status": formalisation_status,
        "formalisation_assertion_id": formalisation.get("assertion_id"),
        "predicate_concept_id": predicate_id or None,
        "canonical_publication": formalisation.get("canonical_publication") is True,
        "concept_notes_status": notes.get("status"),
        "concept_notes_updated": notes.get("notes_updated") is True,
        "message": " ".join(parts),
    }


def _previous_assistant_context(doc: Mapping[str, Any]) -> tuple[str, dict | None]:
    history = doc.get("history") if isinstance(doc.get("history"), list) else []
    for entry in reversed(history):
        if not isinstance(entry, Mapping) or entry.get("role") != "assistant":
            continue
        content = str(entry.get("content") or "").strip()
        metadata = entry.get("concept_q_and_a")
        predicate = (
            metadata.get("elicitation_predicate")
            if isinstance(metadata, Mapping)
            else None
        )
        return content or "What would you like to tell me?", (
            dict(predicate) if isinstance(predicate, Mapping) else None
        )
    return "What would you like to tell me?", None


def _exact_input_item(
    *,
    session_id: str,
    turn_id: str,
    input_kind: str,
    text: str,
    concept: Mapping[str, Any],
    session_doc: Mapping[str, Any],
    previous_prompt: str,
    proposed_predicate: Mapping[str, Any] | None,
    transcript_only_reason: str | None = None,
) -> dict[str, Any]:
    classification = classify_user_input(text)
    if transcript_only_reason:
        classification = {
            "kind": "meta_or_control",
            "asserted_knowledge": False,
            "reason_code": transcript_only_reason,
        }
    base = {
        "schema_version": EXACT_INPUT_RECEIPT_SCHEMA_VERSION,
        "kind": "exact_input",
        "input_kind": input_kind,
        "source_turn_id": turn_id,
        "classification": classification["kind"],
        "asserted_knowledge": classification["asserted_knowledge"],
        "exact_source_text_preserved": True,
        "canonical_publication": False,
        "target_concept_id": concept.get("concept_id"),
    }
    if not classification["asserted_knowledge"]:
        return {
            **base,
            "status": "not_admitted",
            "effect_status": "not_needed",
            "reason_code": classification["reason_code"],
        }
    try:
        stored = concept_service._store_concept_qa_input_assertion(
            interaction_id=session_id,
            input_kind=input_kind,
            input_ordinal=1,
            question_or_statement=previous_prompt,
            input_text=text,
            concept=concept,
            session=session_doc,
            proposed_predicate=proposed_predicate,
            source_event_id=(
                f"concept_q_and_a:{session_id}:turn:{turn_id}:{input_kind}"
            ),
        )
        return {**base, **stored, "reason_code": classification["reason_code"]}
    except Exception:  # noqa: BLE001 - exact-input receipt must report storage failure
        return {
            **base,
            "status": "failed",
            "effect_status": "failed",
            "error_code": "exact_input_assertion_write_failed",
            "reason_code": classification["reason_code"],
        }


def _aggregate_exact_input_receipt(
    *,
    session_id: str,
    turn_id: str,
    answer: str,
    notes_input: str,
    concept: Mapping[str, Any],
    session_doc: Mapping[str, Any],
    transcript_only_reason: str | None = None,
) -> dict[str, Any]:
    previous_prompt, proposed_predicate = _previous_assistant_context(session_doc)
    items = []
    if answer:
        items.append(
            _exact_input_item(
                session_id=session_id,
                turn_id=turn_id,
                input_kind="answer",
                text=answer,
                concept=concept,
                session_doc=session_doc,
                previous_prompt=previous_prompt,
                proposed_predicate=proposed_predicate,
                transcript_only_reason=transcript_only_reason,
            )
        )
    if notes_input:
        items.append(
            _exact_input_item(
                session_id=session_id,
                turn_id=turn_id,
                input_kind="notes",
                text=notes_input,
                concept=concept,
                session_doc=session_doc,
                previous_prompt="User-provided concept notes during Q&A",
                proposed_predicate=proposed_predicate if not answer else None,
                transcript_only_reason=transcript_only_reason,
            )
        )
    stored_ids = [
        str(item.get("assertion_id"))
        for item in items
        if item.get("status") == "stored" and item.get("assertion_id")
    ]
    if any(item.get("status") == "failed" for item in items):
        status, effect_status = "partial_or_failed", "failed"
    elif stored_ids:
        status, effect_status = "stored", "succeeded"
    else:
        status, effect_status = "not_admitted", "not_needed"
    return {
        "schema_version": EXACT_INPUT_RECEIPT_SCHEMA_VERSION,
        "kind": "exact_input",
        "status": status,
        "effect_status": effect_status,
        "assertion_ids": stored_ids,
        "items": items,
        "exact_source_text_preserved": True,
        "canonical_publication": False,
    }


def _confirmation_action(*, concept_id: str, session_id: str, assertion_id: str) -> str:
    return (
        f"/api/concepts/{quote(concept_id, safe='')}/q_and_a/sessions/"
        f"{quote(session_id, safe='')}/formalisations/"
        f"{quote(assertion_id, safe='')}/confirm"
    )


def _deferred_formalisation_receipt(
    *,
    exact_input: Mapping[str, Any],
    proposed_predicate: Any,
    reason_code: str,
    resolution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    admitted_items = [
        item
        for item in exact_input.get("items", [])
        if isinstance(item, Mapping) and item.get("status") == "stored"
    ]
    base = {
        "schema_version": FORMALISATION_RECEIPT_SCHEMA_VERSION,
        "kind": "formalisation",
        "canonical_publication": False,
    }
    if not admitted_items:
        return {
            **base,
            "status": "not_applicable",
            "effect_status": "not_needed",
            "reason_code": "no_asserted_knowledge_admitted",
        }
    receipt = {
        **base,
        "status": "deferred",
        "effect_status": "not_started",
        "reason_code": reason_code,
        "candidate": (
            dict(proposed_predicate)
            if isinstance(proposed_predicate, Mapping)
            else None
        ),
        "elicitation_requirement": (
            concept_service._project_elicitation_metadata(proposed_predicate)
            if isinstance(proposed_predicate, Mapping)
            else None
        ),
        "automatic_formalisation_attempted": resolution is not None,
    }
    if isinstance(resolution, Mapping):
        receipt["resolution"] = {
            key: resolution.get(key)
            for key in ("status", "resolved_concept_id", "candidates")
        }
    return receipt


_UNSAFE_ENTITY_ANSWER_RE = re.compile(
    r"(?:[;.!?]|\b(?:and|or|not|no|never|if|unless|maybe|perhaps|possibly|"
    r"could|would|might|should|is|are|was|were|belongs?|members?|works?|"
    r"said|says|according)\b)",
    re.IGNORECASE,
)


def _safe_single_entity_answer(answer: str) -> bool:
    cleaned = " ".join(str(answer or "").split())
    return bool(
        cleaned
        and len(cleaned) <= 160
        and len(cleaned.split()) <= 8
        and not _UNSAFE_ENTITY_ANSWER_RE.search(cleaned)
    )


def _tentative_formalisation_receipt(
    *,
    exact_input: Mapping[str, Any],
    answer: str,
    proposed_predicate: Any,
    concept_id: str,
    concept_name: str,
    session_id: str,
    turn_id: str,
    user_id: str,
    organisation_concept_id: str | None,
    namespace: str,
) -> dict[str, Any]:
    predicate = (
        dict(proposed_predicate) if isinstance(proposed_predicate, Mapping) else None
    )
    predicate_id = str((predicate or {}).get("predicate_concept_id") or "").strip()
    focal_argument = str((predicate or {}).get("focal_argument") or "").strip()
    other_type = str(
        (predicate or {}).get("other_argument_type_concept_id") or ""
    ).strip()
    if not any(
        isinstance(item, Mapping) and item.get("status") == "stored"
        for item in exact_input.get("items", [])
    ):
        return _deferred_formalisation_receipt(
            exact_input=exact_input,
            proposed_predicate=predicate,
            reason_code="no_asserted_knowledge_admitted",
        )
    if (predicate or {}).get("auto_formalisation_allowed") is not True or str(
        (predicate or {}).get("auto_formalisation_target_status") or ""
    ) != "tentative":
        return _deferred_formalisation_receipt(
            exact_input=exact_input,
            proposed_predicate=predicate,
            reason_code="auto_formalisation_permission_missing",
        )
    if not predicate_id.startswith("#V#"):
        return _deferred_formalisation_receipt(
            exact_input=exact_input,
            proposed_predicate=predicate,
            reason_code="no_unambiguous_known_predicate_candidate",
        )
    if focal_argument not in {"subject", "object"} or not other_type.startswith("#V#"):
        return _deferred_formalisation_receipt(
            exact_input=exact_input,
            proposed_predicate=predicate,
            reason_code="predicate_endpoint_contract_incomplete",
        )
    answer_item = next(
        (
            item
            for item in exact_input.get("items", [])
            if isinstance(item, Mapping)
            and item.get("input_kind") == "answer"
            and item.get("status") == "stored"
        ),
        None,
    )
    if answer_item is None:
        return _deferred_formalisation_receipt(
            exact_input=exact_input,
            proposed_predicate=predicate,
            reason_code="no_asserted_answer_available_for_formalisation",
        )
    if not _safe_single_entity_answer(answer):
        return _deferred_formalisation_receipt(
            exact_input=exact_input,
            proposed_predicate=predicate,
            reason_code="answer_is_not_a_safe_single_entity_reference",
        )
    try:
        from ..security.access_control import override_current_actor

        with override_current_actor(user_id, organisation_concept_id):
            resolution = concept_resolution_service.resolve_concept_by_name(
                name=answer,
                instance_of=other_type,
                match_code_strings=False,
                max_results=3,
            )
    except Exception:  # noqa: BLE001 - resolution adapters fail as a deferred candidate
        return _deferred_formalisation_receipt(
            exact_input=exact_input,
            proposed_predicate=predicate,
            reason_code="entity_resolution_failed",
        )
    resolved_id = str(resolution.get("resolved_concept_id") or "").strip()
    if resolution.get("status") != "resolved" or not resolved_id.startswith("#V#"):
        return _deferred_formalisation_receipt(
            exact_input=exact_input,
            proposed_predicate=predicate,
            reason_code=(
                "entity_reference_ambiguous"
                if resolution.get("status") == "ambiguous"
                else "entity_reference_not_found"
            ),
            resolution=resolution,
        )
    subject_id = concept_id if focal_argument == "subject" else resolved_id
    object_id = resolved_id if focal_argument == "subject" else concept_id
    source_assertion_ids = [
        str(item.get("assertion_id"))
        for item in exact_input.get("items", [])
        if isinstance(item, Mapping)
        and item.get("status") == "stored"
        and item.get("assertion_id")
    ]
    try:
        stored = scoped_assertion_service.upsert_scoped_assertion(
            subject_concept_id=subject_id,
            predicate=predicate_id,
            target_concept_id=object_id,
            scope_mode="user",
            evidence={
                "source_kind": "concept_q_and_a_exact_input",
                "source_session_id": session_id,
                "source_turn_id": turn_id,
                "source_assertion_ids": source_assertion_ids,
                "exact_source_text": answer,
                "elicitation_predicate": predicate,
            },
            acting_user_concept_id=user_id,
            organisation_concept_id=organisation_concept_id,
            namespace=namespace,
            turn_id=turn_id,
            canonical_publication=False,
            epistemic_status="tentative",
        )
    except PermissionError:
        return _deferred_formalisation_receipt(
            exact_input=exact_input,
            proposed_predicate=predicate,
            reason_code="formalisation_endpoint_not_visible",
            resolution=resolution,
        )
    except ValueError:
        return _deferred_formalisation_receipt(
            exact_input=exact_input,
            proposed_predicate=predicate,
            reason_code="formalisation_predicate_or_types_incompatible",
            resolution=resolution,
        )
    except Exception:  # noqa: BLE001 - formalisation adapters fail as a deferred candidate
        return _deferred_formalisation_receipt(
            exact_input=exact_input,
            proposed_predicate=predicate,
            reason_code="tentative_formalisation_write_failed",
            resolution=resolution,
        )
    assertion_id = str(stored.get("assertion_id") or "").strip()
    read_back = stored.get("canonical_read_back")
    if (
        not assertion_id
        or not isinstance(read_back, Mapping)
        or read_back.get("epistemic_status") != "tentative"
    ):
        return _deferred_formalisation_receipt(
            exact_input=exact_input,
            proposed_predicate=predicate,
            reason_code="tentative_formalisation_readback_failed",
            resolution=resolution,
        )
    membership_predicate = bool(
        predicate_id.casefold() == "#v#memberof"
        and focal_argument == "object"
        and other_type.casefold() == "#v#von_user"
        and object_id == concept_id
    )
    return {
        "schema_version": FORMALISATION_RECEIPT_SCHEMA_VERSION,
        "kind": "formalisation",
        "status": "tentative",
        "effect_status": "succeeded",
        "reason_code": "safe_tentative_formalisation_stored",
        "assertion_id": assertion_id,
        "subject_concept_id": subject_id,
        "predicate_concept_id": predicate_id,
        "object_concept_id": object_id,
        "focal_argument": focal_argument,
        "other_argument_type_concept_id": other_type,
        "focal_concept_id": concept_id,
        "requirement_id": (predicate or {}).get("requirement_id"),
        "declared_on_type_concept_id": (predicate or {}).get(
            "declared_on_type_concept_id"
        ),
        "inherited": (predicate or {}).get("inherited"),
        "declaration_depth": (predicate or {}).get("declaration_depth"),
        "declaration_source": (predicate or {}).get("declaration_source"),
        "profile_predicate": (predicate or {}).get("profile_predicate"),
        "elicitation_requirement": concept_service._project_elicitation_metadata(
            predicate or {}
        ),
        "focal_concept_name": concept_name,
        "confirmation_question_template": (predicate or {}).get(
            "confirmation_question_template"
        ),
        "source_assertion_ids": source_assertion_ids,
        "exact_source_text_preserved": True,
        "epistemic_status": "tentative",
        "canonical_publication": False,
        "automatic_authority_grant": False,
        "activation_effect": "organisation_membership"
        if membership_predicate
        else "scoped_assertion_epistemic_promotion",
        "canonical_read_back": dict(read_back),
        "confirmation": {
            "available": True,
            "method": "POST",
            "action": _confirmation_action(
                concept_id=concept_id,
                session_id=session_id,
                assertion_id=assertion_id,
            ),
            "requires_operational_authority": membership_predicate,
            "actor_scope": "original_q_and_a_actor",
            "available_after_finish": True,
            "available_after_cancel": False,
        },
    }


def _initial_notes_receipt() -> dict[str, Any]:
    return {
        "schema_version": NOTES_RECEIPT_SCHEMA_VERSION,
        "kind": "notes",
        "status": "not_attempted",
        "effect_status": "not_needed",
        "reason_code": "downstream_processing_not_started",
    }


def _record_turn_receipt(
    *,
    user_id: str,
    namespace: str,
    session_id: str,
    turn_id: str,
    receipt: Mapping[str, Any],
) -> None:
    query = _actor_query(
        user_id=user_id,
        namespace=namespace,
        session_id=session_id,
    )
    try:
        result = _collection().update_one(
            query,
            {
                "$set": {
                    f"concept_q_and_a.turn_receipts.{turn_id}": dict(receipt),
                    "updated_at": _now(),
                }
            },
        )
    except PyMongoError as exc:
        raise ConceptQAConversationError(
            "Could not persist the Q&A representation receipt",
            error_code="turn_receipt_write_failed",
        ) from exc
    if getattr(result, "matched_count", 0) != 1:
        raise ConceptQAConversationNotFound(
            "Q&A conversation was not found", error_code="conversation_not_found"
        )


def _legacy_history_from_chat(
    doc: Mapping[str, Any], *, exclude_turn_id: str | None = None
) -> list[dict[str, Any]]:
    history = doc.get("history") if isinstance(doc.get("history"), list) else []
    legacy: list[dict[str, Any]] = []
    for entry in history:
        if not isinstance(entry, Mapping) or entry.get("turn_id") == exclude_turn_id:
            continue
        role = entry.get("role")
        content = str(entry.get("content") or "")
        metadata = entry.get("concept_q_and_a")
        timestamp = entry.get("timestamp") or _now()
        if role == "assistant":
            interaction_type = (
                "llm_question" if content.rstrip().endswith("?") else "llm_statement"
            )
            detail_key = (
                "question" if interaction_type == "llm_question" else "statement"
            )
            details: dict[str, Any] = {detail_key: content}
            if isinstance(metadata, Mapping) and isinstance(
                metadata.get("elicitation_predicate"), Mapping
            ):
                details["elicitation_predicate"] = dict(
                    metadata["elicitation_predicate"]
                )
        elif role == "user":
            kind = metadata.get("input_kind") if isinstance(metadata, Mapping) else None
            interaction_type = (
                "user_question" if kind == "user_question" else "user_answer"
            )
            details = {"answer": content, "input_kind": kind or "answer"}
        else:
            continue
        legacy.append(
            {
                "interaction_type": interaction_type,
                "details": details,
                "timestamp": timestamp,
            }
        )
    return legacy


def _representation_item(
    exact_input: Mapping[str, Any], input_kind: str
) -> dict[str, Any]:
    for item in exact_input.get("items", []):
        if isinstance(item, Mapping) and item.get("input_kind") == input_kind:
            return dict(item)
    return {
        "status": "not_provided",
        "effect_status": "not_needed",
        "canonical_publication": False,
    }


def _current_elicitation_predicate(concept_id: str) -> dict[str, Any] | None:
    try:
        plan = concept_service._get_concept_elicitation_plan(concept_id)
    except Exception:  # noqa: BLE001 - elicitation is optional fail-soft guidance
        return None
    if not plan or not isinstance(plan[0], Mapping):
        return None
    return concept_service._project_elicitation_metadata(plan[0])


def _suppressed_predicate_ids(doc: Mapping[str, Any]) -> set[str]:
    qa_state = doc.get("concept_q_and_a")
    raw = (
        qa_state.get("suppressed_predicate_ids")
        if isinstance(qa_state, Mapping)
        else []
    )
    return {
        str(item).strip()
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, str) and item.strip()
    }


def _suppressed_requirement_keys(doc: Mapping[str, Any]) -> set[str]:
    qa_state = doc.get("concept_q_and_a")
    raw = (
        qa_state.get("suppressed_requirement_keys")
        if isinstance(qa_state, Mapping)
        else []
    )
    return {
        str(item).strip()
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, str) and item.strip()
    }


def _addressed_requirement_keys(doc: Mapping[str, Any]) -> set[str]:
    qa_state = doc.get("concept_q_and_a")
    raw = (
        qa_state.get("addressed_requirement_keys")
        if isinstance(qa_state, Mapping)
        else []
    )
    return {
        str(item).strip()
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, str) and item.strip()
    }


def _addressed_predicate_ids(doc: Mapping[str, Any]) -> set[str]:
    qa_state = doc.get("concept_q_and_a")
    raw = (
        qa_state.get("addressed_predicate_ids") if isinstance(qa_state, Mapping) else []
    )
    return {
        str(item).strip()
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, str) and item.strip()
    }


def _next_unsuppressed_elicitation_predicate(
    *,
    concept_id: str,
    session_doc: Mapping[str, Any],
    exclude_requirement: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    legacy_suppressed_predicates = _suppressed_predicate_ids(
        session_doc
    ) | _addressed_predicate_ids(session_doc)
    suppressed_requirement_keys = _suppressed_requirement_keys(
        session_doc
    ) | _addressed_requirement_keys(session_doc)
    excluded_requirement_key = (
        concept_service._elicitation_requirement_identity(exclude_requirement)
        if isinstance(exclude_requirement, Mapping)
        else None
    )
    excluded_legacy_predicate = (
        str(exclude_requirement.get("predicate_concept_id") or "").strip()
        if isinstance(exclude_requirement, Mapping) and excluded_requirement_key is None
        else ""
    )
    try:
        plan = concept_service._get_concept_elicitation_plan(concept_id)
    except Exception:  # noqa: BLE001 - elicitation is optional fail-soft guidance
        return None
    for item in plan or []:
        if not isinstance(item, Mapping):
            continue
        predicate_id = str(item.get("predicate_concept_id") or "").strip()
        requirement_key = concept_service._elicitation_requirement_identity(item)
        if requirement_key and requirement_key in suppressed_requirement_keys:
            continue
        if predicate_id and predicate_id in legacy_suppressed_predicates:
            continue
        if excluded_requirement_key and requirement_key == excluded_requirement_key:
            continue
        if excluded_legacy_predicate and predicate_id == excluded_legacy_predicate:
            continue
        return concept_service._project_elicitation_metadata(item)
    return None


def _suppress_confirmation_requirement(
    *,
    user_id: str,
    namespace: str,
    session_id: str,
    formalisation: Mapping[str, Any],
    disposition: str,
) -> None:
    predicate_id = str(formalisation.get("predicate_concept_id") or "").strip()
    if not predicate_id:
        return
    requirement_key = concept_service._elicitation_requirement_identity(formalisation)
    suppression = {
        "requirement_key": requirement_key,
        "requirement_id": formalisation.get("requirement_id"),
        "predicate_concept_id": predicate_id,
        "focal_argument": formalisation.get("focal_argument"),
        "other_argument_type_concept_id": formalisation.get(
            "other_argument_type_concept_id"
        ),
        "declared_on_type_concept_id": formalisation.get("declared_on_type_concept_id"),
        "declaration_source": formalisation.get("declaration_source"),
        "profile_predicate": formalisation.get("profile_predicate"),
        "assertion_id": formalisation.get("assertion_id"),
        "disposition": disposition,
    }
    add_to_set: dict[str, Any] = {
        "concept_q_and_a.suppressed_requirements": suppression,
    }
    if requirement_key:
        add_to_set["concept_q_and_a.suppressed_requirement_keys"] = requirement_key
    else:
        # Legacy confirmation rows did not retain endpoint identity, so the
        # predicate is the narrowest honest compatibility key available.
        add_to_set["concept_q_and_a.suppressed_predicate_ids"] = predicate_id
    try:
        result = _collection().update_one(
            _actor_query(
                user_id=user_id,
                namespace=namespace,
                session_id=session_id,
            ),
            {
                "$addToSet": add_to_set,
                "$set": {"updated_at": _now()},
            },
        )
    except PyMongoError as exc:
        raise ConceptQAConversationError(
            "Could not preserve the Q&A defer/reject disposition",
            error_code="confirmation_disposition_write_failed",
        ) from exc
    if getattr(result, "matched_count", 0) != 1:
        raise ConceptQAConversationNotFound(
            "Q&A conversation was not found", error_code="conversation_not_found"
        )


def _record_confirmed_requirement_disposition(
    *,
    user_id: str,
    namespace: str,
    session_id: str,
    formalisation: Mapping[str, Any],
) -> dict[str, Any]:
    """Prevent a confirmed actor-scoped relation from nagging in this conversation."""

    predicate_id = str(formalisation.get("predicate_concept_id") or "").strip()
    requirement_key = concept_service._elicitation_requirement_identity(formalisation)
    disposition = {
        "requirement_key": requirement_key,
        "requirement_id": formalisation.get("requirement_id"),
        "predicate_concept_id": predicate_id or None,
        "focal_argument": formalisation.get("focal_argument"),
        "other_argument_type_concept_id": formalisation.get(
            "other_argument_type_concept_id"
        ),
        "declared_on_type_concept_id": formalisation.get("declared_on_type_concept_id"),
        "disposition": "confirmed_for_conversation",
        "canonical_requirement_satisfaction_claimed": False,
    }
    add_to_set: dict[str, Any] = {
        "concept_q_and_a.requirement_dispositions": disposition,
    }
    if requirement_key:
        add_to_set["concept_q_and_a.addressed_requirement_keys"] = requirement_key
    elif predicate_id:
        add_to_set["concept_q_and_a.addressed_predicate_ids"] = predicate_id
    else:
        return {
            "status": "not_recorded",
            "reason_code": "requirement_identity_unavailable",
        }
    try:
        result = _collection().update_one(
            _actor_query(
                user_id=user_id,
                namespace=namespace,
                session_id=session_id,
            ),
            {
                "$addToSet": add_to_set,
                "$set": {"updated_at": _now()},
            },
        )
    except PyMongoError:
        return {
            "status": "failed",
            "reason_code": "conversation_requirement_disposition_write_failed",
            "reconciliation_required": True,
        }
    if getattr(result, "matched_count", 0) != 1:
        return {
            "status": "failed",
            "reason_code": "conversation_not_found",
            "reconciliation_required": True,
        }
    return {
        "status": "stored",
        "requirement_key": requirement_key,
        "canonical_requirement_satisfaction_claimed": False,
    }


def _pending_confirmation(doc: Mapping[str, Any]) -> dict[str, Any] | None:
    history = doc.get("history") if isinstance(doc.get("history"), list) else []
    for entry in reversed(history):
        if not isinstance(entry, Mapping):
            continue
        if entry.get("role") == "user":
            return None
        if entry.get("role") != "assistant":
            continue
        metadata = entry.get("concept_q_and_a")
        if not isinstance(metadata, Mapping):
            return None
        formalisation = metadata.get("formalisation")
        if (
            metadata.get("kind") == "formalisation_confirmation_request"
            and isinstance(formalisation, Mapping)
            and str(formalisation.get("assertion_id") or "").startswith("ska_")
        ):
            return dict(formalisation)
        return None
    return None


def _confirmation_reply_kind(text: str) -> str | None:
    normalised = re.sub(r"[^a-z0-9']+", " ", str(text or "").casefold()).strip()
    if normalised in {
        "yes",
        "yes please",
        "confirm",
        "confirmed",
        "correct",
        "that's right",
        "that is right",
        "do it",
    }:
        return "affirm"
    if normalised in {"no", "nope", "incorrect", "that's wrong", "that is wrong"}:
        return "reject"
    if normalised in {
        "later",
        "not now",
        "defer",
        "skip",
        "leave it tentative",
        "i don't know",
        "i do not know",
        "not sure",
        "i'm not sure",
        "i am not sure",
        "unsure",
        "no idea",
    }:
        return "defer"
    return None


def _locate_formalisation_receipt(
    doc: Mapping[str, Any], assertion_id: str
) -> tuple[str | None, dict[str, Any] | None]:
    qa_state = doc.get("concept_q_and_a")
    receipts = qa_state.get("turn_receipts") if isinstance(qa_state, Mapping) else None
    if not isinstance(receipts, Mapping):
        return None, None
    for source_turn_id, raw_receipt in receipts.items():
        if not isinstance(raw_receipt, Mapping):
            continue
        formalisation = raw_receipt.get("formalisation")
        if (
            isinstance(formalisation, Mapping)
            and formalisation.get("assertion_id") == assertion_id
        ):
            return str(source_turn_id), dict(formalisation)
    return None, None


def _record_formalisation_receipt(
    *,
    user_id: str,
    namespace: str,
    session_id: str,
    source_turn_id: str,
    formalisation: Mapping[str, Any],
) -> None:
    try:
        result = _collection().update_one(
            _actor_query(
                user_id=user_id,
                namespace=namespace,
                session_id=session_id,
            ),
            {
                "$set": {
                    f"concept_q_and_a.turn_receipts.{source_turn_id}.formalisation": dict(
                        formalisation
                    ),
                    "updated_at": _now(),
                }
            },
        )
    except PyMongoError as exc:
        raise ConceptQAConversationError(
            "Could not persist the formalisation confirmation receipt",
            error_code="formalisation_confirmation_receipt_write_failed",
        ) from exc
    if getattr(result, "matched_count", 0) != 1:
        raise ConceptQAConversationNotFound(
            "Q&A conversation was not found", error_code="conversation_not_found"
        )


def _confirm_formalisation_effect(
    *,
    formalisation: Mapping[str, Any],
    assertion_id: str,
    user_id: str,
    organisation_concept_id: str | None,
    namespace: str,
    request_id: str,
    expected_focal_concept_id: str | None = None,
) -> dict[str, Any]:
    assertion = scoped_assertion_service.get_visible_scoped_assertion_by_id(
        assertion_id,
        user_concept_id=user_id,
        organisation_concept_id=organisation_concept_id,
    )
    if not isinstance(assertion, Mapping):
        return {
            "success": False,
            "status": "tentative",
            "effect_status": "not_started",
            "error_code": "tentative_formalisation_not_visible",
            "actionable_denial": {
                "mechanism": "ask the original asserting actor to confirm",
            },
        }
    expected = {
        "subject_concept_id": formalisation.get("subject_concept_id"),
        "predicate": formalisation.get("predicate_concept_id"),
        "object_concept_id": formalisation.get("object_concept_id"),
    }
    if any(assertion.get(key) != value for key, value in expected.items()):
        return {
            "success": False,
            "status": "tentative",
            "effect_status": "not_started",
            "error_code": "formalisation_endpoint_readback_mismatch",
        }
    predicate_id = str(formalisation.get("predicate_concept_id") or "").strip()
    focal_concept_id = str(
        expected_focal_concept_id or formalisation.get("focal_concept_id") or ""
    ).strip()
    is_membership = bool(
        predicate_id.casefold() == "#v#memberof"
        and str(formalisation.get("focal_argument") or "").strip() == "object"
        and str(formalisation.get("other_argument_type_concept_id") or "").casefold()
        == "#v#von_user"
        and focal_concept_id
        and str(formalisation.get("object_concept_id") or "").strip()
        == focal_concept_id
    )
    already_asserted = (assertion.get("epistemic_status") or "asserted") == "asserted"
    if already_asserted and not is_membership:
        return {
            "success": True,
            "status": "asserted",
            "effect_status": "succeeded",
            "changed": False,
            "idempotent_replay": True,
            "canonical_read_back": dict(assertion),
        }

    governance: dict[str, Any] | None = None
    if is_membership:
        from .organisation_membership_governance_service import (
            manage_organisation_membership,
        )
        from .organisation_membership_service import (
            resolve_user_organisation_membership,
        )

        member_id = str(formalisation.get("subject_concept_id") or "").strip()
        organisation_id = str(formalisation.get("object_concept_id") or "").strip()

        def _membership_read_back() -> dict[str, Any] | None:
            try:
                membership = resolve_user_organisation_membership(
                    member_id,
                    organisation_id,
                )
            except Exception:  # noqa: BLE001 - mutation path still supplies read-back
                return None
            if not isinstance(membership, Mapping):
                return {
                    "user_concept_id": member_id,
                    "organisation_concept_id": organisation_id,
                    "membership_present": False,
                    "role": None,
                }
            return {
                "user_concept_id": member_id,
                "organisation_concept_id": organisation_id,
                "membership_present": True,
                # Confirmation needs only the exact membership postcondition;
                # do not widen an actor-scoped Q&A receipt with another user's
                # organisation role.
                "role_projected": False,
            }

        membership_read_back = _membership_read_back()
        if (
            isinstance(membership_read_back, Mapping)
            and membership_read_back.get("membership_present") is True
        ):
            governance = {
                "success": True,
                "effect_status": "succeeded",
                "changed": False,
                "idempotent_replay": True,
                "postcondition_preexisting": True,
                "canonical_read_back": dict(membership_read_back),
                "authority_check_required": False,
                "reason_code": "membership_already_present",
            }
        else:
            governance = manage_organisation_membership(
                action="add",
                user_concept_id=member_id,
                organisation_concept_id=organisation_id,
                role="member",
                request_id=request_id,
                reason=(
                    "Confirm actor-scoped tentative concept Q&A membership "
                    f"formalisation {assertion_id}"
                ),
                acting_actor_concept_id=user_id,
            )
        if governance.get("success") is not True:
            # An authorised administrator may have completed the canonical
            # membership through its own actor-scoped management path after
            # this actor's first attempt. Reconcile that postcondition before
            # returning another authority denial.
            reconciled_membership = _membership_read_back()
            if (
                isinstance(reconciled_membership, Mapping)
                and reconciled_membership.get("membership_present") is True
            ):
                governance = {
                    "success": True,
                    "effect_status": "succeeded",
                    "changed": False,
                    "idempotent_replay": True,
                    "postcondition_preexisting": True,
                    "canonical_read_back": dict(reconciled_membership),
                    "authority_check_required": False,
                    "reason_code": "membership_present_after_reconciliation",
                }
            else:
                authority = governance.get("authority_decision")
                required_permissions = (
                    authority.get("required_permissions")
                    if isinstance(authority, Mapping)
                    else ["MANAGE_MEMBERS"]
                )
                return {
                    "success": False,
                    "status": "tentative",
                    "effect_status": governance.get("effect_status") or "not_started",
                    "error_code": governance.get("error_code")
                    or "organisation_membership_confirmation_denied",
                    "authority_decision": authority,
                    "governance_receipt": governance.get("governance_receipt"),
                    "canonical_read_back": governance.get("canonical_read_back"),
                    "actionable_denial": {
                        "who_can_add_membership": (
                            "an organisation admin or owner with the represented "
                            "MANAGE_MEMBERS permission"
                        ),
                        "required_permissions": required_permissions,
                        "mechanism": "canonical_organisation_membership_handoff",
                        "membership_management_path": (
                            "canonical organisation membership management"
                        ),
                        "same_q_and_a_action_available_to_other_actor": False,
                        "original_actor_next_step": (
                            "After an authorised actor adds the membership, the "
                            "original Q&A actor can retry this Confirm action; it "
                            "will reconcile the membership and promote the "
                            "source-linked tentative assertion."
                        ),
                        "tentative_assertion_preserved": True,
                    },
                }
        if already_asserted:
            return {
                "success": True,
                "status": "asserted",
                "effect_status": "succeeded",
                "changed": bool(governance.get("changed")),
                "idempotent_replay": not bool(governance.get("changed")),
                "canonical_read_back": dict(assertion),
                "governed_activation": governance,
            }
    try:
        promoted = scoped_assertion_service.promote_scoped_assertion_epistemic_status(
            assertion_id=assertion_id,
            acting_user_concept_id=user_id,
            organisation_concept_id=organisation_concept_id,
            namespace=namespace,
            request_id=request_id,
        )
    except PermissionError:
        response = {
            "success": False,
            "status": "tentative",
            "effect_status": "not_started",
            "error_code": "formalisation_confirmation_actor_required",
            "actionable_denial": {
                "who_can_confirm": "the original asserting actor",
                "tentative_assertion_preserved": True,
            },
        }
        if governance is not None and governance.get("success") is True:
            response.update(
                {
                    "effect_status": "partial_success",
                    "error_code": "governed_activation_succeeded_assertion_promotion_pending",
                    "governed_activation": governance,
                    "canonical_read_back": governance.get("canonical_read_back"),
                    "reconciliation_required": True,
                }
            )
        return response
    except Exception:  # noqa: BLE001 - preserve partial governed-effect receipt
        response = {
            "success": False,
            "status": "tentative",
            "effect_status": "failed",
            "error_code": "formalisation_confirmation_failed",
            "actionable_denial": {"tentative_assertion_preserved": True},
        }
        if governance is not None and governance.get("success") is True:
            response.update(
                {
                    "effect_status": "partial_success",
                    "error_code": "governed_activation_succeeded_assertion_promotion_pending",
                    "governed_activation": governance,
                    "canonical_read_back": governance.get("canonical_read_back"),
                    "reconciliation_required": True,
                }
            )
        return response
    return {
        "success": True,
        "status": "asserted",
        "effect_status": "succeeded",
        "changed": promoted.get("changed"),
        "assertion_id": assertion_id,
        "canonical_read_back": promoted.get("canonical_read_back"),
        "governed_activation": governance,
    }


def _confirmation_failure_message(confirmation: Mapping[str, Any]) -> str:
    denial = confirmation.get("actionable_denial")
    if isinstance(denial, Mapping) and denial.get("mechanism") == (
        "canonical_organisation_membership_handoff"
    ):
        return (
            "I could not add this membership with your current authority. The "
            "tentative assertion is preserved. An authorised organisation admin "
            "or owner can add the membership through canonical organisation "
            "membership management; afterwards, return as the original Q&A actor "
            "and retry Confirm."
        )
    return (
        "I could not confirm that formalisation with the current actor's "
        "authority. The tentative assertion is preserved in this actor-scoped "
        "Q&A receipt."
    )


def _confirmation_question(
    *, formalisation: Mapping[str, Any], answer_text: str
) -> str:
    template = str(formalisation.get("confirmation_question_template") or "").strip()
    if template:
        try:
            rendered = template.format(
                counterpart_name=answer_text,
                instance_name=str(
                    formalisation.get("focal_concept_name") or "this concept"
                ),
            ).strip()
        except (KeyError, ValueError):
            rendered = ""
        if rendered:
            return rendered
    return f"I recorded a tentative relation for {answer_text}. Should I confirm it?"


def _notes_receipt_from_downstream(result: Mapping[str, Any]) -> dict[str, Any]:
    representation = result.get("representation")
    notes = (
        representation.get("concept_notes")
        if isinstance(representation, Mapping)
        else None
    )
    source = dict(notes) if isinstance(notes, Mapping) else {}
    raw_status = str(source.get("status") or "not_attempted")
    if source.get("effect_status") == "failed" or raw_status in {
        "error",
        "persistence_failed",
    }:
        status = "failed"
    elif source.get("notes_updated") is True or raw_status == "updated":
        status = "updated"
    else:
        status = "not_updated"
    return {
        "schema_version": NOTES_RECEIPT_SCHEMA_VERSION,
        "kind": "notes",
        "status": status,
        "effect_status": source.get("effect_status") or "not_needed",
        "notes_updated": source.get("notes_updated") is True,
        "source_assertion_id": source.get("source_assertion_id"),
        "reason_code": source.get("reason_code") or source.get("reason"),
        "error_code": source.get("error_code"),
    }


def _append_turn_once(
    *,
    user_id: str,
    namespace: str,
    session_id: str,
    message: dict[str, Any],
    require_active: bool,
) -> dict[str, Any]:
    expected = {"concept_q_and_a.lifecycle.status": ACTIVE} if require_active else None
    result = chat_history_service.append_message_to_history_once(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        message=message,
        expected_session_fields=expected,
    )
    if result.get("matched"):
        return result
    doc = _find_session(user_id=user_id, namespace=namespace, session_id=session_id)
    if doc is None:
        raise ConceptQAConversationNotFound(
            "Q&A conversation was not found", error_code="conversation_not_found"
        )
    status = _session_lifecycle(doc).get("status")
    if require_active and status in TERMINAL_STATUSES:
        raise ConceptQAConversationConflict(
            f"Q&A conversation is {status}", error_code="conversation_terminal"
        )
    raise ConceptQAConversationConflict(
        "Q&A turn could not be appended",
        error_code="conversation_turn_compare_and_set_failed",
    )


def _ensure_initial_assistant_turn(
    *,
    user_id: str,
    namespace: str,
    session_id: str,
    concept: Mapping[str, Any],
    session_doc: Mapping[str, Any],
) -> None:
    history = (
        session_doc.get("history")
        if isinstance(session_doc.get("history"), list)
        else []
    )
    if any(
        isinstance(item, Mapping) and item.get("role") == "assistant"
        for item in history
    ):
        return
    qa_state = session_doc.get("concept_q_and_a")
    initial_notes = (
        qa_state.get("initial_notes") if isinstance(qa_state, Mapping) else None
    )
    runtime_session = {
        "user_id": user_id,
        "organisation_concept_id": session_doc.get("organisation_concept_id"),
        "namespace": namespace,
        "llm_selection": qa_state.get("llm_selection")
        if isinstance(qa_state, Mapping)
        else None,
    }
    result = concept_service.generate_initial_question(
        str(concept.get("concept_id")),
        initial_notes=str(initial_notes or ""),
        interaction_session=runtime_session,
        persist_interaction_question=False,
    )
    if not isinstance(result, Mapping) or result.get("status") != "success":
        raise ConceptQAConversationError(
            "Could not generate the initial Q&A turn",
            error_code=str(
                (result or {}).get("status")
                if isinstance(result, Mapping)
                else "initial_question_failed"
            ),
        )
    question = str(result.get("question") or "").strip()
    if not question:
        raise ConceptQAConversationError(
            "Initial Q&A turn was empty", error_code="initial_question_empty"
        )
    emitted_predicate = result.get("elicitation_predicate")
    elicitation_predicate = (
        concept_service._project_elicitation_metadata(emitted_predicate)
        if isinstance(emitted_predicate, Mapping)
        else None
    )
    assistant_turn_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"von:concept-qa:{session_id}:initial-assistant")
    )
    metadata: dict[str, Any] = {"kind": "initial_question"}
    if elicitation_predicate:
        metadata["elicitation_predicate"] = elicitation_predicate
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": question,
        "turn_id": assistant_turn_id,
        "concept_q_and_a": metadata,
    }
    llm_debug_data = _normalise_turn_llm_debug_data(result.get("llm_debug_data"))
    if llm_debug_data is not None:
        assistant_message["llm_debug_data"] = llm_debug_data
    _append_turn_once(
        user_id=user_id,
        namespace=namespace,
        session_id=session_id,
        message=assistant_message,
        require_active=True,
    )


def start_concept_qa_conversation(
    *,
    concept_id: str,
    user_id: str,
    namespace: str | None = None,
    organisation_concept_id: str | None = None,
    initial_notes: str | None = None,
    initial_turn_id: str | None = None,
    model_provider: str | None = None,
    model: str | None = None,
    model_parameters: Any = None,
) -> dict[str, Any]:
    """Start or resume the one active actor/concept Q&A conversation."""

    actor_id = _normalise_actor_id(user_id, field="user_id")
    org_id = _normalise_org_id(organisation_concept_id)
    resolved_namespace = _resolve_namespace(
        user_id=actor_id,
        namespace=namespace,
        organisation_concept_id=org_id,
    )
    concept = _load_focal_concept(
        concept_id,
        user_id=actor_id,
        organisation_concept_id=org_id,
    )
    canonical_concept_id = str(concept["concept_id"])
    collection = _collection()
    active_query = _actor_query(user_id=actor_id, namespace=resolved_namespace)
    active_query.update(
        {
            "focal_concept_ids": canonical_concept_id,
            "concept_q_and_a.lifecycle.status": ACTIVE,
        }
    )
    existing = collection.find_one(active_query, sort=[("updated_at", DESCENDING)])
    created = False
    initial_receipt_turn_id: str | None = None
    if existing is None:
        session_id = str(uuid.uuid4())
        display_name = get_concept_display_name_with_names_fallback(concept)
        llm_selection = concept_service._normalise_interaction_llm_selection(
            provider=model_provider,
            model=model,
            model_parameters=model_parameters,
        )
        now = _now()
        qa_state: dict[str, Any] = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "concept": {
                "concept_id": canonical_concept_id,
                "storage_id": str(concept.get("_id") or ""),
                "name": display_name,
            },
            "active_key": _active_key(
                user_id=actor_id,
                namespace=resolved_namespace,
                concept_id=canonical_concept_id,
            ),
            "lifecycle": {
                "status": ACTIVE,
                "revision": 0,
                "started_at": now,
                "terminal_at": None,
            },
            "turn_receipts": {},
            "initial_notes": str(initial_notes or "").strip() or None,
        }
        if llm_selection:
            qa_state["llm_selection"] = llm_selection
        try:
            chat_history_service.create_chat_session(
                user_id=actor_id,
                session_id=session_id,
                session_name=f"Concept Q&A: {display_name or canonical_concept_id}",
                namespace=resolved_namespace,
                organisation_concept_id=org_id,
                mode=MODE,
                origin_kind=ORIGIN_KIND,
                created_by_actor_concept_id=actor_id,
                focal_concept_ids=[canonical_concept_id],
                concept_q_and_a_state=qa_state,
            )
            created = True
            existing = _find_session(
                user_id=actor_id,
                namespace=resolved_namespace,
                session_id=session_id,
            )
        except DuplicateKeyError:
            # The partial unique active-key index is the concurrency boundary.
            # Return the winner instead of creating a second active carrier.
            created = False
            existing = collection.find_one(
                active_query, sort=[("updated_at", DESCENDING)]
            )
        if existing is None:
            raise ConceptQAConversationError(
                "Could not read back the Q&A carrier",
                error_code="conversation_readback_failed",
            )
        notes_text = str(initial_notes or "").strip()
        if created and notes_text:
            start_turn_id = (
                _required_text(initial_turn_id, field="initial_turn_id")
                if initial_turn_id
                else str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"von:concept-qa:{session_id}:initial-notes",
                    )
                )
            )
            _append_turn_once(
                user_id=actor_id,
                namespace=resolved_namespace,
                session_id=session_id,
                message={
                    "role": "user",
                    "content": notes_text,
                    "turn_id": start_turn_id,
                    "concept_q_and_a": {
                        "kind": "initial_notes",
                        "input_kind": "asserted_knowledge",
                    },
                },
                require_active=True,
            )
            existing = (
                _find_session(
                    user_id=actor_id,
                    namespace=resolved_namespace,
                    session_id=session_id,
                )
                or existing
            )
            exact_input = _aggregate_exact_input_receipt(
                session_id=session_id,
                turn_id=start_turn_id,
                answer="",
                notes_input=notes_text,
                concept=concept,
                session_doc=existing,
            )
            _, predicate = _previous_assistant_context(existing)
            receipt = {
                "schema_version": TURN_RECEIPT_SCHEMA_VERSION,
                "turn_id": start_turn_id,
                "exact_input": exact_input,
                "formalisation": _deferred_formalisation_receipt(
                    exact_input=exact_input,
                    proposed_predicate=predicate,
                    reason_code="initial_notes_are_not_a_short_elicited_entity_answer",
                ),
                "notes": {
                    **_initial_notes_receipt(),
                    "reason_code": "initial_notes_are_prompt_context_only",
                },
            }
            _record_turn_receipt(
                user_id=actor_id,
                namespace=resolved_namespace,
                session_id=session_id,
                turn_id=start_turn_id,
                receipt=receipt,
            )
            initial_receipt_turn_id = start_turn_id
    session_id = str(existing.get("session_id"))
    refreshed = _find_session(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=session_id,
    )
    if refreshed is None:
        raise ConceptQAConversationError(
            "Could not read back the Q&A carrier",
            error_code="conversation_readback_failed",
        )
    _ensure_initial_assistant_turn(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=session_id,
        concept=concept,
        session_doc=refreshed,
    )
    readback = _find_session(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=session_id,
    )
    if readback is None:
        raise ConceptQAConversationError(
            "Could not read back the initial Q&A turn",
            error_code="conversation_readback_failed",
        )
    if initial_receipt_turn_id:
        qa_state = readback.get("concept_q_and_a")
        receipts = (
            qa_state.get("turn_receipts") if isinstance(qa_state, Mapping) else None
        )
        initial_receipt = (
            receipts.get(initial_receipt_turn_id)
            if isinstance(receipts, Mapping)
            else None
        )
        if isinstance(initial_receipt, Mapping) and not initial_receipt.get(
            "completed_at"
        ):
            completed_initial_receipt = dict(initial_receipt)
            completed_initial_receipt["completed_at"] = _now()
            completed_initial_receipt["assistant_turn_id"] = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"von:concept-qa:{session_id}:initial-assistant",
                )
            )
            _record_turn_receipt(
                user_id=actor_id,
                namespace=resolved_namespace,
                session_id=session_id,
                turn_id=initial_receipt_turn_id,
                receipt=completed_initial_receipt,
            )
            readback = (
                _find_session(
                    user_id=actor_id,
                    namespace=resolved_namespace,
                    session_id=session_id,
                )
                or readback
            )
    return {**_project_session(readback), "created": created, "resumed": not created}


def list_concept_qa_conversations(
    *,
    concept_id: str,
    user_id: str,
    namespace: str | None = None,
    organisation_concept_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    actor_id = _normalise_actor_id(user_id, field="user_id")
    org_id = _normalise_org_id(organisation_concept_id)
    resolved_namespace = _resolve_namespace(
        user_id=actor_id,
        namespace=namespace,
        organisation_concept_id=org_id,
    )
    concept = _load_focal_concept(
        concept_id,
        user_id=actor_id,
        organisation_concept_id=org_id,
    )
    canonical_concept_id = str(concept["concept_id"])
    query = _actor_query(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=session_id,
    )
    query["focal_concept_ids"] = canonical_concept_id
    try:
        docs = list(
            _collection(read_only=True)
            .find(query)
            .sort([("updated_at", DESCENDING), ("created_at", DESCENDING)])
            .limit(_MAX_SESSIONS)
        )
    except PyMongoError as exc:
        raise ConceptQAConversationError(
            "Could not list Q&A conversations",
            error_code="conversation_list_failed",
        ) from exc
    sessions = [_project_session(doc) for doc in docs]
    active_session = next(
        (
            item
            for item in sessions
            if item.get("lifecycle", {}).get("status") == ACTIVE
        ),
        None,
    )
    legacy_active_interactions: list[dict[str, Any]] = []
    try:
        storage_id = str(concept.get("_id") or "")
        if storage_id:
            legacy_active_interactions = (
                concept_service.get_active_interactions_for_concept(
                    storage_id,
                    actor_id,
                    organisation_concept_id=org_id,
                )[:_MAX_SESSIONS]
            )
    except Exception:  # noqa: BLE001 - legacy compatibility cannot block canonical listing
        legacy_active_interactions = []
    return {
        "schema_version": "concept_q_and_a_collection.v1",
        "concept_id": canonical_concept_id,
        "sessions": sessions,
        "active_session": active_session,
        "legacy_active_interactions": _iso(legacy_active_interactions),
        # Compatibility alias. The adjacent scope field makes it explicit that
        # this legacy store projection contains active rows only.
        "legacy_interactions": _iso(legacy_active_interactions),
        "legacy_interaction_scope": "active_only",
        "count": len(sessions),
    }


def submit_concept_qa_turn(
    *,
    concept_id: str,
    session_id: str,
    turn_id: str,
    user_id: str,
    answer: str,
    notes_input: str | None = None,
    namespace: str | None = None,
    organisation_concept_id: str | None = None,
) -> dict[str, Any]:
    actor_id = _normalise_actor_id(user_id, field="user_id")
    resolved_session_id = _required_text(session_id, field="session_id")
    resolved_turn_id = _required_text(turn_id, field="turn_id")
    answer_text = answer.strip() if isinstance(answer, str) else ""
    notes_text = notes_input.strip() if isinstance(notes_input, str) else ""
    if not answer_text and not notes_text:
        raise InvalidConceptQAConversationInput(
            "answer or notes_input must contain text",
            error_code="concept_q_and_a_input_required",
        )
    org_id = _normalise_org_id(organisation_concept_id)
    resolved_namespace = _resolve_namespace(
        user_id=actor_id,
        namespace=namespace,
        organisation_concept_id=org_id,
    )
    concept = _load_focal_concept(
        concept_id,
        user_id=actor_id,
        organisation_concept_id=org_id,
    )
    canonical_concept_id = str(concept["concept_id"])
    doc = _find_session(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=resolved_session_id,
    )
    if doc is None or canonical_concept_id not in list(
        doc.get("focal_concept_ids") or []
    ):
        raise ConceptQAConversationNotFound(
            "Q&A conversation was not found for this concept",
            error_code="conversation_not_found",
        )
    lifecycle = _session_lifecycle(doc)
    if lifecycle.get("status") != ACTIVE:
        raise ConceptQAConversationConflict(
            f"Q&A conversation is {lifecycle.get('status') or 'not active'}",
            error_code="conversation_terminal",
        )
    effective_org_id = _normalise_org_id(doc.get("organisation_concept_id")) or org_id
    prior_doc = doc
    pending_confirmation = _pending_confirmation(prior_doc)
    representation_status_query = bool(
        answer_text and not notes_text and _is_representation_status_query(answer_text)
    )
    confirmation_reply = (
        _confirmation_reply_kind(answer_text)
        if pending_confirmation is not None and answer_text and not notes_text
        else None
    )
    transcript_only_reason = (
        "confirmation_reply_is_control_evidence"
        if confirmation_reply is not None
        else "representation_status_query_is_control_evidence"
        if representation_status_query
        else None
    )

    transcript_entries: list[dict[str, Any]] = []
    if answer_text:
        classification = (
            {"kind": "meta_or_control"}
            if transcript_only_reason
            else classify_user_input(answer_text)
        )
        transcript_entries.append(
            {
                "role": "user",
                "content": answer_text,
                "turn_id": resolved_turn_id,
                "concept_q_and_a": {
                    "kind": "user_input",
                    "input_kind": classification["kind"],
                    "confirmation_reply": confirmation_reply,
                },
            }
        )
    if notes_text:
        notes_turn_id = (
            resolved_turn_id
            if not answer_text
            else str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"von:concept-qa:{resolved_session_id}:{resolved_turn_id}:notes",
                )
            )
        )
        transcript_entries.append(
            {
                "role": "user",
                "content": notes_text,
                "turn_id": notes_turn_id,
                "in_reply_to_turn_id": resolved_turn_id if answer_text else None,
                "concept_q_and_a": {
                    "kind": "user_input",
                    "input_kind": "notes",
                    "source_request_turn_id": resolved_turn_id,
                },
            }
        )

    primary_duplicate = False
    for entry in transcript_entries:
        append_result = _append_turn_once(
            user_id=actor_id,
            namespace=resolved_namespace,
            session_id=resolved_session_id,
            message=entry,
            require_active=True,
        )
        existing_message = append_result.get("message")
        if append_result.get("duplicate") and isinstance(existing_message, Mapping):
            if existing_message.get("content") != entry["content"]:
                raise ConceptQAConversationConflict(
                    "turn_id was already used for different content",
                    error_code="turn_id_content_conflict",
                )
            if entry["turn_id"] == resolved_turn_id:
                primary_duplicate = True
    if primary_duplicate:
        replay_doc = _find_session(
            user_id=actor_id,
            namespace=resolved_namespace,
            session_id=resolved_session_id,
        )
        qa_state = (
            replay_doc.get("concept_q_and_a")
            if isinstance(replay_doc, Mapping)
            else None
        )
        receipts = (
            qa_state.get("turn_receipts") if isinstance(qa_state, Mapping) else None
        )
        existing_receipt = (
            receipts.get(resolved_turn_id) if isinstance(receipts, Mapping) else None
        )
        if isinstance(existing_receipt, Mapping) and existing_receipt.get(
            "completed_at"
        ):
            return {
                **_project_session(replay_doc),
                "receipts": _iso(dict(existing_receipt)),
                "turn_id": resolved_turn_id,
                "replayed": True,
            }

    doc = (
        _find_session(
            user_id=actor_id,
            namespace=resolved_namespace,
            session_id=resolved_session_id,
        )
        or prior_doc
    )
    exact_input = _aggregate_exact_input_receipt(
        session_id=resolved_session_id,
        turn_id=resolved_turn_id,
        answer=answer_text,
        notes_input=notes_text,
        concept=concept,
        session_doc=prior_doc,
        transcript_only_reason=transcript_only_reason,
    )
    _, proposed_predicate = _previous_assistant_context(prior_doc)
    if pending_confirmation is not None and confirmation_reply is not None:
        formalisation: dict[str, Any] = {
            "schema_version": FORMALISATION_RECEIPT_SCHEMA_VERSION,
            "kind": "formalisation_confirmation_reply",
            "status": "processing",
            "effect_status": "not_started",
            "assertion_id": pending_confirmation.get("assertion_id"),
            "reply_kind": confirmation_reply,
        }
    else:
        formalisation = _tentative_formalisation_receipt(
            exact_input=exact_input,
            answer=answer_text,
            proposed_predicate=proposed_predicate,
            concept_id=canonical_concept_id,
            concept_name=(
                get_concept_display_name_with_names_fallback(concept)
                or canonical_concept_id
            ),
            session_id=resolved_session_id,
            turn_id=resolved_turn_id,
            user_id=actor_id,
            organisation_concept_id=effective_org_id,
            namespace=resolved_namespace,
        )
    receipt: dict[str, Any] = {
        "schema_version": TURN_RECEIPT_SCHEMA_VERSION,
        "turn_id": resolved_turn_id,
        "accepted_at": _now(),
        "transcript_turn_ids": [entry["turn_id"] for entry in transcript_entries],
        "exact_input": exact_input,
        "formalisation": formalisation,
        "notes": _initial_notes_receipt(),
    }
    _record_turn_receipt(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=resolved_session_id,
        turn_id=resolved_turn_id,
        receipt=receipt,
    )

    assistant_metadata: dict[str, Any]
    downstream: Mapping[str, Any] | None = None
    status = "success"
    if representation_status_query:
        representation_status = _representation_status_from_receipts(prior_doc)
        receipt["formalisation"] = {
            "schema_version": FORMALISATION_RECEIPT_SCHEMA_VERSION,
            "kind": "formalisation",
            "status": "not_applicable",
            "effect_status": "not_needed",
            "canonical_publication": False,
            "reason_code": "representation_status_query_is_control_evidence",
        }
        receipt["notes"] = {
            **_initial_notes_receipt(),
            "reason_code": "representation_status_query_does_not_update_notes",
        }
        receipt["representation_status"] = representation_status
        assistant_content = str(representation_status["message"])
        assistant_metadata = {
            "kind": "representation_status_response",
            "representation_status": representation_status,
        }
    elif pending_confirmation is not None and confirmation_reply is not None:
        assertion_id = str(pending_confirmation.get("assertion_id") or "")
        source_turn_id, original_formalisation = _locate_formalisation_receipt(
            doc, assertion_id
        )
        if not source_turn_id or not isinstance(original_formalisation, Mapping):
            confirmation = {
                "success": False,
                "status": "tentative",
                "effect_status": "not_started",
                "error_code": "formalisation_confirmation_source_not_found",
            }
        elif confirmation_reply == "affirm":
            confirmation = _confirm_formalisation_effect(
                formalisation=original_formalisation,
                assertion_id=assertion_id,
                user_id=actor_id,
                organisation_concept_id=effective_org_id,
                namespace=resolved_namespace,
                request_id=(
                    f"concept_q_and_a:{resolved_session_id}:{assertion_id}:confirm"
                ),
                expected_focal_concept_id=canonical_concept_id,
            )
            if confirmation.get("success") is True:
                confirmation = {
                    **confirmation,
                    "conversation_progress": _record_confirmed_requirement_disposition(
                        user_id=actor_id,
                        namespace=resolved_namespace,
                        session_id=resolved_session_id,
                        formalisation=original_formalisation,
                    ),
                }
            updated_original = dict(original_formalisation)
            updated_original["status"] = confirmation.get("status", "tentative")
            updated_original["epistemic_status"] = confirmation.get(
                "status", "tentative"
            )
            updated_original["effect_status"] = confirmation.get("effect_status")
            if "canonical_read_back" in confirmation:
                updated_original["canonical_read_back"] = confirmation.get(
                    "canonical_read_back"
                )
            updated_original["confirmation"] = {
                **dict(updated_original.get("confirmation") or {}),
                "available": confirmation.get("success") is not True,
                "last_attempt": confirmation,
            }
            _record_formalisation_receipt(
                user_id=actor_id,
                namespace=resolved_namespace,
                session_id=resolved_session_id,
                source_turn_id=source_turn_id,
                formalisation=updated_original,
            )
        elif confirmation_reply == "reject":
            try:
                rejected = scoped_assertion_service.retract_scoped_assertion(
                    assertion_id=assertion_id,
                    acting_user_concept_id=actor_id,
                    organisation_concept_id=effective_org_id,
                    namespace=resolved_namespace,
                )
                confirmation = {
                    "success": True,
                    "status": "rejected",
                    "effect_status": "succeeded",
                    "changed": rejected.get("changed"),
                    "canonical_read_back": rejected.get("canonical_read_back"),
                }
                updated_original = {
                    **dict(original_formalisation),
                    "status": "rejected",
                    "epistemic_status": "rejected",
                    "effect_status": confirmation.get("effect_status"),
                    "canonical_read_back": confirmation.get("canonical_read_back"),
                    "confirmation": {
                        **dict(original_formalisation.get("confirmation") or {}),
                        "available": False,
                        "last_attempt": confirmation,
                    },
                }
                _record_formalisation_receipt(
                    user_id=actor_id,
                    namespace=resolved_namespace,
                    session_id=resolved_session_id,
                    source_turn_id=source_turn_id,
                    formalisation=updated_original,
                )
            except Exception:  # noqa: BLE001 - rejection failure is a typed receipt
                confirmation = {
                    "success": False,
                    "status": "tentative",
                    "effect_status": "failed",
                    "error_code": "tentative_formalisation_rejection_failed",
                }
        else:
            confirmation = {
                "success": True,
                "status": "tentative",
                "effect_status": "not_needed",
                "changed": False,
                "deferred": True,
            }
            if source_turn_id and isinstance(original_formalisation, Mapping):
                updated_original = {
                    **dict(original_formalisation),
                    "confirmation": {
                        **dict(original_formalisation.get("confirmation") or {}),
                        "available": True,
                        "last_attempt": confirmation,
                    },
                }
                _record_formalisation_receipt(
                    user_id=actor_id,
                    namespace=resolved_namespace,
                    session_id=resolved_session_id,
                    source_turn_id=source_turn_id,
                    formalisation=updated_original,
                )
        next_predicate = None
        if (
            source_turn_id
            and isinstance(original_formalisation, Mapping)
            and confirmation_reply in {"affirm", "defer", "reject"}
            and confirmation.get("success") is True
        ):
            if confirmation_reply in {"defer", "reject"}:
                _suppress_confirmation_requirement(
                    user_id=actor_id,
                    namespace=resolved_namespace,
                    session_id=resolved_session_id,
                    formalisation=original_formalisation,
                    disposition=confirmation_reply,
                )
            progress_doc = (
                _find_session(
                    user_id=actor_id,
                    namespace=resolved_namespace,
                    session_id=resolved_session_id,
                )
                or doc
            )
            next_predicate = _next_unsuppressed_elicitation_predicate(
                concept_id=canonical_concept_id,
                session_doc=progress_doc,
                exclude_requirement=(
                    original_formalisation if confirmation_reply == "affirm" else None
                ),
            )
        receipt["formalisation"] = {
            **formalisation,
            "status": confirmation.get("status"),
            "effect_status": confirmation.get("effect_status"),
            "confirmation": confirmation,
        }
        receipt["notes"] = {
            **_initial_notes_receipt(),
            "reason_code": "confirmation_control_turn_does_not_update_notes",
        }
        if confirmation.get("success") is True and confirmation_reply == "affirm":
            assistant_content = (
                "Confirmed. The formalisation has been read back as asserted."
            )
        elif confirmation_reply == "reject" and confirmation.get("success") is True:
            assistant_content = "Understood. I rejected the tentative formalisation."
        elif confirmation_reply == "defer":
            assistant_content = "Understood. I left the formalisation tentative."
        else:
            assistant_content = _confirmation_failure_message(confirmation)
            status = "partial_success"
        if (
            confirmation_reply in {"affirm", "defer", "reject"}
            and confirmation.get("success") is True
        ):
            next_question = str((next_predicate or {}).get("question") or "").strip()
            assistant_content = (
                f"{assistant_content} {next_question}"
                if next_question
                else (
                    f"{assistant_content} What other useful information should be "
                    "recorded about this concept?"
                )
            )
        assistant_metadata = {
            "kind": "formalisation_confirmation_result",
            "formalisation": receipt["formalisation"],
        }
        if next_predicate:
            assistant_metadata["elicitation_predicate"] = next_predicate
    else:
        qa_state = prior_doc.get("concept_q_and_a")
        transient_session = {
            "concept_id": str(concept.get("_id") or ""),
            "user_id": actor_id,
            "organisation_concept_id": effective_org_id,
            "namespace": resolved_namespace,
            "history": _legacy_history_from_chat(prior_doc),
            "llm_selection": qa_state.get("llm_selection")
            if isinstance(qa_state, Mapping)
            else None,
            "suppressed_requirement_keys": list(
                qa_state.get("suppressed_requirement_keys") or []
            )
            if isinstance(qa_state, Mapping)
            else [],
            "suppressed_predicate_ids": list(
                qa_state.get("suppressed_predicate_ids") or []
            )
            if isinstance(qa_state, Mapping)
            else [],
            "addressed_requirement_keys": list(
                qa_state.get("addressed_requirement_keys") or []
            )
            if isinstance(qa_state, Mapping)
            else [],
            "addressed_predicate_ids": list(
                qa_state.get("addressed_predicate_ids") or []
            )
            if isinstance(qa_state, Mapping)
            else [],
        }
        downstream = concept_service.submit_concept_answer(
            interaction_id=resolved_session_id,
            user_answer=answer_text,
            user_notes_input=notes_text,
            session=transient_session,
            representation_overrides={
                "exact_answer": _representation_item(exact_input, "answer"),
                "notes_input": _representation_item(exact_input, "notes"),
            },
            persist_interaction_session=False,
            allow_canonical_notes_update=False,
        )
        if not isinstance(downstream, Mapping) or downstream.get("status") != "success":
            receipt["notes"] = {
                "schema_version": NOTES_RECEIPT_SCHEMA_VERSION,
                "kind": "notes",
                "status": "failed",
                "effect_status": "failed",
                "error_code": str(
                    downstream.get("status")
                    if isinstance(downstream, Mapping)
                    else "downstream_processing_failed"
                ),
            }
            assistant_content = (
                "I preserved your exact input and its representation receipt, but I "
                "could not complete the notes update or next question. You can retry this turn."
            )
            assistant_metadata = {"kind": "downstream_processing_failed"}
            status = "partial_success"
        else:
            receipt["notes"] = _notes_receipt_from_downstream(downstream)
            assistant_content = str(downstream.get("next_step_content") or "").strip()
            assistant_metadata = {
                "kind": downstream.get("next_step_type") or "assistant_response",
            }
            if formalisation.get("status") == "tentative":
                assistant_content = _confirmation_question(
                    formalisation=formalisation,
                    answer_text=answer_text,
                )
                assistant_metadata = {
                    "kind": "formalisation_confirmation_request",
                    "formalisation": {
                        key: formalisation.get(key)
                        for key in (
                            "assertion_id",
                            "subject_concept_id",
                            "predicate_concept_id",
                            "object_concept_id",
                            "requirement_id",
                            "focal_argument",
                            "other_argument_type_concept_id",
                            "declared_on_type_concept_id",
                            "inherited",
                            "declaration_depth",
                            "declaration_source",
                            "profile_predicate",
                            "confirmation_question_template",
                            "elicitation_requirement",
                            "confirmation",
                        )
                    },
                }
            else:
                emitted_predicate = downstream.get("elicitation_predicate")
                next_predicate = (
                    concept_service._project_elicitation_metadata(emitted_predicate)
                    if isinstance(emitted_predicate, Mapping)
                    else None
                )
                if next_predicate:
                    assistant_metadata["elicitation_predicate"] = next_predicate
        if not assistant_content:
            assistant_content = "Your input was preserved. What else should I know?"

    assistant_turn_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"von:concept-qa:{resolved_session_id}:{resolved_turn_id}:assistant",
        )
    )
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": assistant_content,
        "turn_id": assistant_turn_id,
        "in_reply_to_turn_id": resolved_turn_id,
        "concept_q_and_a": assistant_metadata,
    }
    llm_debug_data = _normalise_turn_llm_debug_data(
        downstream.get("llm_debug_data") if isinstance(downstream, Mapping) else None
    )
    if llm_debug_data is not None:
        assistant_message["llm_debug_data"] = llm_debug_data
    _append_turn_once(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=resolved_session_id,
        message=assistant_message,
        require_active=False,
    )
    assistant_readback = _find_session(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=resolved_session_id,
    )
    if not isinstance(assistant_readback, Mapping) or not any(
        isinstance(entry, Mapping) and entry.get("turn_id") == assistant_turn_id
        for entry in assistant_readback.get("history") or []
    ):
        raise ConceptQAConversationError(
            "Could not read back the assistant Q&A turn",
            error_code="assistant_turn_readback_failed",
        )
    receipt["assistant_turn_id"] = assistant_turn_id
    receipt["completed_at"] = _now()
    _record_turn_receipt(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=resolved_session_id,
        turn_id=resolved_turn_id,
        receipt=receipt,
    )
    readback = _find_session(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=resolved_session_id,
    )
    if readback is None:
        raise ConceptQAConversationError(
            "Could not read back the completed Q&A turn",
            error_code="conversation_readback_failed",
        )
    return {
        **_project_session(readback),
        "status": status,
        "turn_id": resolved_turn_id,
        "receipts": _iso(receipt),
        "downstream_error": (
            _iso(dict(downstream))
            if status == "partial_success" and isinstance(downstream, Mapping)
            else None
        ),
        "replayed": False,
    }


def confirm_concept_qa_formalisation(
    *,
    concept_id: str,
    session_id: str,
    assertion_id: str,
    user_id: str,
    namespace: str | None = None,
    organisation_concept_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Confirm one source-linked tentative relation and return canonical read-back."""

    actor_id = _normalise_actor_id(user_id, field="user_id")
    resolved_session_id = _required_text(session_id, field="session_id")
    resolved_assertion_id = _required_text(assertion_id, field="assertion_id")
    if not resolved_assertion_id.startswith("ska_"):
        raise InvalidConceptQAConversationInput(
            "assertion_id must identify a scoped assertion",
            error_code="invalid_formalisation_assertion_id",
        )
    org_id = _normalise_org_id(organisation_concept_id)
    resolved_namespace = _resolve_namespace(
        user_id=actor_id,
        namespace=namespace,
        organisation_concept_id=org_id,
    )
    concept = _load_focal_concept(
        concept_id,
        user_id=actor_id,
        organisation_concept_id=org_id,
    )
    canonical_concept_id = str(concept["concept_id"])
    doc = _find_session(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=resolved_session_id,
    )
    if doc is None or canonical_concept_id not in list(
        doc.get("focal_concept_ids") or []
    ):
        raise ConceptQAConversationNotFound(
            "Q&A conversation was not found for this concept",
            error_code="conversation_not_found",
        )
    lifecycle_status = _session_lifecycle(doc).get("status")
    if lifecycle_status == CANCELLED:
        raise ConceptQAConversationConflict(
            "A cancelled Q&A conversation cannot confirm formalisations",
            error_code="conversation_cancelled",
        )
    if lifecycle_status not in {ACTIVE, FINISHED}:
        raise ConceptQAConversationConflict(
            "Q&A conversation is not confirmable",
            error_code="conversation_not_confirmable",
        )
    source_turn_id, formalisation = _locate_formalisation_receipt(
        doc, resolved_assertion_id
    )
    if not source_turn_id or not isinstance(formalisation, Mapping):
        raise ConceptQAConversationNotFound(
            "Tentative formalisation was not found in this Q&A conversation",
            error_code="formalisation_not_found",
        )
    effective_org_id = _normalise_org_id(doc.get("organisation_concept_id")) or org_id
    resolved_request_id = (
        _required_text(request_id, field="request_id")
        if request_id
        else f"concept_q_and_a:{resolved_session_id}:{resolved_assertion_id}:confirm"
    )
    confirmation = _confirm_formalisation_effect(
        formalisation=formalisation,
        assertion_id=resolved_assertion_id,
        user_id=actor_id,
        organisation_concept_id=effective_org_id,
        namespace=resolved_namespace,
        request_id=resolved_request_id,
        expected_focal_concept_id=canonical_concept_id,
    )
    if confirmation.get("success") is True:
        confirmation = {
            **confirmation,
            "conversation_progress": _record_confirmed_requirement_disposition(
                user_id=actor_id,
                namespace=resolved_namespace,
                session_id=resolved_session_id,
                formalisation=formalisation,
            ),
        }
    updated_formalisation = dict(formalisation)
    updated_formalisation["status"] = confirmation.get("status", "tentative")
    updated_formalisation["epistemic_status"] = confirmation.get("status", "tentative")
    updated_formalisation["effect_status"] = confirmation.get("effect_status")
    if "canonical_read_back" in confirmation:
        updated_formalisation["canonical_read_back"] = confirmation.get(
            "canonical_read_back"
        )
    updated_formalisation["confirmation"] = {
        **dict(updated_formalisation.get("confirmation") or {}),
        "available": confirmation.get("success") is not True,
        "available_after_finish": True,
        "available_after_cancel": False,
        "last_attempt": confirmation,
    }
    _record_formalisation_receipt(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=resolved_session_id,
        source_turn_id=source_turn_id,
        formalisation=updated_formalisation,
    )
    next_predicate = None
    if confirmation.get("success") is True and lifecycle_status == ACTIVE:
        progress_doc = (
            _find_session(
                user_id=actor_id,
                namespace=resolved_namespace,
                session_id=resolved_session_id,
            )
            or doc
        )
        next_predicate = _next_unsuppressed_elicitation_predicate(
            concept_id=canonical_concept_id,
            session_doc=progress_doc,
            exclude_requirement=updated_formalisation,
        )
        next_question = str((next_predicate or {}).get("question") or "").strip()
        assistant_content = (
            "Confirmed. The formalisation has been read back as asserted. "
            + (
                next_question
                or "What other useful information should be recorded about this concept?"
            )
        )
    elif confirmation.get("effect_status") == "partial_success":
        assistant_content = (
            "The governed activation succeeded, but the tentative assertion still "
            "needs reconciliation. Its canonical read-back is preserved in the receipt."
        )
    else:
        assistant_content = _confirmation_failure_message(confirmation)
    result_turn_id: str | None = None
    if lifecycle_status == ACTIVE:
        result_turn_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                (
                    f"von:concept-qa:{resolved_session_id}:{resolved_assertion_id}:"
                    f"direct-confirm:{confirmation.get('status')}:"
                    f"{confirmation.get('effect_status')}"
                ),
            )
        )
        result_metadata: dict[str, Any] = {
            "kind": "formalisation_confirmation_result",
            "formalisation": updated_formalisation,
            "confirmation_source": "explicit_action",
        }
        if next_predicate:
            result_metadata["elicitation_predicate"] = next_predicate
        try:
            _append_turn_once(
                user_id=actor_id,
                namespace=resolved_namespace,
                session_id=resolved_session_id,
                message={
                    "role": "assistant",
                    "content": assistant_content,
                    "turn_id": result_turn_id,
                    "concept_q_and_a": result_metadata,
                },
                require_active=True,
            )
        except ConceptQAConversationConflict as exc:
            if exc.error_code != "conversation_terminal":
                raise
            # The effect receipt is durable. If Finish won the race, keep the
            # transcript terminal and let the response project the updated
            # source receipt without appending a post-terminal turn.
            result_turn_id = None
    readback = _find_session(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=resolved_session_id,
    )
    if readback is None:
        raise ConceptQAConversationError(
            "Could not read back formalisation confirmation",
            error_code="formalisation_confirmation_readback_failed",
        )
    return {
        **_project_session(readback),
        "success": confirmation.get("success") is True,
        "status": confirmation.get("status", "tentative"),
        "effect_status": confirmation.get("effect_status"),
        "assertion_id": resolved_assertion_id,
        "source_turn_id": source_turn_id,
        "assistant_turn_id": result_turn_id,
        "confirmed_after_finish": (
            _session_lifecycle(readback).get("status") == FINISHED
        ),
        "confirmation": _iso(confirmation),
        "receipts": {"formalisation": _iso(updated_formalisation)},
    }


def transition_concept_qa_conversation(
    *,
    concept_id: str,
    session_id: str,
    user_id: str,
    target_status: str,
    namespace: str | None = None,
    organisation_concept_id: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    if target_status not in TERMINAL_STATUSES:
        raise InvalidConceptQAConversationInput(
            "target_status must be finished or cancelled",
            error_code="invalid_lifecycle_target",
        )
    actor_id = _normalise_actor_id(user_id, field="user_id")
    resolved_session_id = _required_text(session_id, field="session_id")
    org_id = _normalise_org_id(organisation_concept_id)
    resolved_namespace = _resolve_namespace(
        user_id=actor_id,
        namespace=namespace,
        organisation_concept_id=org_id,
    )
    concept = _load_focal_concept(
        concept_id,
        user_id=actor_id,
        organisation_concept_id=org_id,
    )
    canonical_concept_id = str(concept["concept_id"])
    query = _actor_query(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=resolved_session_id,
    )
    query.update(
        {
            "focal_concept_ids": canonical_concept_id,
            "concept_q_and_a.lifecycle.status": ACTIVE,
        }
    )
    if expected_revision is not None:
        if isinstance(expected_revision, bool) or not isinstance(
            expected_revision, int
        ):
            raise InvalidConceptQAConversationInput(
                "expected_revision must be an integer",
                error_code="invalid_expected_revision",
            )
        query["concept_q_and_a.lifecycle.revision"] = expected_revision
    now = _now()
    try:
        result = _collection().update_one(
            query,
            {
                "$set": {
                    "concept_q_and_a.lifecycle.status": target_status,
                    "concept_q_and_a.lifecycle.terminal_at": now,
                    "concept_q_and_a.lifecycle.terminal_reason": (
                        "user_finished"
                        if target_status == FINISHED
                        else "user_cancelled"
                    ),
                    "updated_at": now,
                },
                "$inc": {"concept_q_and_a.lifecycle.revision": 1},
                "$unset": {"concept_q_and_a.active_key": ""},
            },
        )
    except PyMongoError as exc:
        raise ConceptQAConversationError(
            "Could not transition the Q&A lifecycle",
            error_code="lifecycle_write_failed",
        ) from exc
    readback = _find_session(
        user_id=actor_id,
        namespace=resolved_namespace,
        session_id=resolved_session_id,
    )
    if readback is None or canonical_concept_id not in list(
        readback.get("focal_concept_ids") or []
    ):
        raise ConceptQAConversationNotFound(
            "Q&A conversation was not found", error_code="conversation_not_found"
        )
    lifecycle = _session_lifecycle(readback)
    if getattr(result, "modified_count", 0) == 0:
        if lifecycle.get("status") == target_status:
            return {**_project_session(readback), "changed": False}
        raise ConceptQAConversationConflict(
            "Q&A lifecycle compare-and-set failed",
            error_code="lifecycle_compare_and_set_failed",
        )
    if lifecycle.get("status") != target_status:
        raise ConceptQAConversationError(
            "Q&A lifecycle read-back did not match the requested state",
            error_code="lifecycle_readback_failed",
        )
    return {**_project_session(readback), "changed": True}
