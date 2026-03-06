# src/backend/services/relation_elicitation_service.py
from typing import List, Dict, Any, Optional

from ..services import concept_service
from ..db.repositories.concepts_repository import ConceptsRepository
from .uncertain_relationship_service import (
    collect_uncertain_predicates,
    upsert_uncertain_relationship_assertion,
)


class RelationElicitationService:
    """Service for proactively enriching the knowledge graph."""

    def __init__(self, llm_client: Optional[Any] = None):
        """Initialise the service with an optional preconfigured LLM client."""
        self._llm_initialisation_error: Optional[Exception] = None
        if llm_client is not None:
            self.llm_client = llm_client
            return

        try:
            from ..languagemodels.llm_interface import get_llm_client

            self.llm_client = get_llm_client()
        except Exception as exc:  # pragma: no cover - defensive bootstrap
            # Defer raising until the client is actually required so tests that
            # patch downstream dependencies can still import this module
            self.llm_client = None
            self._llm_initialisation_error = exc

    def get_elicitation_opportunities(
        self,
        instance_id: str,
        *,
        include_reverse_subtypes: bool = True,
        include_hypothesized: bool = False,
    ) -> List[str]:
        """Return unfilled suggested or salient predicates for an individual.

        Traverses direct and ancestor types (forward is_a_type_of + reverse has_subtype) and
        unions values of both "suggested_relations_for_type" and "#V#salient_binary_predicate_for_type".
        Filters out predicates already present as relationship keys.
        By default, predicates already present in hypothesized_relations are also
        excluded; set include_hypothesized=True when downstream workflows need to
        process high-confidence hypotheses for auto-apply.
        """
        instance_concept = concept_service.get_concept_by_id(instance_id)
        if not instance_concept:
            return []

        relationships = instance_concept.get("relationships", {}) or {}
        instance_of = relationships.get("is_an_instance_of", [])
        if not instance_of:
            return []

        from ..db.repositories.concepts_repository import ConceptsRepository

        repo = ConceptsRepository

        candidate_predicates: List[str] = []
        seen_predicates = set()
        queue = list(instance_of)
        visited_types = set()
        max_expansions = 200
        expansions = 0
        while queue and expansions < max_expansions:
            t_id = queue.pop(0)
            if not isinstance(t_id, str) or not t_id or t_id in visited_types:
                continue
            visited_types.add(t_id)
            expansions += 1
            try:
                t_concept = concept_service.get_concept_by_concept_id(t_id)
            except Exception:
                t_concept = None
            if t_concept:
                trels = t_concept.get("relationships") or {}
                for rel_name in trels.get("suggested_relations_for_type", []) or []:
                    if isinstance(rel_name, str) and rel_name not in seen_predicates:
                        seen_predicates.add(rel_name)
                        candidate_predicates.append(rel_name)
                for pred in trels.get("#V#salient_binary_predicate_for_type", []) or []:
                    if isinstance(pred, str) and pred not in seen_predicates:
                        seen_predicates.add(pred)
                        candidate_predicates.append(pred)
                forward_parents = trels.get("is_a_type_of") or []
                if isinstance(forward_parents, list):
                    for p in forward_parents:
                        if (
                            isinstance(p, str)
                            and p not in visited_types
                            and p not in queue
                        ):
                            queue.append(p)
            if include_reverse_subtypes:
                try:
                    reverse_cursor = repo.find(
                        {"relationships.has_subtype": {"$in": [t_id]}},
                        {"concept_id": 1},
                    )
                    for d in reverse_cursor:
                        cid = d.get("concept_id")
                        if (
                            isinstance(cid, str)
                            and cid not in visited_types
                            and cid not in queue
                        ):
                            queue.append(cid)
                except Exception:
                    pass

        if not candidate_predicates:
            return []

        existing_relations = set(relationships.keys())
        existing_hypothesized = set(
            (instance_concept.get("hypothesized_relations") or {}).keys()
        )
        existing_uncertain = collect_uncertain_predicates(
            source_id=instance_id,
            source_doc=instance_concept,
            include_legacy=True,
        )
        return [
            c
            for c in candidate_predicates
            if c not in existing_relations
            and (
                include_hypothesized
                or (
                    c not in existing_hypothesized
                    and c not in existing_uncertain
                )
            )
        ]

    def generate_question_for_elicit(
        self, instance_id: str, predicate: str
    ) -> Optional[str]:
        """Generate a natural language question for a relation."""
        instance_concept = concept_service.get_concept_by_id(instance_id)
        if not instance_concept:
            return None
        instance_name = instance_concept.get("name", "this concept")
        question_prompt_concept = concept_service.get_concept_by_concept_id(
            "user_question_prompt_for_relation"
        )
        if not question_prompt_concept:
            return None
        question_template = question_prompt_concept.get("attributes", {}).get(
            "prompt_template"
        )
        if not question_template:
            return None
        return question_template.replace("[Instance Name]", instance_name).replace(
            "[Relation Name]", predicate.replace("#V#", "").replace("_", " ")
        )

    def process_and_store_hypothesis(
        self, instance_id: str, predicate: str, answer: str
    ) -> Optional[Dict[str, Any]]:
        """Process user answer and store a canonical uncertain assertion.

        Legacy ``hypothesized_relations`` writes are intentionally avoided here.
        Workflow-governed acquisition should persist through the canonical
        ``uncertain_relationship_assertions`` pathway so promotion, auditing,
        and low-imposition policy all operate on the same source of truth.
        """
        understanding_prompt_concept = concept_service.get_concept_by_concept_id(
            "understand_user_response_for_relation"
        )
        if not understanding_prompt_concept:
            return None
        understanding_template = understanding_prompt_concept.get("attributes", {}).get(
            "prompt_template"
        )
        if not understanding_template:
            return None
        if self.llm_client is None:
            raise RuntimeError(
                "LLM client unavailable for relation elicitation"
            ) from self._llm_initialisation_error

        understanding_prompt = understanding_template.replace(
            "[User Response]", answer
        ).replace("[Relation Name]", predicate.replace("#V#", "").replace("_", " "))
        extracted_value = self.llm_client.generate(understanding_prompt)
        hypothesis = {
            "value": extracted_value.strip(),
            "confidence_score": 0.85,
            "source_interaction_id": "interaction_abc_123",
            "source": "llm_extraction",
            "evidence_count": 1,
        }
        upsert_result = upsert_uncertain_relationship_assertion(
            source_id=instance_id,
            predicate=predicate,
            target=hypothesis["value"],
            confidence_score=float(hypothesis["confidence_score"]),
            provenance={
                "source": "relation_elicitation_service",
                "source_interaction_id": hypothesis["source_interaction_id"],
                "extraction_source": hypothesis["source"],
            },
            evidence_count=int(hypothesis.get("evidence_count", 1)),
        )
        if not bool(upsert_result.get("success")):
            return None

        assertion = upsert_result.get("assertion")
        assertion = dict(assertion) if isinstance(assertion, dict) else {}
        return {
            "assertion_id": assertion.get("assertion_id"),
            "value": hypothesis["value"],
            "confidence_score": float(hypothesis["confidence_score"]),
            "source_interaction_id": hypothesis["source_interaction_id"],
            "source": hypothesis["source"],
            "evidence_count": int(hypothesis.get("evidence_count", 1)),
            "status": assertion.get("status", "proposed"),
            "stored_in": "uncertain_relationship_assertions",
        }

    def elicit_relation(
        self, instance_id: str, predicate: str
    ) -> Optional[Dict[str, Any]]:
        """
        Manages the Q&A flow to elicit a value for a specific relation and
        creates a hypothesis. (Interactive version for testing)

        Args:
            instance_id: The concept_id of the instance.
            predicate: The predicate of the relation to elicit.

        Returns:
            The newly created hypothesis, or None if the process fails.
        """
        question = self.generate_question_for_elicit(instance_id, predicate)
        if not question:
            return None

        print(f"Question for user: {question}")
        user_response = input("Your answer: ")

        return self.process_and_store_hypothesis(instance_id, predicate, user_response)
