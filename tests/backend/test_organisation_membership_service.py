"""Tests for organisation_membership_service.py

Tests the Vontology-based membership model that stores user-organisation
relationships and roles using the memberOf predicate and hasRole text relations.
"""

import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.backend.services.organisation_membership_service import (
    MEMBERSHIP_RELATIONSHIP_KIND,
    create_organisation_membership,
    get_organisation_members,
    get_user_memberships,
    organisation_role_storage_text,
    parse_organisation_role_storage_text,
    remove_organisation_membership,
    update_user_role,
)


@pytest.fixture(autouse=True)
def mock_membership_authority_barrier(monkeypatch):
    """Keep unit tests off live coordination storage."""

    monkeypatch.setattr(
        "src.backend.services.organisation_membership_service."
        "ontology_authority_membership_mutation_barrier",
        nullcontext,
    )
    monkeypatch.setattr(
        "src.backend.services.organisation_membership_service."
        "organisation_membership_scope_barrier",
        lambda *_args, **_kwargs: nullcontext(),
    )


@pytest.fixture(autouse=True)
def mock_window_authority_invalidation(monkeypatch):
    """Keep membership unit tests off window-session persistence."""

    invalidated_users = []
    monkeypatch.setattr(
        "src.backend.services.organisation_membership_service."
        "_invalidate_user_window_authority",
        lambda user_id, org_id: invalidated_users.append((user_id, org_id)),
    )
    return invalidated_users


@pytest.fixture
def mock_concepts_repo():
    """Mock ConceptsRepository for testing."""
    with patch(
        "src.backend.services.organisation_membership_service.ConceptsRepository"
    ) as mock:
        yield mock


@pytest.fixture
def mock_text_value_service():
    """Mock text_value_service for testing."""
    with patch(
        "src.backend.services.organisation_membership_service.upsert_text_for_concept"
    ) as mock:
        yield mock


@pytest.fixture
def mock_access_control():
    """Mock access control to allow all concepts."""
    with patch(
        "src.backend.services.organisation_membership_service.can_access_concept"
    ) as mock:
        mock.return_value = True
        yield mock


@pytest.fixture
def mock_text_repos():
    """Mock text value repositories."""
    with (
        patch(
            "src.backend.db.repositories.text_value_repository.TextRelationsRepository"
        ) as text_rels,
        patch(
            "src.backend.db.repositories.text_value_repository.TextValuesRepository"
        ) as text_vals,
    ):
        def find_text_value_by_id(value, projection=None):
            query = {"_id": value}
            if projection is None:
                return text_vals.find_one(query)
            return text_vals.find_one(query, projection)

        text_vals.find_one_by_id.side_effect = find_text_value_by_id
        yield text_rels, text_vals


# --- Tests for create_organisation_membership ---


class TestCreateOrganisationMembership:
    """Tests for creating organisation membership relationships."""

    def test_create_membership_success(
        self,
        mock_concepts_repo,
        mock_text_value_service,
        mock_access_control,
        mock_text_repos,
        mock_window_authority_invalidation,
    ):
        """Test successful creation of a membership relationship."""
        # Setup
        user_id = "#V#michael_witbrock"
        org_id = "#V#sail"
        role = "admin"

        # Multiple find_one calls: initial user & org lookups, then _check call
        mock_concepts_repo.find_one.side_effect = [
            {"concept_id": user_id, "relationships": {}},  # user concept
            {"concept_id": org_id, "relationships": {}},  # org concept
            {
                "concept_id": user_id,
                "relationships": {},
            },  # _check_relationship_exists lookup
        ]
        mock_concepts_repo.mutate_relationship_edge.return_value = True
        mock_text_value_service.return_value = {
            "text_value_id": "tv_123",
            "relation_id": "rel_456",
        }

        # Execute
        result = create_organisation_membership(user_id, org_id, role)

        # Assert
        assert result["user_concept_id"] == user_id
        assert result["organisation_concept_id"] == org_id
        assert result["role"] == role
        assert result["relationship_created"] is True
        assert (
            result["relationship_id"]
            == f"{user_id}::{MEMBERSHIP_RELATIONSHIP_KIND}::{org_id}"
        )

        # Verify mutate_relationship_edge was called
        mock_concepts_repo.mutate_relationship_edge.assert_called_once()
        call_args = mock_concepts_repo.mutate_relationship_edge.call_args
        assert call_args[1]["source_id"] == user_id
        assert call_args[1]["target_id"] == org_id
        assert call_args[1]["action"] == "add"

        # Verify text value service was called for role
        mock_text_value_service.assert_called_once()
        call_args = mock_text_value_service.call_args
        assert call_args[1]["subject_concept_id"] == user_id
        # Predicate is stored as namespaced concept identifier
        assert call_args[1]["predicate"] == "#V#hasRole"
        assert call_args[1]["text"] == f"{role}::{org_id}"
        assert mock_window_authority_invalidation == []

    def test_create_membership_already_exists(
        self,
        mock_concepts_repo,
        mock_text_value_service,
        mock_access_control,
        mock_window_authority_invalidation,
    ):
        """Test creating a membership that already exists."""
        user_id = "#V#michael_witbrock"
        org_id = "#V#sail"

        # Setup: membership already exists
        # find_one is called multiple times: initially in create function, then in _check_relationship_exists
        mock_concepts_repo.find_one.side_effect = [
            {
                "concept_id": user_id,
                "relationships": {"memberOf": [org_id]},
            },  # user lookup in create
            {"concept_id": org_id, "relationships": {}},  # org lookup in create
            {
                "concept_id": user_id,
                "relationships": {"memberOf": [org_id]},
            },  # user lookup in _check
        ]
        mock_text_value_service.return_value = {
            "text_value_id": "tv_123",
            "relation_id": "rel_456",
        }

        # Execute
        result = create_organisation_membership(user_id, org_id, "member")

        # Assert
        assert result["relationship_created"] is False
        mock_concepts_repo.mutate_relationship_edge.assert_not_called()
        assert mock_window_authority_invalidation == [(user_id, org_id)]

    def test_create_membership_invalid_user_id(self, mock_access_control):
        """Test creation with invalid user_id."""
        with pytest.raises(
            ValueError, match="user_concept_id must be a non-empty string"
        ):
            create_organisation_membership("", "#V#sail", "member")

    def test_create_membership_invalid_org_id(self, mock_access_control):
        """Test creation with invalid org_id."""
        with pytest.raises(
            ValueError, match="organisation_concept_id must be a non-empty string"
        ):
            create_organisation_membership("#V#user", "", "member")

    def test_create_membership_access_denied_user(
        self, mock_access_control, mock_concepts_repo
    ):
        """Test creation when user lacks access to user concept."""
        mock_access_control.side_effect = [False, True]  # First call fails

        with pytest.raises(PermissionError, match="Cannot access user concept"):
            create_organisation_membership("#V#user", "#V#sail", "member")

    def test_create_membership_access_denied_org(
        self, mock_access_control, mock_concepts_repo
    ):
        """Test creation when user lacks access to org concept."""
        mock_access_control.side_effect = [True, False]  # Second call fails

        with pytest.raises(PermissionError, match="Cannot access organisation concept"):
            create_organisation_membership("#V#user", "#V#sail", "member")

    def test_create_membership_user_not_found(
        self, mock_concepts_repo, mock_access_control
    ):
        """Test creation when user concept doesn't exist."""
        mock_concepts_repo.find_one.return_value = None

        with pytest.raises(ValueError, match="User concept .* not found"):
            create_organisation_membership("#V#nonexistent", "#V#sail", "member")

    def test_create_membership_org_not_found(
        self, mock_concepts_repo, mock_access_control
    ):
        """Test creation when org concept doesn't exist."""
        mock_concepts_repo.find_one.side_effect = [
            {"concept_id": "#V#user", "relationships": {}},  # user exists
            None,  # org doesn't exist
        ]

        with pytest.raises(ValueError, match="Organisation concept .* not found"):
            create_organisation_membership("#V#user", "#V#nonexistent", "member")


# --- Tests for get_user_memberships ---


class TestGetUserMemberships:
    """Tests for retrieving user's memberships."""

    def test_get_memberships_success(
        self, mock_concepts_repo, mock_access_control, mock_text_repos
    ):
        """Test successful retrieval of user memberships."""
        user_id = "#V#michael_witbrock"
        orgs = ["#V#sail", "#V#other_org"]

        # Setup
        mock_concepts_repo.find_one.return_value = {
            "concept_id": user_id,
            "relationships": {"memberOf": orgs},
        }

        text_rels, text_vals = mock_text_repos
        text_rels.find.return_value = [
            {
                "subject_concept_id": user_id,
                "predicate": "hasRole",
                "object_text_id": "tv_1",
                "context": {"organisation_id": "#V#sail"},
            },
            {
                "subject_concept_id": user_id,
                "predicate": "hasRole",
                "object_text_id": "tv_2",
                "context": {"organisation_id": "#V#other_org"},
            },
        ]

        text_vals.find_one.side_effect = [{"text": "admin"}, {"text": "member"}]

        # Execute
        result = get_user_memberships(user_id)

        # Assert
        assert result["user_concept_id"] == user_id
        assert result["total_memberships"] == 2
        assert len(result["memberships"]) == 2
        assert result["memberships"][0] == {
            "organisation_concept_id": "#V#sail",
            "role": "admin",
        }
        assert result["memberships"][1] == {
            "organisation_concept_id": "#V#other_org",
            "role": "member",
        }

    def test_get_memberships_empty(
        self, mock_concepts_repo, mock_access_control, mock_text_repos
    ):
        """Test retrieval when user has no memberships."""
        user_id = "#V#michael_witbrock"

        mock_concepts_repo.find_one.return_value = {
            "concept_id": user_id,
            "relationships": {},
        }

        text_rels, _ = mock_text_repos
        text_rels.find.return_value = []

        # Execute
        result = get_user_memberships(user_id)

        # Assert
        assert result["total_memberships"] == 0
        assert result["memberships"] == []

    def test_get_memberships_merges_canonical_and_legacy_relationships(
        self, mock_concepts_repo, mock_access_control, mock_text_repos
    ):
        """Both stored membership predicates remain visible and deduplicated."""
        user_id = "#V#michael_witbrock"
        mock_concepts_repo.find_one.return_value = {
            "concept_id": user_id,
            "relationships": {
                "memberOf": ["#V#university_of_auckland_strong_ai_lab"],
                "#V#member_of_organisation": [
                    "#V#the_lu_witbrock_household",
                    "#V#university_of_auckland_strong_ai_lab",
                ],
            },
        }
        text_rels, _ = mock_text_repos
        text_rels.find.return_value = []

        result = get_user_memberships(user_id)

        assert result["memberships"] == [
            {
                "organisation_concept_id": (
                    "#V#university_of_auckland_strong_ai_lab"
                ),
                "role": "member",
            },
            {
                "organisation_concept_id": "#V#the_lu_witbrock_household",
                "role": "member",
            },
        ]

    def test_get_memberships_decodes_scope_distinct_equal_roles(
        self, mock_concepts_repo, mock_access_control, mock_text_repos
    ):
        user_id = "#V#michael_witbrock"
        orgs = ["#V#org_a", "#V#org_b"]
        mock_concepts_repo.find_one.return_value = {
            "concept_id": user_id,
            "relationships": {"memberOf": orgs},
        }
        text_rels, text_vals = mock_text_repos
        text_rels.find.return_value = [
            {
                "subject_concept_id": user_id,
                "object_text_id": "owner-a",
                "context": {"organisation_id": "#V#org_a"},
            },
            {
                "subject_concept_id": user_id,
                "object_text_id": "owner-b",
                "context": {"organisation_id": "#V#org_b"},
            },
        ]
        text_vals.find_one.side_effect = [
            {"text": "owner::#V#org_a"},
            {"text": "owner::#V#org_b"},
        ]

        result = get_user_memberships(user_id)

        assert result["memberships"] == [
            {"organisation_concept_id": "#V#org_a", "role": "owner"},
            {"organisation_concept_id": "#V#org_b", "role": "owner"},
        ]

    def test_get_memberships_invalid_user_id(self, mock_access_control):
        """Test retrieval with invalid user_id."""
        with pytest.raises(
            ValueError, match="user_concept_id must be a non-empty string"
        ):
            get_user_memberships("")


def test_organisation_role_storage_is_scope_distinct_and_legacy_readable():
    first = organisation_role_storage_text("owner", "#V#org_a")
    second = organisation_role_storage_text("owner", "#V#org_b")

    assert first != second
    assert parse_organisation_role_storage_text(
        first, {"organisation_id": "#V#org_a"}
    ) == ("owner", "#V#org_a")
    assert parse_organisation_role_storage_text(
        "admin", {"organisation_id": "#V#org_a"}
    ) == ("admin", "#V#org_a")
    assert parse_organisation_role_storage_text(
        first, {"organisation_id": "#V#org_b"}
    ) == (None, None)

# --- Tests for get_organisation_members ---


class TestGetOrganisationMembers:
    """Tests for retrieving organisation members."""

    def test_get_members_success(
        self, mock_concepts_repo, mock_access_control, mock_text_repos
    ):
        """Test successful retrieval of organisation members."""
        org_id = "#V#sail"

        # Setup
        mock_concepts_repo.find_one.return_value = {
            "concept_id": org_id,
            "relationships": {},
        }

        text_rels, text_vals = mock_text_repos
        text_rels.find.return_value = [
            {
                "subject_concept_id": "#V#michael_witbrock",
                "predicate": "hasRole",
                "object_text_id": "tv_1",
                "context": {"organisation_id": org_id},
            },
            {
                "subject_concept_id": "#V#john_smith",
                "predicate": "hasRole",
                "object_text_id": "tv_2",
                "context": {"organisation_id": org_id},
            },
        ]

        text_vals.find_one.side_effect = [{"text": "admin"}, {"text": "member"}]

        # Execute
        result = get_organisation_members(org_id)

        # Assert
        assert result["organisation_concept_id"] == org_id
        assert result["total_members"] == 2
        members_by_user = {m["user_concept_id"]: m for m in result["members"]}
        assert members_by_user["#V#michael_witbrock"]["role"] == "admin"
        assert members_by_user["#V#john_smith"]["role"] == "member"

    def test_get_members_with_role_filter(
        self, mock_concepts_repo, mock_access_control, mock_text_repos
    ):
        """Test retrieval with role filter."""
        org_id = "#V#sail"

        mock_concepts_repo.find_one.return_value = {
            "concept_id": org_id,
            "relationships": {},
        }

        text_rels, text_vals = mock_text_repos
        text_rels.find.return_value = [
            {
                "subject_concept_id": "#V#michael_witbrock",
                "predicate": "hasRole",
                "object_text_id": "tv_1",
                "context": {"organisation_id": org_id},
            },
            {
                "subject_concept_id": "#V#john_smith",
                "predicate": "hasRole",
                "object_text_id": "tv_2",
                "context": {"organisation_id": org_id},
            },
        ]

        text_vals.find_one.side_effect = [{"text": "admin"}, {"text": "member"}]

        # Execute
        result = get_organisation_members(org_id, role_filter="admin")

        # Assert
        assert result["total_members"] == 1
        assert result["members"][0]["user_concept_id"] == "#V#michael_witbrock"
        assert result["members"][0]["role"] == "admin"

    def test_get_members_invalid_org_id(self, mock_access_control):
        """Test retrieval with invalid org_id."""
        with pytest.raises(
            ValueError, match="organisation_concept_id must be a non-empty string"
        ):
            get_organisation_members("")


# --- Tests for update_user_role ---


class TestUpdateUserRole:
    """Tests for updating user roles."""

    def test_update_role_success(
        self,
        mock_concepts_repo,
        mock_access_control,
        mock_text_value_service,
        mock_text_repos,
        mock_window_authority_invalidation,
    ):
        """Test successful role update."""
        user_id = "#V#michael_witbrock"
        org_id = "#V#sail"

        # Setup: user has membership
        mock_concepts_repo.find_one.side_effect = [
            {"concept_id": user_id, "relationships": {"memberOf": [org_id]}},
            {"concept_id": user_id, "relationships": {"memberOf": [org_id]}},
        ]

        text_rels, text_vals = mock_text_repos
        text_rels.find_one.return_value = {
            "subject_concept_id": user_id,
            "object_text_id": "tv_1",
            "context": {"organisation_id": org_id},
        }
        text_vals.find_one.return_value = {"text": "member"}

        mock_text_value_service.return_value = {
            "text_value_id": "tv_1",
            "relation_id": "rel_1",
        }

        # Execute
        result = update_user_role(user_id, org_id, "admin")

        # Assert
        assert result["user_concept_id"] == user_id
        assert result["organisation_concept_id"] == org_id
        assert result["new_role"] == "admin"
        assert result["role_updated"] is True
        assert result["previous_role"] == "member"
        assert mock_window_authority_invalidation == [(user_id, org_id)]

    def test_update_role_not_member(
        self, mock_concepts_repo, mock_access_control, mock_text_repos
    ):
        """Test role update for non-member."""
        user_id = "#V#michael_witbrock"
        org_id = "#V#sail"

        # Setup: user doesn't have membership
        mock_concepts_repo.find_one.return_value = {
            "concept_id": user_id,
            "relationships": {},
        }

        text_rels, _ = mock_text_repos
        text_rels.find.return_value = []

        with pytest.raises(ValueError, match="is not a member of organisation"):
            update_user_role(user_id, org_id, "admin")

    def test_update_role_no_change(
        self,
        mock_concepts_repo,
        mock_access_control,
        mock_text_repos,
        mock_window_authority_invalidation,
    ):
        """Test role update when role hasn't changed."""
        user_id = "#V#michael_witbrock"
        org_id = "#V#sail"

        mock_concepts_repo.find_one.side_effect = [
            {"concept_id": user_id, "relationships": {"memberOf": [org_id]}},
            {"concept_id": user_id, "relationships": {"memberOf": [org_id]}},
        ]

        text_rels, text_vals = mock_text_repos
        text_rels.find_one.return_value = {
            "subject_concept_id": user_id,
            "object_text_id": "tv_1",
            "context": {"organisation_id": org_id},
        }
        text_vals.find_one.return_value = {"text": f"admin::{org_id}"}

        # Execute
        result = update_user_role(user_id, org_id, "admin")

        # Assert
        assert result["role_updated"] is False
        assert mock_window_authority_invalidation == []


# --- Tests for remove_organisation_membership ---


class TestRemoveOrganisationMembership:
    """Tests for removing memberships."""

    def test_remove_membership_success(
        self,
        mock_concepts_repo,
        mock_access_control,
        mock_text_repos,
        mock_window_authority_invalidation,
        monkeypatch,
    ):
        """Test successful removal of membership."""
        user_id = "#V#michael_witbrock"
        org_id = "#V#sail"

        mock_concepts_repo.mutate_relationship_edge.return_value = True

        text_rels, _ = mock_text_repos
        text_rels.find_one.return_value = {
            "_id": "rel_id",
            "subject_concept_id": user_id,
            "predicate": "hasRole",
            "context": {"organisation_id": org_id},
        }
        monkeypatch.setattr(
            "src.backend.services.ontology_authority_role_service.authority_role_read_back",
            lambda **_kwargs: {"active": False, "grants": []},
        )

        # Execute
        result = remove_organisation_membership(user_id, org_id)

        # Assert
        assert result["user_concept_id"] == user_id
        assert result["organisation_concept_id"] == org_id
        assert result["membership_removed"] is True

        # Verify mutate_relationship_edge was called
        mock_concepts_repo.mutate_relationship_edge.assert_called_once()
        assert mock_window_authority_invalidation == [(user_id, org_id)]

    def test_remove_membership_requires_semantic_authority_revocation_first(
        self, mock_concepts_repo, mock_access_control, mock_text_repos, monkeypatch
    ):
        monkeypatch.setattr(
            "src.backend.services.ontology_authority_role_service.authority_role_read_back",
            lambda **_kwargs: {"active": True, "grants": [{"relation_id": "role"}]},
        )

        with pytest.raises(
            PermissionError,
            match="organisation_ontology_authority_must_be_revoked",
        ):
            remove_organisation_membership("#V#admin", "#V#org")

        mock_concepts_repo.mutate_relationship_edge.assert_not_called()

    def test_remove_membership_invalid_user_id(self, mock_access_control):
        """Test removal with invalid user_id."""
        with pytest.raises(
            ValueError, match="user_concept_id must be a non-empty string"
        ):
            remove_organisation_membership("", "#V#sail")
