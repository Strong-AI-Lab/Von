"""Tests for concept renaming functionality (JVNAUTOSCI-945).

Tests cover:
1. Validation of rename requests
2. Simulated rename operations
3. Alias resolution
4. Protected concept checks
"""

from unittest.mock import MagicMock, patch

from src.backend.utils.concept_id_utils import (
    validate_concept_id_for_rename,
    normalise_for_lookup,
)
from src.backend.services.concept_rename_service import (
    rename_concept,
    check_concept_id_available,
    check_concept_used_in_namespaces,
    resolve_concept_by_alias,
    get_concept_aliases,
    PROTECTED_CONCEPTS,
)


class TestValidateConceptIdForRename:
    """Tests for validate_concept_id_for_rename utility function."""

    def test_valid_rename_request(self):
        """Valid rename returns canonical new ID."""
        result, error = validate_concept_id_for_rename("#V#old_name", "#V#new_name")
        assert error is None
        assert result == "#V#new_name"

    def test_canonicalises_ids(self):
        """IDs are canonicalised before comparison."""
        result, error = validate_concept_id_for_rename("#V#Old-Name", "new name")
        assert error is None
        assert result == "#V#new_name"

    def test_rejects_same_id_after_canonicalisation(self):
        """Rejects rename when IDs are the same after canonicalisation."""
        result, error = validate_concept_id_for_rename("#V#foo_bar", "Foo-Bar")
        assert result is None
        assert error is not None
        assert "same as current" in error.lower()

    def test_rejects_invalid_old_id(self):
        """Rejects invalid old concept_id."""
        result, error = validate_concept_id_for_rename("", "#V#new_name")
        assert result is None
        assert error is not None
        assert "invalid" in error.lower()

    def test_rejects_invalid_new_id(self):
        """Rejects invalid new concept_id."""
        result, error = validate_concept_id_for_rename("#V#old", "")
        assert result is None
        assert error is not None
        assert "invalid" in error.lower()

    def test_rejects_too_long_slug(self):
        """Rejects slugs that exceed maximum length."""
        long_slug = "a" * 250
        result, error = validate_concept_id_for_rename("#V#old", f"#V#{long_slug}")
        assert result is None
        assert error is not None
        assert "maximum length" in error.lower()


class TestNormaliseForLookup:
    """Tests for normalise_for_lookup utility function."""

    def test_nfkc_normalisation(self):
        """Uses NFKC normalisation for lookup."""
        # Composed vs decomposed
        assert normalise_for_lookup("café") == normalise_for_lookup("cafe\u0301")

    def test_lowercases(self):
        """Lowercases for comparison."""
        assert normalise_for_lookup("HELLO") == "hello"

    def test_handles_compatibility_chars(self):
        """Handles compatibility characters (e.g., ﬁ ligature)."""
        assert normalise_for_lookup("ﬁle") == normalise_for_lookup("file")


class TestRenameConceptSimulation:
    """Tests for rename_concept with simulate=True."""

    @patch("src.backend.services.concept_rename_service.ConceptsRepository")
    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_simulate_returns_operations_report(
        self, mock_text_repo, mock_concepts_repo
    ):
        """Simulation returns a detailed operations report."""

        # Mock: old concept exists, new ID is available
        def find_one_side_effect(query):
            concept_id = query.get("concept_id")
            if concept_id == "#V#old_name":
                return {"concept_id": "#V#old_name", "guid": "test-guid-123"}
            return None  # New ID doesn't exist

        mock_concepts_repo.find_one.side_effect = find_one_side_effect
        mock_concepts_repo.find.return_value = []
        mock_text_repo.find_one.return_value = None
        mock_text_repo.count_documents.return_value = 0

        result = rename_concept("#V#old_name", "#V#new_name", simulate=True)

        assert result["success"] is True
        assert result["simulate"] is True
        assert "operations" in result
        assert any(op["type"] == "update_concept_id" for op in result["operations"])

    @patch("src.backend.services.concept_rename_service.ConceptsRepository")
    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_simulate_detects_affected_relationships(
        self, mock_text_repo, mock_concepts_repo
    ):
        """Simulation detects concepts with affected relationships."""

        def find_one_side_effect(query):
            concept_id = query.get("concept_id")
            if concept_id == "#V#old_name":
                return {"concept_id": "#V#old_name", "guid": "test-guid-123"}
            return None  # New ID doesn't exist

        mock_concepts_repo.find_one.side_effect = find_one_side_effect
        # Simulate one concept with a relationship to old_name
        mock_concepts_repo.find.return_value = [
            {
                "concept_id": "#V#related",
                "relationships": {"related_to": ["#V#old_name", "#V#other"]},
            }
        ]
        mock_text_repo.find_one.return_value = None
        mock_text_repo.count_documents.return_value = 0

        result = rename_concept("#V#old_name", "#V#new_name", simulate=True)

        assert result["success"] is True
        # Should detect the relationship reference
        rewrite_ops = [
            op for op in result["operations"] if "relationship" in op["type"]
        ]
        assert len(rewrite_ops) > 0

    @patch("src.backend.services.concept_rename_service.ConceptsRepository")
    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_protected_concept_rejected(self, mock_text_repo, mock_concepts_repo):
        """Protected concepts cannot be renamed."""
        for protected_id in list(PROTECTED_CONCEPTS)[:2]:
            result = rename_concept(protected_id, "#V#new_name", simulate=True)
            assert result["success"] is False
            assert any("protected" in err.lower() for err in result["errors"])

    @patch("src.backend.services.concept_rename_service.ConceptsRepository")
    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_code_mentioned_concept_rejected(self, mock_text_repo, mock_concepts_repo):
        """Concepts mentioned in Von code cannot be renamed."""
        mock_concepts_repo.find_one.return_value = {
            "concept_id": "#V#code_linked",
            "guid": "123e4567-e89b-12d3-a456-426614174000",
            "relationships": {
                "is_an_instance_of": ["#V#mentioned_in_von_code"],
            },
        }

        result = rename_concept("#V#code_linked", "#V#new_name", simulate=True)

        assert result["success"] is False
        assert any("mentioned in von code" in err.lower() for err in result["errors"])
        assert any("cannot be renamed" in err.lower() for err in result["errors"])

    @patch("src.backend.services.concept_rename_service.get_db")
    @patch("src.backend.services.concept_rename_service.ConceptsRepository")
    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_namespace_used_concept_rejected(
        self, mock_text_repo, mock_concepts_repo, mock_get_db
    ):
        """Concepts used in namespaces cannot be renamed."""
        mock_concepts_repo.find_one.return_value = {
            "concept_id": "#V#namespace_user",
            "guid": "123e4567-e89b-12d3-a456-426614174000",
            "relationships": {},
        }
        # Mock database with chat sessions using this concept
        mock_db = MagicMock()
        mock_chat_coll = MagicMock()
        mock_chat_coll.count_documents.side_effect = [
            5,  # as user_id
            0,  # as organisation_concept_id
            0,  # in namespace string
        ]
        mock_interactions_coll = MagicMock()
        mock_interactions_coll.count_documents.return_value = 0
        mock_db.get_collection.side_effect = lambda name: (
            mock_chat_coll if name == "chat_history" else mock_interactions_coll
        )
        mock_get_db.return_value = mock_db

        result = rename_concept("#V#namespace_user", "#V#new_name", simulate=True)

        assert result["success"] is False
        assert any("namespace" in err.lower() for err in result["errors"])
        assert any("cannot be renamed" in err.lower() for err in result["errors"])

    @patch("src.backend.services.concept_rename_service.ConceptsRepository")
    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_nonexistent_concept_rejected(self, mock_text_repo, mock_concepts_repo):
        """Non-existent concepts cannot be renamed."""
        mock_concepts_repo.find_one.return_value = None

        result = rename_concept("#V#nonexistent", "#V#new_name", simulate=True)

        assert result["success"] is False
        assert any("not found" in err.lower() for err in result["errors"])


class TestCheckConceptIdAvailable:
    """Tests for check_concept_id_available."""

    @patch("src.backend.services.concept_rename_service.ConceptsRepository")
    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_available_id_returns_true(self, mock_text_repo, mock_concepts_repo):
        """Available ID returns (True, None)."""
        mock_concepts_repo.find_one.return_value = None
        mock_text_repo.find_one.return_value = None

        available, error = check_concept_id_available("#V#available_name")

        assert available is True
        assert error is None

    @patch("src.backend.services.concept_rename_service.ConceptsRepository")
    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_existing_concept_returns_false(self, mock_text_repo, mock_concepts_repo):
        """Existing concept ID returns (False, error)."""
        mock_concepts_repo.find_one.return_value = {"concept_id": "#V#taken"}
        mock_text_repo.find_one.return_value = None

        available, error = check_concept_id_available("#V#taken")

        assert available is False
        assert error is not None
        assert "already exists" in error.lower()

    @patch("src.backend.services.concept_rename_service.ConceptsRepository")
    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_aliased_id_returns_false(self, mock_text_repo, mock_concepts_repo):
        """ID that is an alias returns (False, error)."""
        mock_concepts_repo.find_one.return_value = None
        mock_text_repo.find_one.return_value = {
            "subject_concept_id": "#V#current_name",
            "text": "#V#old_alias",
        }

        available, error = check_concept_id_available("#V#old_alias")

        assert available is False
        assert error is not None
        assert "alias" in error.lower()


class TestCheckConceptUsedInNamespaces:
    """Tests for check_concept_used_in_namespaces."""

    @patch("src.backend.services.concept_rename_service.get_db")
    def test_not_used_returns_false(self, mock_get_db):
        """Concept not used in namespaces returns (False, empty dict)."""
        mock_db = MagicMock()
        mock_coll = MagicMock()
        mock_coll.count_documents.return_value = 0
        mock_db.get_collection.return_value = mock_coll
        mock_get_db.return_value = mock_db

        is_used, usage = check_concept_used_in_namespaces("#V#unused_concept")

        assert is_used is False
        assert usage["as_user_id"] == 0
        assert usage["as_organisation_id"] == 0

    @patch("src.backend.services.concept_rename_service.get_db")
    def test_used_as_user_returns_true(self, mock_get_db):
        """Concept used as user_id returns (True, usage counts)."""
        mock_db = MagicMock()
        mock_chat_coll = MagicMock()
        mock_chat_coll.count_documents.side_effect = [10, 0, 0]  # user, org, namespace
        mock_interactions_coll = MagicMock()
        mock_interactions_coll.count_documents.return_value = 0
        mock_db.get_collection.side_effect = lambda name: (
            mock_chat_coll if name == "chat_history" else mock_interactions_coll
        )
        mock_get_db.return_value = mock_db

        is_used, usage = check_concept_used_in_namespaces("#V#user_concept")

        assert is_used is True
        assert usage["as_user_id"] == 10

    @patch("src.backend.services.concept_rename_service.get_db")
    def test_db_unavailable_returns_false(self, mock_get_db):
        """When DB is unavailable, returns False to avoid blocking renames."""
        mock_get_db.return_value = None

        is_used, usage = check_concept_used_in_namespaces("#V#some_concept")

        assert is_used is False
        assert usage == {}


class TestResolveConceptByAlias:
    """Tests for resolve_concept_by_alias."""

    @patch("src.backend.services.concept_rename_service.ConceptsRepository")
    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_direct_hit_returns_id(self, mock_text_repo, mock_concepts_repo):
        """Direct concept_id match returns the ID."""
        mock_concepts_repo.find_one.return_value = {"concept_id": "#V#direct"}

        result = resolve_concept_by_alias("#V#direct")

        assert result == "#V#direct"

    @patch("src.backend.services.concept_rename_service.ConceptsRepository")
    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_alias_resolves_to_current_id(self, mock_text_repo, mock_concepts_repo):
        """Alias resolves to current concept_id."""
        mock_concepts_repo.find_one.return_value = None
        mock_text_repo.find_one.return_value = {
            "subject_concept_id": "#V#current_name",
            "text": "#V#old_alias",
        }

        result = resolve_concept_by_alias("#V#old_alias")

        assert result == "#V#current_name"

    @patch("src.backend.services.concept_rename_service.ConceptsRepository")
    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_unknown_id_returns_none(self, mock_text_repo, mock_concepts_repo):
        """Unknown ID returns None."""
        mock_concepts_repo.find_one.return_value = None
        mock_text_repo.find_one.return_value = None

        result = resolve_concept_by_alias("#V#unknown")

        assert result is None


class TestGetConceptAliases:
    """Tests for get_concept_aliases."""

    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_returns_alias_list(self, mock_text_repo):
        """Returns list of aliases for a concept."""
        mock_text_repo.find.return_value = [
            {
                "text": "#V#old_name_1",
                "context": {"alias_source": "rename"},
                "created_at": "2026-01-01",
            },
            {
                "text": "#V#old_name_2",
                "context": {"alias_source": "rename"},
                "created_at": "2026-01-02",
            },
        ]

        aliases = get_concept_aliases("#V#current_name")

        assert len(aliases) == 2
        assert aliases[0]["alias"] == "#V#old_name_1"
        assert aliases[1]["alias"] == "#V#old_name_2"

    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_filters_non_concept_id_codes(self, mock_text_repo):
        """Filters out CODE names that don't look like concept IDs."""
        mock_text_repo.find.return_value = [
            {"text": "#V#old_alias", "context": {}},
            {"text": "not-a-concept-id", "context": {}},
            {"text": "abc-123-guid", "context": {}},
        ]

        aliases = get_concept_aliases("#V#current")

        # Should only include the #V# prefixed one
        assert len(aliases) == 1
        assert aliases[0]["alias"] == "#V#old_alias"

    @patch("src.backend.services.concept_rename_service.TextRelationsRepository")
    def test_invalid_id_returns_empty(self, mock_text_repo):
        """Invalid concept_id returns empty list."""
        aliases = get_concept_aliases("")

        assert aliases == []
        mock_text_repo.find.assert_not_called()
