"""Regression tests for database safety guardrails.

These guardrails exist to prevent accidental data loss in the production
Mongo database (von_db) when running tests or invoking destructive tools.
"""

from __future__ import annotations

import pytest


def test_assert_safe_database_name_for_pytest_raises_on_von_db():
    from src.backend.db import mongo_client

    with pytest.raises(RuntimeError, match=r"pytest.*VON_DB_NAME=von_db"):
        mongo_client.assert_safe_database_name_for_pytest("von_db")


def test_assert_safe_database_name_for_pytest_allows_test_db_names():
    from src.backend.db import mongo_client

    mongo_client.assert_safe_database_name_for_pytest("test_von_db")
    mongo_client.assert_safe_database_name_for_pytest("von_db_local")
