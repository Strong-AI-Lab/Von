from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
import logging

from ..db.repositories.meta_relations_repository import MetaRelationsRepository

logger = logging.getLogger(__name__)


class MetaRelationsServiceError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def upsert_suggested_relations_for_type(
    subject_type_id: str, predicate_ids: List[str], updated_by: str
) -> Dict[str, Any]:
    if not subject_type_id or not isinstance(subject_type_id, str):
        raise MetaRelationsServiceError("subject_type_id required")
    predicate_ids = [p for p in predicate_ids if isinstance(p, str) and p]
    doc = {
        "type": "suggested_relations_for_type",
        "subject_type_id": subject_type_id,
        "predicate_ids": predicate_ids,
        "updated_at": _utcnow(),
        "updated_by": updated_by,
    }
    MetaRelationsRepository.update_one(
        {"type": "suggested_relations_for_type", "subject_type_id": subject_type_id},
        {"$set": doc},
        upsert=True,
    )
    return doc


def get_suggested_relations_for_type(subject_type_id: str) -> Optional[List[str]]:
    doc = MetaRelationsRepository.find_one(
        {"type": "suggested_relations_for_type", "subject_type_id": subject_type_id}
    )
    if not doc:
        return None
    return doc.get("predicate_ids") or []


def upsert_question_prompt(
    predicate_id: str, template: str, locale: str, updated_by: str
) -> Dict[str, Any]:
    if not predicate_id:
        raise MetaRelationsServiceError("predicate_id required")
    if not template:
        raise MetaRelationsServiceError("template required")
    doc = {
        "type": "user_question_prompt_for_relation",
        "predicate_id": predicate_id,
        "template": template,
        "locale": locale or "en-US",
        "updated_at": _utcnow(),
        "updated_by": updated_by,
    }
    MetaRelationsRepository.update_one(
        {
            "type": "user_question_prompt_for_relation",
            "predicate_id": predicate_id,
            "locale": doc["locale"],
        },
        {"$set": doc},
        upsert=True,
    )
    return doc


def get_question_prompt(predicate_id: str, locale: str = "en-US") -> Optional[str]:
    doc = MetaRelationsRepository.find_one(
        {
            "type": "user_question_prompt_for_relation",
            "predicate_id": predicate_id,
            "locale": locale,
        }
    )
    if not doc:
        return None
    return doc.get("template")


def upsert_parse_prompt(
    predicate_id: str, parse_prompt: str, updated_by: str
) -> Dict[str, Any]:
    if not predicate_id:
        raise MetaRelationsServiceError("predicate_id required")
    if not parse_prompt:
        raise MetaRelationsServiceError("parse_prompt required")
    doc = {
        "type": "understand_user_response_for_relation",
        "predicate_id": predicate_id,
        "parse_prompt": parse_prompt,
        "updated_at": _utcnow(),
        "updated_by": updated_by,
    }
    MetaRelationsRepository.update_one(
        {"type": "understand_user_response_for_relation", "predicate_id": predicate_id},
        {"$set": doc},
        upsert=True,
    )
    return doc


def get_parse_prompt(predicate_id: str) -> Optional[str]:
    doc = MetaRelationsRepository.find_one(
        {
            "type": "understand_user_response_for_relation",
            "predicate_id": predicate_id,
        }
    )
    if not doc:
        return None
    return doc.get("parse_prompt")


def delete_meta_relation(doc_type: str, **identity_fields: Any) -> bool:
    if not doc_type:
        raise MetaRelationsServiceError("doc_type required")
    flt = {"type": doc_type}
    for k, v in identity_fields.items():
        flt[k] = v
    result = MetaRelationsRepository.delete_one(flt)
    return bool(getattr(result, "deleted_count", 0))
