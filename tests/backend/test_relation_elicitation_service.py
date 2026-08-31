# src/backend/services/tests/test_relation_elicitation_service.py
import unittest
from unittest.mock import patch, MagicMock

from src.backend.services.relation_elicitation_service import RelationElicitationService


class TestRelationElicitationService(unittest.TestCase):
    @patch("src.backend.services.concept_service.get_concept_by_concept_id")
    @patch("src.backend.services.concept_service.get_concept_by_id")
    def test_get_elicitation_opportunities(
        self, mock_get_by_id, mock_get_by_concept_id
    ):
        # Arrange
        instance_id = "instance_123"
        type_id = "#V#Person"

        mock_get_by_id.return_value = {
            "concept_id": instance_id,
            "name": "Test Person",
            "relationships": {
                "is_an_instance_of": [type_id],
                "existing_relation": ["some_value"],
            },
            "hypothesized_relations": {
                "hypothesized_relation": [{"value": "some_hypothesis"}]
            },
        }

        mock_get_by_concept_id.return_value = {
            "concept_id": type_id,
            "name": "Person",
            "relationships": {
                "suggested_relations_for_type": [
                    "existing_relation",
                    "hypothesized_relation",
                    "new_opportunity",
                ]
            },
        }

        service = RelationElicitationService()

        # Act
        opportunities = service.get_elicitation_opportunities(instance_id)

        # Assert
        self.assertEqual(opportunities, ["new_opportunity"])
        mock_get_by_id.assert_called_once_with(instance_id)
        mock_get_by_concept_id.assert_called_once_with(type_id)

    @patch("src.backend.services.concept_service.get_concept_by_concept_id")
    @patch("src.backend.services.concept_service.get_concept_by_id")
    def test_get_elicitation_opportunities_including_hypotheses(
        self, mock_get_by_id, mock_get_by_concept_id
    ):
        instance_id = "instance_123"
        type_id = "#V#Person"
        mock_get_by_id.return_value = {
            "concept_id": instance_id,
            "relationships": {"is_an_instance_of": [type_id]},
            "hypothesized_relations": {
                "hypothesized_relation": [{"value": "#V#x", "confidence_score": 0.99}]
            },
        }
        mock_get_by_concept_id.return_value = {
            "concept_id": type_id,
            "relationships": {
                "suggested_relations_for_type": [
                    "hypothesized_relation",
                    "new_opportunity",
                ]
            },
        }

        service = RelationElicitationService()
        opportunities = service.get_elicitation_opportunities(
            instance_id,
            include_hypothesized=True,
        )

        self.assertEqual(opportunities, ["hypothesized_relation", "new_opportunity"])

    @patch("src.backend.services.concept_service.get_concept_by_id")
    @patch(
        "src.backend.services.relation_elicitation_service.ConceptsRepository.find"
    )
    def test_get_elicitation_plan_exposes_predicate_and_question(
        self, mock_repo_find, mock_get_by_id
    ):
        mock_get_by_id.return_value = {
            "concept_id": "#V#primary_labs",
            "name": "Primary Labs",
            "relationships": {"is_an_instance_of": ["#V#organisation"]},
        }

        mock_repo_find.side_effect = [
            [
                {
                    "concept_id": "#V#organisation",
                    "inherited_salient_binary_predicates": [
                        "#V#has_member_role"
                    ],
                    "relationships": {},
                }
            ],
            [
                {
                    "concept_id": "#V#has_member_role",
                    "name": "has member role",
                }
            ],
        ]
        service = RelationElicitationService(llm_client=False)

        assert service.get_elicitation_plan("#V#primary_labs", limit=1) == [
            {
                "predicate_concept_id": "#V#has_member_role",
                "predicate_label": "has member role",
                "priority": 1,
                "question": "What is has member role for Primary Labs?",
                "status": "missing",
            }
        ]

    @patch("src.backend.services.concept_service.get_concept_by_id")
    @patch(
        "src.backend.services.relation_elicitation_service.ConceptsRepository.find"
    )
    def test_get_elicitation_plan_recovers_from_stale_empty_inherited_cache(
        self, mock_repo_find, mock_get_by_id
    ):
        mock_get_by_id.return_value = {
            "concept_id": "#V#primary_labs",
            "name": "Primary Labs",
            "relationships": {
                "is_an_instance_of": ["#V#von_user_organisation"]
            },
        }
        mock_repo_find.side_effect = [
            [
                {
                    "concept_id": "#V#von_user_organisation",
                    "inherited_salient_binary_predicates": [],
                    "relationships": {"is_a_type_of": ["#V#organization"]},
                }
            ],
            [
                {
                    "concept_id": "#V#organization",
                    "relationships": {
                        "#V#salient_binary_predicate_for_type": ["#V#has_member"]
                    },
                }
            ],
            [{"concept_id": "#V#has_member", "name": "has member"}],
        ]

        plan = RelationElicitationService(llm_client=False).get_elicitation_plan(
            "#V#primary_labs",
            limit=1,
        )

        assert plan[0]["predicate_concept_id"] == "#V#has_member"
        assert plan[0]["question"] == "What is has member for Primary Labs?"
        assert mock_repo_find.call_count == 3

    @patch(
        "src.backend.services.relation_elicitation_service.upsert_uncertain_relationship_assertion"
    )
    @patch("src.backend.services.concept_service.get_concept_by_concept_id")
    def test_process_and_store_hypothesis_uses_canonical_uncertain_assertions(
        self,
        mock_get_by_concept_id,
        mock_upsert_uncertain,
    ):
        mock_get_by_concept_id.return_value = {
            "concept_id": "understand_user_response_for_relation",
            "attributes": {
                "prompt_template": "Extract [Relation Name] from: [User Response]"
            },
        }
        mock_upsert_uncertain.return_value = {
            "success": True,
            "assertion": {"assertion_id": "ura_123", "status": "proposed"},
        }

        llm_client = MagicMock()
        llm_client.generate.return_value = "#V#strong_ai_lab"
        service = RelationElicitationService(llm_client=llm_client)

        result = service.process_and_store_hypothesis(
            "#V#alice",
            "#V#has_affiliation",
            "Alice is affiliated with Strong AI Lab.",
        )
        assert result is not None

        self.assertEqual(result["assertion_id"], "ura_123")
        self.assertEqual(result["value"], "#V#strong_ai_lab")
        self.assertEqual(result["confidence_score"], 0.85)
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["stored_in"], "uncertain_relationship_assertions")
        mock_upsert_uncertain.assert_called_once()


def test_default_catalogue_exposes_shared_concept_elicitation_capability():
    from src.backend.integrations.internal_mcp import build_default_catalogue

    definition = build_default_catalogue().get("get_concept_elicitation_opportunities")

    assert definition is not None
    assert definition.category == "read"
    assert "mixed-initiative" in definition.description
    assert "formalisation candidate" in definition.description


if __name__ == "__main__":
    unittest.main()
