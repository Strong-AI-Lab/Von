# src/backend/services/relation_elicitation_service.py
from typing import Any, Dict, List, Optional

from ..db.repositories.concepts_repository import ConceptsRepository
from ..services import concept_service
from .constitutive_relation_requirement_service import (
    ConstitutiveRelationRequirementService,
)
from .uncertain_relationship_service import (
    collect_uncertain_predicates,
    upsert_uncertain_relationship_assertion,
)


class RelationElicitationService:
    """Service for proactively enriching the knowledge graph."""

    def __init__(
        self,
        llm_client: Optional[Any] = None,
        *,
        constitutive_requirement_service: Optional[Any] = None,
    ):
        """Initialise the service with an optional preconfigured LLM client."""
        self.constitutive_requirement_service = (
            constitutive_requirement_service or ConstitutiveRelationRequirementService()
        )
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

        Traverses direct and ancestor types (forward ``is_a_type_of`` plus
        reverse ``has_subtype``) and combines suggested and salient predicates.
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
                or (c not in existing_hypothesized and c not in existing_uncertain)
            )
        ]

    def get_elicitation_plan(
        self,
        instance_id: str,
        *,
        limit: int = 6,
    ) -> List[Dict[str, Any]]:
        """Return bounded missing-predicate candidates with human questions.

        This is the shared read surface for concept-tab and ordinary
        conversation elicitation.  It does not assert that a generated answer
        fills the predicate; callers preserve exact user text first and treat
        any formalisation as a separately provenance-bearing enrichment.
        """

        bounded_limit = max(1, min(int(limit or 6), 12))
        instance = concept_service.get_concept_by_id(instance_id)
        if not instance:
            return []
        instance_name = str(instance.get("name") or "this concept").strip()
        relationships = instance.get("relationships") or {}
        direct_types: List[str] = []
        for instance_predicate in ("is_an_instance_of", "#V#is_an_instance_of"):
            values = relationships.get(instance_predicate) or []
            if isinstance(values, str):
                values = [values]
            for value in values:
                if isinstance(value, str) and value and value not in direct_types:
                    direct_types.append(value)

        # Prefer the recomputed inherited field used by the salient-predicate
        # API. If an empty cache is stale, recover through a small number of
        # batched ancestor reads rather than one lookup per ancestor.
        type_projection = {
            "concept_id": 1,
            "inherited_salient_binary_predicates": 1,
            "relationships.suggested_relations_for_type": 1,
            "relationships.#V#salient_binary_predicate_for_type": 1,
            "relationships.is_a_type_of": 1,
            "relationships.#V#is_a_type_of": 1,
        }
        type_docs = list(
            ConceptsRepository.find(
                {"concept_id": {"$in": direct_types}},
                type_projection,
            )
        )
        opportunities: List[str] = []
        seen_predicates: set[str] = set()
        visited_types: set[str] = set()
        type_depth_by_id: Dict[str, int] = {type_id: 0 for type_id in direct_types}

        def consume_type_docs(docs: List[Dict[str, Any]]) -> List[str]:
            next_parents: List[str] = []
            for doc in docs:
                concept_id = doc.get("concept_id")
                if isinstance(concept_id, str):
                    visited_types.add(concept_id)
                type_relationships = doc.get("relationships") or {}
                candidates = [
                    *(type_relationships.get("suggested_relations_for_type") or []),
                    *(
                        type_relationships.get("#V#salient_binary_predicate_for_type")
                        or []
                    ),
                    *(doc.get("inherited_salient_binary_predicates") or []),
                ]
                for candidate in candidates:
                    if (
                        isinstance(candidate, str)
                        and candidate
                        and candidate not in seen_predicates
                    ):
                        seen_predicates.add(candidate)
                        opportunities.append(candidate)
                for type_predicate in ("is_a_type_of", "#V#is_a_type_of"):
                    for parent in type_relationships.get(type_predicate) or []:
                        if (
                            isinstance(parent, str)
                            and parent
                            and parent not in visited_types
                            and parent not in next_parents
                        ):
                            next_parents.append(parent)
                            parent_depth = int(type_depth_by_id.get(concept_id, 0)) + 1
                            current_depth = type_depth_by_id.get(parent)
                            if current_depth is None or parent_depth < current_depth:
                                type_depth_by_id[parent] = parent_depth
            return next_parents

        pending_parents = consume_type_docs(type_docs)
        ancestor_batches = 0
        while pending_parents and ancestor_batches < 16 and len(visited_types) < 250:
            remaining_capacity = max(1, 250 - len(visited_types))
            current_batch = pending_parents[: min(32, remaining_capacity)]
            remaining_parents = pending_parents[len(current_batch) :]
            ancestor_docs = list(
                ConceptsRepository.find(
                    {"concept_id": {"$in": current_batch}},
                    type_projection,
                )
            )
            discovered_parents = consume_type_docs(ancestor_docs)
            pending_parents = list(
                dict.fromkeys(
                    parent
                    for parent in [*remaining_parents, *discovered_parents]
                    if parent not in visited_types
                )
            )
            ancestor_batches += 1

        # A constitutive relation is not merely salient.  Evaluate it against
        # canonical relation incidence (including incoming relations) and put
        # missing witnesses before optional salience prompts.  Any profile/read
        # failure remains advisory and must not block concept creation or the
        # rest of elicitation.
        try:
            constitutive_plan, _constitutive_diagnostics = (
                self.constitutive_requirement_service.get_missing_requirements(
                    instance=instance,
                    type_depth_by_id=type_depth_by_id,
                )
            )
        except Exception:
            constitutive_plan = []

        existing_relations = set(relationships.keys())
        existing_hypothesized = set(
            (instance.get("hypothesized_relations") or {}).keys()
        )
        existing_uncertain = collect_uncertain_predicates(
            source_id=instance_id,
            source_doc=instance,
            include_legacy=True,
        )
        opportunities = [
            predicate
            for predicate in opportunities
            if predicate not in existing_relations
            and predicate not in existing_hypothesized
            and predicate not in existing_uncertain
            and predicate
            not in {
                item.get("predicate_concept_id")
                for item in constitutive_plan
                if isinstance(item, dict)
            }
        ]

        plan: List[Dict[str, Any]] = [
            dict(item) for item in constitutive_plan[:bounded_limit]
        ]
        selected_predicates = opportunities[: max(0, bounded_limit - len(plan))]
        predicate_docs = (
            list(
                ConceptsRepository.find(
                    {"concept_id": {"$in": selected_predicates}},
                    {"concept_id": 1, "name": 1, "names": 1},
                )
            )
            if selected_predicates
            else []
        )
        predicate_docs_by_id = {
            str(doc.get("concept_id")): doc
            for doc in predicate_docs
            if doc.get("concept_id")
        }
        for predicate in selected_predicates:
            predicate_doc = predicate_docs_by_id.get(predicate) or {}
            predicate_label = str(
                predicate_doc.get("name")
                or predicate.replace("#V#", "").replace("_", " ")
            ).strip()
            question = f"What is {predicate_label} for {instance_name}?"
            plan.append(
                {
                    "predicate_concept_id": predicate,
                    "predicate_label": predicate_label,
                    "requirement_kind": "salient_relation",
                    "priority_class": "salient",
                    "question": question,
                    "status": "missing",
                }
            )
        for priority, item in enumerate(plan, start=1):
            item["priority"] = priority
        return plan

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
