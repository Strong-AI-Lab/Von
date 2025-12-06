from __future__ import annotations

from typing import Any, Dict, List, Optional
from pymongo.collection import Collection
from pymongo.database import Database
import logging

from ..mongo_client import get_db, get_meta_relations_collection

logger = logging.getLogger(__name__)


class MetaRelationsRepository:
    """Repository for the 'meta_relations' collection.
    Holds auxiliary documents supporting relation elicitation workflow.
    Document types (field `type`):
      - suggested_relations_for_type (keys: subject_type_id, predicate_ids[])
      - user_question_prompt_for_relation (keys: predicate_id, template, locale)
      - understand_user_response_for_relation (keys: predicate_id, parse_prompt)
    """

    @staticmethod
    def collection() -> Collection | None:
        return get_meta_relations_collection()

    @staticmethod
    def db() -> Database | None:
        return get_db()

    # Basic wrappers
    @staticmethod
    def find_one(filter: Dict[str, Any], projection: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        coll = MetaRelationsRepository.collection()
        if coll is None:
            return None
        return coll.find_one(filter, projection)

    @staticmethod
    def find(filter: Dict[str, Any], projection: Optional[Dict[str, Any]] = None, sort: Optional[List] = None, skip: int = 0, limit: int = 0):
        coll = MetaRelationsRepository.collection()
        if coll is None:
            return []
        cursor = coll.find(filter, projection)
        if sort:
            cursor = cursor.sort(sort)
        if skip:
            cursor = cursor.skip(skip)
        if limit:
            cursor = cursor.limit(limit)
        return cursor

    @staticmethod
    def insert_one(document: Dict[str, Any]):
        coll = MetaRelationsRepository.collection()
        if coll is None:
            raise RuntimeError("meta_relations collection not available")
        return coll.insert_one(document)

    @staticmethod
    def update_one(filter: Dict[str, Any], update: Dict[str, Any], upsert: bool = False):
        coll = MetaRelationsRepository.collection()
        if coll is None:
            raise RuntimeError("meta_relations collection not available")
        return coll.update_one(filter, update, upsert=upsert)

    @staticmethod
    def delete_one(filter: Dict[str, Any]):
        coll = MetaRelationsRepository.collection()
        if coll is None:
            raise RuntimeError("meta_relations collection not available")
        return coll.delete_one(filter)

    @staticmethod
    def count_documents(filter: Dict[str, Any]) -> int:
        coll = MetaRelationsRepository.collection()
        if coll is None:
            return 0
        return coll.count_documents(filter)
