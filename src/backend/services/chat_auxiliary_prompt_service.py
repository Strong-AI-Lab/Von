"""Chat auxiliary prompt retrieval.

JVNAUTOSCI-797: Allow chat to include user-specific system prompts stored in the
Vontology as instances of `#V#von_llm_prompt` linked to the current user.

The UI provides a selected user concept ID, but the backend must only apply
prompts for the effective authenticated user.

This module keeps the logic small and testable.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import logging
import os

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.security.visibility_predicates import SPECIFIC_TO_USER_PREDICATES
from src.backend.services.concept_service import get_concept_by_concept_id
from src.backend.services.text_value_service import get_texts_for_concept

_VON_CHAT_BEHAVIOUR_PROMPT_TYPE = "#V#von_chat_behaviour_prompt"
# Legacy/alternate ID (US spelling) observed in stored concepts.
_VON_CHAT_BEHAVIOR_PROMPT_TYPE = "#V#von_chat_behavior_prompt"
_VON_CHAT_NARRATION_PROMPT_TYPE = "#V#von_chat_narration_prompt"
_LEGACY_VON_LLM_PROMPT_TYPE = "#V#von_llm_prompt"
_SPECIFIC_TO_USER_PREDICATE_CANDIDATES = (
    "#V#specific_to_von_user",
    "specific_to_von_user",
    *SPECIFIC_TO_USER_PREDICATES,
)


logger = logging.getLogger(__name__)

APPLIED_PROMPT_SNAPSHOT_SCHEMA_VERSION = "applied_prompt_snapshot.v1"
APPLIED_PROMPT_CONTEXT_SCHEMA_VERSION = "applied_prompt_context.v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_applied_prompt_snapshot(
    *,
    user_concept_id: str,
    namespace: str | None,
    organisation_concept_id: str | None,
    turn_id: str | None,
    behaviour_fragments: Sequence[Dict[str, Any]] = (),
    narration_fragments: Sequence[Dict[str, Any]] = (),
    screen_fragments: Sequence[Dict[str, Any]] = (),
    screen_prompt_text: str | None = None,
    screen_prompt_concept_ids: Sequence[str] = (),
    screen_prompt_source: str | None = None,
) -> Dict[str, Any]:
    """Build the exact server-bound represented-prompt snapshot for one turn."""

    prompts: List[Dict[str, Any]] = []

    def add_fragments(
        application_kind: str,
        application_surface: str,
        fragments: Sequence[Dict[str, Any]],
        *,
        source: str = "user_specific_vontology_prompt",
    ) -> None:
        for fragment in fragments:
            if not isinstance(fragment, dict):
                continue
            concept_id = fragment.get("concept_id")
            content = fragment.get("content")
            if not isinstance(concept_id, str) or not concept_id.strip():
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            normalised_content = content.strip()
            prompts.append(
                {
                    "application_kind": application_kind,
                    "application_surface": application_surface,
                    "application_order": len(prompts),
                    "concept_id": concept_id.strip(),
                    "content": normalised_content,
                    "content_char_count": len(normalised_content),
                    "content_sha256": _sha256_text(normalised_content),
                    "source": source,
                }
            )

    add_fragments("behaviour", "model_behaviour", behaviour_fragments)
    add_fragments("narration", "spoken_block", narration_fragments)
    add_fragments("screen", "screen_block", screen_fragments)

    # The screen prompt may come from the represented stage-prompt fallback rather
    # than the user's directly linked screen fragments. Preserve what was actually
    # applied, not merely what a fresh configuration lookup would find later.
    if (
        not any(item.get("application_kind") == "screen" for item in prompts)
        and isinstance(screen_prompt_text, str)
        and screen_prompt_text.strip()
    ):
        prompt_ids = [
            value.strip()
            for value in screen_prompt_concept_ids
            if isinstance(value, str) and value.strip()
        ]
        concept_id = prompt_ids[0] if prompt_ids else None
        if concept_id:
            normalised_content = screen_prompt_text.strip()
            prompts.append(
                {
                    "application_kind": "screen",
                    "application_surface": "screen_block",
                    "application_order": len(prompts),
                    "concept_id": concept_id,
                    "content": normalised_content,
                    "content_char_count": len(normalised_content),
                    "content_sha256": _sha256_text(normalised_content),
                    "source": screen_prompt_source or "represented_screen_prompt",
                }
            )

    snapshot: Dict[str, Any] = {
        "schema_version": APPLIED_PROMPT_SNAPSHOT_SCHEMA_VERSION,
        "turn_id": turn_id,
        "actor": {
            "user_concept_id": user_concept_id,
            "organisation_concept_id": organisation_concept_id,
            "namespace": namespace,
        },
        "prompt_count": len(prompts),
        "prompts": prompts,
    }
    snapshot["snapshot_sha256"] = _sha256_text(_canonical_json(snapshot))
    return snapshot


def serialise_applied_prompt_snapshot(snapshot: Dict[str, Any]) -> str:
    return _canonical_json(snapshot)


def normalise_applied_prompt_snapshot(
    value: Any,
    *,
    expected_user_concept_id: str | None = None,
    expected_namespace: str | None = None,
) -> Dict[str, Any] | None:
    """Validate and copy an exact turn-applied prompt snapshot.

    Persisted telemetry may carry the snapshot beyond the request that built it,
    so consumers must verify both its integrity hash and, when supplied, its
    actor scope before treating it as historical prompt evidence.
    """

    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            snapshot = json.loads(value)
        except (TypeError, ValueError):
            return None
    elif isinstance(value, Mapping):
        snapshot = copy.deepcopy(dict(value))
    else:
        return None
    if snapshot.get("schema_version") != APPLIED_PROMPT_SNAPSHOT_SCHEMA_VERSION:
        return None
    supplied_snapshot_sha256 = snapshot.get("snapshot_sha256")
    unsigned_snapshot = dict(snapshot)
    unsigned_snapshot.pop("snapshot_sha256", None)
    expected_snapshot_sha256 = _sha256_text(_canonical_json(unsigned_snapshot))
    if supplied_snapshot_sha256 != expected_snapshot_sha256:
        return None
    actor = snapshot.get("actor")
    prompts = snapshot.get("prompts")
    if not isinstance(actor, dict) or not isinstance(prompts, list):
        return None
    if (
        isinstance(expected_user_concept_id, str)
        and expected_user_concept_id.strip()
        and actor.get("user_concept_id") != expected_user_concept_id.strip()
    ):
        return None
    if (
        isinstance(expected_namespace, str)
        and expected_namespace.strip()
        and actor.get("namespace") != expected_namespace.strip()
    ):
        return None
    return snapshot


def build_applied_prompt_manifest_message(snapshot: Dict[str, Any]) -> str | None:
    prompts = snapshot.get("prompts")
    if not isinstance(prompts, list) or not prompts:
        return None
    manifest = {
        "schema_version": "applied_prompt_manifest.v1",
        "turn_id": snapshot.get("turn_id"),
        "snapshot_sha256": snapshot.get("snapshot_sha256"),
        "prompts": [
            {
                "application_kind": item.get("application_kind"),
                "application_surface": item.get("application_surface"),
                "concept_id": item.get("concept_id"),
                "content_char_count": item.get("content_char_count"),
                "content_sha256": item.get("content_sha256"),
            }
            for item in prompts
            if isinstance(item, dict)
        ],
    }
    return (
        "APPLIED VONTOLOGY PROMPT MANIFEST (server-derived):\n"
        "These represented prompts are owned by or applicable to the authenticated "
        "actor and are distinct from provider/platform instructions. Their exact "
        "turn-applied content is inspectable with the delegated "
        "chat_get_applied_prompt_context capability.\n" + _canonical_json(manifest)
    )


def render_applied_prompt_context(
    snapshot_json: str,
    *,
    include_content: bool = True,
    max_chars: int = 20000,
) -> Dict[str, Any]:
    """Render a bounded actor-safe view of a trusted turn snapshot."""

    if not isinstance(snapshot_json, str) or not snapshot_json.strip():
        raise ValueError("trusted applied-prompt snapshot is required")
    snapshot = normalise_applied_prompt_snapshot(snapshot_json)
    if snapshot is None:
        raise ValueError("trusted applied-prompt snapshot is invalid or failed integrity checks")
    actor = snapshot.get("actor")
    prompts = snapshot.get("prompts")
    assert isinstance(actor, dict)
    assert isinstance(prompts, list)

    bounded_max_chars = max(0, min(int(max_chars), 50000))
    remaining = bounded_max_chars
    rendered_prompts: List[Dict[str, Any]] = []
    truncated = False
    for item in prompts:
        if not isinstance(item, dict):
            continue
        rendered = {
            key: item.get(key)
            for key in (
                "application_kind",
                "application_surface",
                "application_order",
                "concept_id",
                "content_char_count",
                "content_sha256",
                "source",
            )
        }
        content = item.get("content")
        if include_content and isinstance(content, str):
            returned_content = content[:remaining]
            rendered["content"] = returned_content
            rendered["content_truncated"] = len(returned_content) < len(content)
            remaining -= len(returned_content)
            truncated = truncated or rendered["content_truncated"]
        rendered_prompts.append(rendered)

    return {
        "success": True,
        "schema_version": APPLIED_PROMPT_CONTEXT_SCHEMA_VERSION,
        "source": "server_bound_turn_snapshot",
        "turn_id": snapshot.get("turn_id"),
        "snapshot_sha256": snapshot.get("snapshot_sha256"),
        "actor": dict(actor),
        "prompt_count": len(rendered_prompts),
        "prompt_concept_ids": [
            item["concept_id"]
            for item in rendered_prompts
            if isinstance(item.get("concept_id"), str)
        ],
        "prompt_classes": list(
            dict.fromkeys(
                item["application_kind"]
                for item in rendered_prompts
                if isinstance(item.get("application_kind"), str)
            )
        ),
        "include_content": bool(include_content),
        "max_chars": bounded_max_chars,
        "content_truncated": truncated,
        "prompts": rendered_prompts,
    }


def _debug_user_prompt_logging_enabled() -> bool:
    value = os.getenv("VON_DEBUG_USER_PROMPT_LOADING", "")
    value = value.strip().lower()
    return value in {"1", "true", "yes", "on"}


def _normalise_predicate_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.startswith("#V#"):
        return value[3:]
    return value


def _get_prompt_content_for_concept(concept_id: str) -> Optional[str]:
    if not isinstance(concept_id, str) or not concept_id.strip():
        return None

    concept_id = concept_id.strip()

    try:
        concept = get_concept_by_concept_id(concept_id)
    except Exception:
        concept = None

    if isinstance(concept, dict):
        content = concept.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

    try:
        texts = get_texts_for_concept(concept_id)
    except Exception:
        texts = None

    if not isinstance(texts, list):
        return None

    content_candidates: List[Dict[str, Any]] = []
    for text in texts:
        if not isinstance(text, dict):
            continue
        predicate = _normalise_predicate_id(text.get("predicate"))
        if predicate not in {"hasContent", "hasDescription"}:
            continue
        text_value = text.get("text")
        if not isinstance(text_value, str):
            continue
        if not text_value.strip():
            continue
        content_candidates.append(text)

    if not content_candidates:
        return None

    def _sort_key(item: Dict[str, Any]) -> tuple[int, int, str]:
        predicate = _normalise_predicate_id(item.get("predicate"))
        lang = item.get("lang")
        text_value_id = item.get("text_value_id")

        predicate_rank = 0 if predicate == "hasContent" else 1
        lang_rank = 0 if lang in {"en-NZ", "en"} else 1
        text_id = text_value_id if isinstance(text_value_id, str) else ""

        return (predicate_rank, lang_rank, text_id)

    content_candidates.sort(key=_sort_key)

    joined = "\n\n".join(candidate["text"].strip() for candidate in content_candidates)
    joined = joined.strip()
    return joined if joined else None


def build_user_specific_system_prompt(
    user_concept_id: str,
    *,
    prompt_types: Sequence[str] | None = None,
) -> Optional[str]:
    """Build a system prompt string for the given authenticated user.

    We locate concepts that:
    - are instances of `#V#von_chat_behaviour_prompt` (or legacy `#V#von_llm_prompt`), and
    - are linked to the user via `#V#specific_to_von_user`.

    If multiple prompts exist, we concatenate them in a deterministic order.
    """

    fragments = get_user_specific_prompt_fragments(
        user_concept_id, prompt_types=prompt_types
    )
    prompt_texts = [
        fragment["content"] for fragment in fragments if fragment.get("content")
    ]
    if not prompt_texts:
        return None

    return "\n\n".join(prompt_texts)


def _normalise_prompt_types(
    prompt_types: Sequence[str] | None,
) -> Tuple[str, ...]:
    if prompt_types is None:
        # Default behaviour: apply the user's chat behaviour prompt. We keep
        # the legacy type as a fallback so restored data continues to work.
        return (
            _VON_CHAT_BEHAVIOUR_PROMPT_TYPE,
            _VON_CHAT_BEHAVIOR_PROMPT_TYPE,
            _LEGACY_VON_LLM_PROMPT_TYPE,
        )

    normalised: List[str] = []
    for value in prompt_types:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value:
            continue
        if value not in normalised:
            normalised.append(value)

    return tuple(normalised)


def get_user_specific_prompt_fragments(
    user_concept_id: str,
    *,
    prompt_types: Sequence[str] | None = None,
) -> List[Dict[str, Any]]:
    """Return the list of Vontology prompt fragments for a user.

    Each fragment is a dict with:
    - concept_id: str
    - content: str

    The list is returned in deterministic order.
    """

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return []

    user_concept_id = user_concept_id.strip()

    debug_enabled = _debug_user_prompt_logging_enabled()

    type_candidates = _normalise_prompt_types(prompt_types)
    if not type_candidates:
        return []

    relationship_fields = [
        f"relationships.{predicate}"
        for predicate in _SPECIFIC_TO_USER_PREDICATE_CANDIDATES
        if isinstance(predicate, str) and predicate.strip()
    ]

    projection = {"concept_id": 1, "name": 1}
    if debug_enabled:
        for field in relationship_fields:
            projection[field] = 1

    prompt_candidates = ConceptsRepository.find(
        {
            "relationships.is_an_instance_of": {"$in": list(type_candidates)},
            "$or": [{field: user_concept_id} for field in relationship_fields],
        },
        projection=projection,
        sort=[("concept_id", 1)],
        limit=50,
    )

    prompt_ids: List[str] = []
    for doc in prompt_candidates:
        concept_id = doc.get("concept_id") if isinstance(doc, dict) else None
        if isinstance(concept_id, str) and concept_id.startswith("#V#"):
            prompt_ids.append(concept_id)

            if debug_enabled:
                relationships = (
                    doc.get("relationships") if isinstance(doc, dict) else None
                )
                match_predicate = None
                if isinstance(relationships, dict):
                    for predicate in _SPECIFIC_TO_USER_PREDICATE_CANDIDATES:
                        values = relationships.get(predicate)
                        if isinstance(values, list) and user_concept_id in values:
                            match_predicate = predicate
                            break
                        if isinstance(values, str) and values == user_concept_id:
                            match_predicate = predicate
                            break

                logger.debug(
                    "User prompt concept loaded: concept_id=%s user_concept_id=%s match_predicate=%s",
                    concept_id,
                    user_concept_id,
                    match_predicate or "(unknown)",
                )

    if not prompt_ids:
        return []

    fragments: List[Dict[str, Any]] = []
    for concept_id in prompt_ids:
        content = _get_prompt_content_for_concept(concept_id)
        if isinstance(content, str) and content.strip():
            fragments.append({"concept_id": concept_id, "content": content.strip()})

    return fragments
