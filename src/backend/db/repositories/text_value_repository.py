from __future__ import annotations

from typing import Any, Dict, List, Optional
from pymongo.collection import Collection
from pymongo.database import Database
import logging

from ..mongo_client import (
    get_db,
    get_text_values_collection,
    get_text_relations_collection,
)

logger = logging.getLogger(__name__)


class TextValuesRepository:
    """Repository for the 'text_values' collection."""

    @staticmethod
    def collection() -> Collection | None:
        return get_text_values_collection()

    @staticmethod
    def db() -> Database | None:
        return get_db()

    # Basic wrappers
    @staticmethod
    def find_one(
        filter: Dict[str, Any], projection: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        coll = TextValuesRepository.collection()
        if coll is None:
            return None
        return coll.find_one(filter, projection)

    @staticmethod
    def find(
        filter: Dict[str, Any],
        projection: Optional[Dict[str, Any]] = None,
        sort: Optional[List] = None,
        skip: int = 0,
        limit: int = 0,
        max_time_ms: Optional[int] = None,
    ):
        coll = TextValuesRepository.collection()
        if coll is None:
            return []
        cursor = coll.find(filter, projection)
        if max_time_ms and max_time_ms > 0:
            cursor = cursor.max_time_ms(max_time_ms)
        if sort:
            cursor = cursor.sort(sort)
        if skip:
            cursor = cursor.skip(skip)
        if limit:
            cursor = cursor.limit(limit)
        return cursor

    @staticmethod
    def insert_one(document: Dict[str, Any]):
        coll = TextValuesRepository.collection()
        if coll is None:
            raise RuntimeError("text_values collection not available")
        return coll.insert_one(document)

    @staticmethod
    def update_one(
        filter: Dict[str, Any], update: Dict[str, Any], upsert: bool = False
    ):
        coll = TextValuesRepository.collection()
        if coll is None:
            raise RuntimeError("text_values collection not available")
        return coll.update_one(filter, update, upsert=upsert)

    @staticmethod
    def update_many(filter: Dict[str, Any], update: Dict[str, Any]):
        coll = TextValuesRepository.collection()
        if coll is None:
            raise RuntimeError("text_values collection not available")
        return coll.update_many(filter, update)

    @staticmethod
    def find_one_and_update(
        filter: Dict[str, Any], update: Dict[str, Any], return_document: bool = False
    ):
        coll = TextValuesRepository.collection()
        if coll is None:
            return None
        return coll.find_one_and_update(filter, update, return_document=return_document)

    @staticmethod
    def delete_one(filter: Dict[str, Any]):
        coll = TextValuesRepository.collection()
        if coll is None:
            raise RuntimeError("text_values collection not available")
        return coll.delete_one(filter)

    @staticmethod
    def delete_many(filter: Dict[str, Any]):
        coll = TextValuesRepository.collection()
        if coll is None:
            raise RuntimeError("text_values collection not available")
        return coll.delete_many(filter)

    @staticmethod
    def count_documents(filter: Dict[str, Any]) -> int:
        coll = TextValuesRepository.collection()
        if coll is None:
            return 0
        return coll.count_documents(filter)

    @staticmethod
    def aggregate(pipeline: List[Dict[str, Any]]):
        coll = TextValuesRepository.collection()
        if coll is None:
            return []
        return coll.aggregate(pipeline)


class TextRelationsRepository:
    """Repository for the 'text_relations' collection."""

    @staticmethod
    def collection() -> Collection | None:
        return get_text_relations_collection()

    @staticmethod
    def db() -> Database | None:
        return get_db()

    # Basic wrappers
    @staticmethod
    def find_one(
        filter: Dict[str, Any], projection: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        coll = TextRelationsRepository.collection()
        if coll is None:
            return None
        return coll.find_one(filter, projection)

    @staticmethod
    def find(
        filter: Dict[str, Any],
        projection: Optional[Dict[str, Any]] = None,
        sort: Optional[List] = None,
        skip: int = 0,
        limit: int = 0,
        max_time_ms: Optional[int] = None,
    ):
        coll = TextRelationsRepository.collection()
        if coll is None:
            return []
        cursor = coll.find(filter, projection)
        if max_time_ms and max_time_ms > 0:
            cursor = cursor.max_time_ms(max_time_ms)
        if sort:
            cursor = cursor.sort(sort)
        if skip:
            cursor = cursor.skip(skip)
        if limit:
            cursor = cursor.limit(limit)
        return cursor

    @staticmethod
    def insert_one(document: Dict[str, Any]):
        coll = TextRelationsRepository.collection()
        if coll is None:
            raise RuntimeError("text_relations collection not available")
        return coll.insert_one(document)

    @staticmethod
    def update_one(
        filter: Dict[str, Any], update: Dict[str, Any], upsert: bool = False
    ):
        coll = TextRelationsRepository.collection()
        if coll is None:
            raise RuntimeError("text_relations collection not available")
        return coll.update_one(filter, update, upsert=upsert)

    @staticmethod
    def update_many(filter: Dict[str, Any], update: Dict[str, Any]):
        coll = TextRelationsRepository.collection()
        if coll is None:
            raise RuntimeError("text_relations collection not available")
        return coll.update_many(filter, update)

    @staticmethod
    def find_one_and_update(
        filter: Dict[str, Any], update: Dict[str, Any], return_document: bool = False
    ):
        coll = TextRelationsRepository.collection()
        if coll is None:
            return None
        return coll.find_one_and_update(filter, update, return_document=return_document)

    @staticmethod
    def delete_one(filter: Dict[str, Any]):
        coll = TextRelationsRepository.collection()
        if coll is None:
            raise RuntimeError("text_relations collection not available")
        return coll.delete_one(filter)

    @staticmethod
    def delete_many(filter: Dict[str, Any]):
        coll = TextRelationsRepository.collection()
        if coll is None:
            raise RuntimeError("text_relations collection not available")
        return coll.delete_many(filter)

    @staticmethod
    def count_documents(filter: Dict[str, Any]) -> int:
        coll = TextRelationsRepository.collection()
        if coll is None:
            return 0
        return coll.count_documents(filter)

    @staticmethod
    def aggregate(pipeline: List[Dict[str, Any]]):
        coll = TextRelationsRepository.collection()
        if coll is None:
            return []
        return coll.aggregate(pipeline)
