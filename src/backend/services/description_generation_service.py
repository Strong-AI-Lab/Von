# src/backend/services/description_generation_service.py
"""Service for generating concept descriptions using LLM.

JIRA: JVNAUTOSCI-1044
Provides workflow-driven description generation for concepts that lack proper
hasDescription text relations. Supports both on-demand generation (via MCP tool)
and background enrichment.
"""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..prompt.annotation_prompt import AnnotationPromptBuilder
from .description_metadata_service import extract_inline_description_metadata

logger = logging.getLogger(__name__)


def _get_llm_client(*args, **kwargs):
    """Load LLM client lazily to avoid circular imports during module load."""
    from ..languagemodels.llm_interface import get_llm_client

    return get_llm_client(*args, **kwargs)


# Prompt concept for description generation
DESCRIPTION_PROMPT_CONCEPT_ID = "#V#generate_concept_description_prompt"
_PROMPT_CACHE: Dict[str, Any] = {"text": None, "ts": 0.0, "source_predicate": None}
_PROMPT_TTL = 300  # seconds

# Default prompt if Vontology concept not found
_DEFAULT_DESCRIPTION_PROMPT = """You are a knowledge engineer helping to document an ontology.

Given the following concept information, generate a clear, concise description (1-3 sentences) that:
1. Explains what the concept represents
2. Distinguishes it from similar concepts
3. Uses precise, encyclopaedic language

Concept name: {concept_name}
Concept ID: {concept_id}
Type hierarchy: {type_hierarchy}
Relationships: {relationships}

Respond with ONLY the description text, no preamble or explanation."""


class DescriptionGenerationService:
    """Service for generating concept descriptions using LLM.

    Supports:
    - On-demand generation via generate_description()
    - Batch enrichment via enrich_concepts_without_descriptions()
    - Placeholder detection via is_placeholder_description()
    """

    # Patterns that indicate a placeholder/auto-generated description
    PLACEHOLDER_PATTERNS = [
        r"^Description fallback",
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",  # Timestamp patterns
        r"^[a-z_]+\s*$",  # Just snake_case text
        r"^#V#",  # Starts with concept ID prefix
        r"^Unnamed\s*(concept|type|entity)?$",
        r"^No\s+description",
        r"^\s*$",  # Empty or whitespace only
    ]

    def __init__(self, llm_client: Optional[Any] = None):
        """Initialise service with optional pre-configured LLM client."""
        self._llm_client = llm_client
        self._llm_init_error: Optional[Exception] = None
        self._prompt_builder = AnnotationPromptBuilder(
            DESCRIPTION_PROMPT_CONCEPT_ID, ttl_sec=_PROMPT_TTL
        )

    @property
    def llm_client(self):
        """Lazy-load LLM client."""
        if self._llm_client is not None:
            return self._llm_client
        try:
            self._llm_client = _get_llm_client()
            return self._llm_client
        except Exception as e:
            self._llm_init_error = e
            raise RuntimeError(
                "LLM client unavailable for description generation"
            ) from e

    @classmethod
    def is_placeholder_description(cls, text: Optional[str]) -> bool:
        """Check if a description appears to be an auto-generated placeholder.

        Args:
            text: The description text to check

        Returns:
            True if the text matches placeholder patterns
        """
        if text is None:
            return True
        if not isinstance(text, str):
            return True

        stripped = text.strip()
        if not stripped:
            return True

        # Check against known placeholder patterns
        for pattern in cls.PLACEHOLDER_PATTERNS:
            if re.match(pattern, stripped, re.IGNORECASE):
                return True

        # Check if it's just a concept ID converted to title case
        # e.g., "Some Concept Name" from "#V#some_concept_name"
        if "_" not in stripped and stripped.istitle():
            # Could be a valid name, but check if it looks like a converted ID
            lower_underscored = stripped.lower().replace(" ", "_")
            if lower_underscored.startswith("v#") or len(stripped.split()) <= 2:
                # Likely a simple ID-derived placeholder if very short
                pass  # Don't flag short titles as placeholders

        # Check for extremely short descriptions (< 10 chars, likely placeholder)
        if len(stripped) < 10:
            return True

        return False

    def _get_prompt_template(self) -> str:
        """Get the description generation prompt from Vontology or use default."""
        now = time.time()
        if _PROMPT_CACHE["text"] and now - _PROMPT_CACHE["ts"] < _PROMPT_TTL:
            return _PROMPT_CACHE["text"]

        # Try Vontology-stored prompt
        try:
            instruction = self._prompt_builder.get_instruction()
            if instruction and "missing canonical relation" not in instruction.lower():
                _PROMPT_CACHE["text"] = instruction
                _PROMPT_CACHE["ts"] = now
                _PROMPT_CACHE["source_predicate"] = self._prompt_builder._cache.get(
                    "source_predicate"
                )
                return instruction
        except Exception as e:
            logger.debug(f"Could not load prompt from Vontology: {e}")

        # Use default
        _PROMPT_CACHE["text"] = _DEFAULT_DESCRIPTION_PROMPT
        _PROMPT_CACHE["ts"] = now
        _PROMPT_CACHE["source_predicate"] = "default"
        return _DEFAULT_DESCRIPTION_PROMPT

    def _build_context_for_concept(self, concept: Dict[str, Any]) -> Dict[str, str]:
        """Build context dictionary for prompt template from concept data.

        Args:
            concept: Concept document with relationships, names, etc.

        Returns:
            Dict with concept_name, concept_id, type_hierarchy, relationships
        """
        from ..services import concept_service

        concept_id = concept.get("concept_id", "")

        # Get display name
        names = concept.get("names", [])
        concept_name = concept.get("name", "")
        if names and isinstance(names, list):
            # Prefer NL names over CODE names
            for n in names:
                if isinstance(n, dict) and n.get("type") == "NL":
                    concept_name = n.get("name", concept_name)
                    break
            if not concept_name and names:
                first = names[0]
                concept_name = (
                    first.get("name", "") if isinstance(first, dict) else str(first)
                )

        if not concept_name:
            # Derive from concept_id as fallback
            concept_name = concept_id.replace("#V#", "").replace("_", " ").title()

        # Build type hierarchy
        relationships = concept.get("relationships", {}) or {}
        type_hierarchy_parts = []

        # is_a_type_of for types
        parents = relationships.get("is_a_type_of", [])
        if parents:
            type_hierarchy_parts.append(f"is a type of: {', '.join(parents)}")

        # is_an_instance_of for individuals
        instance_of = relationships.get("is_an_instance_of", [])
        if instance_of:
            type_hierarchy_parts.append(f"is an instance of: {', '.join(instance_of)}")

        type_hierarchy = (
            "; ".join(type_hierarchy_parts)
            if type_hierarchy_parts
            else "No type hierarchy specified"
        )

        # Format other relationships (limit to avoid overwhelming prompt)
        rel_parts = []
        excluded_keys = {
            "is_a_type_of",
            "is_an_instance_of",
            "has_instance",
            "has_subtype",
        }
        for key, values in relationships.items():
            if key in excluded_keys:
                continue
            if isinstance(values, list) and values:
                # Limit to first 5 values
                limited = values[:5]
                suffix = f"... (+{len(values) - 5} more)" if len(values) > 5 else ""
                rel_parts.append(f"{key}: {', '.join(str(v) for v in limited)}{suffix}")
            elif values:
                rel_parts.append(f"{key}: {values}")

        relationships_str = (
            "; ".join(rel_parts[:10]) if rel_parts else "No additional relationships"
        )

        return {
            "concept_name": concept_name,
            "concept_id": concept_id,
            "type_hierarchy": type_hierarchy,
            "relationships": relationships_str,
        }

    def generate_description(
        self,
        concept_id: str,
        *,
        force: bool = False,
        store: bool = True,
    ) -> Dict[str, Any]:
        """Generate a description for a concept using LLM.

        Args:
            concept_id: The concept to generate a description for
            force: If True, generate even if a description already exists
            store: If True, store the generated description as a hasDescription text relation

        Returns:
            Dict with:
                success: bool
                description: str (the generated text, or existing if not forced)
                was_generated: bool (True if newly generated)
                concept_id: str
                error: str (if success is False)
        """
        from ..services import concept_service
        from ..services.text_value_service import (
            get_texts_for_concept,
            upsert_text_for_concept,
        )

        result: Dict[str, Any] = {
            "success": False,
            "description": None,
            "was_generated": False,
            "concept_id": concept_id,
        }

        # Fetch concept
        try:
            concept = concept_service.get_concept_by_concept_id(concept_id)
            if not concept:
                result["error"] = f"Concept not found: {concept_id}"
                return result
        except Exception as e:
            result["error"] = f"Error fetching concept: {e}"
            return result

        # Check existing description
        try:
            existing = get_texts_for_concept(
                subject_concept_id=concept_id, predicate="hasDescription", limit=1
            )
            if existing and not force:
                existing_text = existing[0].get("text", "")
                if not self.is_placeholder_description(existing_text):
                    result["success"] = True
                    result["description"] = existing_text
                    result["was_generated"] = False
                    return result
        except Exception as e:
            logger.warning(f"Error checking existing description for {concept_id}: {e}")

        # Build prompt
        try:
            template = self._get_prompt_template()
            context = self._build_context_for_concept(concept)
            prompt = template.format(**context)
        except Exception as e:
            result["error"] = f"Error building prompt: {e}"
            return result

        # Call LLM
        try:
            llm = self.llm_client
            raw_response = llm.generate(prompt=prompt)
            description = (
                raw_response.strip()
                if isinstance(raw_response, str)
                else str(raw_response).strip()
            )

            # Validate response
            if not description or len(description) < 10:
                result["error"] = "LLM returned empty or too short description"
                return result

            # Clean up response (remove any preamble the LLM might have added)
            if description.startswith("Description:"):
                description = description[12:].strip()

        except Exception as e:
            result["error"] = f"LLM generation failed: {e}"
            logger.error(f"LLM generation failed for {concept_id}: {e}")
            return result

        # Store if requested
        if store:
            try:
                cleaned_description, extracted_metadata = (
                    extract_inline_description_metadata(description)
                )
                if cleaned_description:
                    description = cleaned_description
                now_iso = datetime.now(timezone.utc).isoformat()
                provenance_payload: Dict[str, Any] = {
                    "source": "llm_generation",
                    "service": "description_generation_service",
                    "timestamp": now_iso,
                }
                context_payload: Dict[str, Any] = {
                    "generated": True,
                    "jira": "JVNAUTOSCI-1044",
                }
                if extracted_metadata:
                    if isinstance(extracted_metadata.get("source"), str):
                        provenance_payload.setdefault(
                            "source_label", extracted_metadata["source"]
                        )
                    if isinstance(extracted_metadata.get("attribution"), str):
                        provenance_payload.setdefault(
                            "attribution", extracted_metadata["attribution"]
                        )
                    if isinstance(extracted_metadata.get("timestamp"), str):
                        provenance_payload.setdefault(
                            "upstream_timestamp", extracted_metadata["timestamp"]
                        )
                    if isinstance(extracted_metadata.get("parent"), str):
                        context_payload.setdefault(
                            "parent_concept_id", extracted_metadata["parent"]
                        )
                    confidence_score = extracted_metadata.get("confidence_score")
                    if isinstance(confidence_score, (int, float)):
                        context_payload.setdefault(
                            "confidence_score", float(confidence_score)
                        )
                    context_payload["inline_metadata_migrated"] = True

                upsert_text_for_concept(
                    subject_concept_id=concept_id,
                    predicate="hasDescription",
                    text=description,
                    lang="en-NZ",
                    provenance=provenance_payload,
                    context=context_payload,
                )
                logger.info(f"Stored generated description for {concept_id}")
            except Exception as e:
                logger.error(f"Failed to store description for {concept_id}: {e}")
                result["error"] = f"Description generated but storage failed: {e}"
                result["description"] = description
                result["was_generated"] = True
                return result

        result["success"] = True
        result["description"] = description
        result["was_generated"] = True
        return result

    def enrich_concepts_without_descriptions(
        self,
        *,
        limit: int = 10,
        kind_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Find and enrich concepts that lack proper descriptions.

        Args:
            limit: Maximum number of concepts to process
            kind_filter: Optionally filter by concept kind ('type', 'individual', 'predicate')

        Returns:
            Dict with:
                processed: int (number of concepts processed)
                succeeded: int (number successfully enriched)
                failed: int (number that failed)
                details: List of per-concept results
        """
        from ..db.repositories.concepts_repository import ConceptsRepository

        results = {
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "details": [],
        }

        # Build query for concepts without good descriptions
        query: Dict[str, Any] = {}
        if kind_filter:
            query["kind"] = kind_filter

        try:
            # Fetch concepts (we'll check descriptions individually)
            cursor = ConceptsRepository.find(
                query,
                {"concept_id": 1, "name": 1, "names": 1, "relationships": 1, "kind": 1},
            )
            concepts = list(cursor)[
                : limit * 2
            ]  # Fetch extra to account for filtered out
        except Exception as e:
            logger.error(f"Error querying concepts: {e}")
            results["error"] = str(e)
            return results

        # Process each concept
        from ..services.text_value_service import get_texts_for_concept

        for concept in concepts:
            if results["processed"] >= limit:
                break

            concept_id = concept.get("concept_id")
            if not concept_id:
                continue

            # Check if it needs a description
            try:
                existing = get_texts_for_concept(
                    subject_concept_id=concept_id, predicate="hasDescription", limit=1
                )
                if existing:
                    existing_text = existing[0].get("text", "")
                    if not self.is_placeholder_description(existing_text):
                        continue  # Has a good description, skip
            except Exception:
                pass

            # Generate description
            result = self.generate_description(concept_id, force=True, store=True)
            results["processed"] += 1
            results["details"].append(
                {
                    "concept_id": concept_id,
                    "success": result.get("success", False),
                    "error": result.get("error"),
                }
            )

            if result.get("success"):
                results["succeeded"] += 1
            else:
                results["failed"] += 1

        return results


# Module-level singleton instance
_service_instance: Optional[DescriptionGenerationService] = None


def get_description_generation_service() -> DescriptionGenerationService:
    """Get or create the singleton service instance."""
    global _service_instance
    if _service_instance is None:
        _service_instance = DescriptionGenerationService()
    return _service_instance


def generate_concept_description(
    concept_id: str,
    *,
    force: bool = False,
    store: bool = True,
) -> Dict[str, Any]:
    """Convenience function to generate a description for a concept.

    Args:
        concept_id: The concept to generate a description for
        force: If True, generate even if a description already exists
        store: If True, store the generated description

    Returns:
        Dict with success, description, was_generated, error fields
    """
    service = get_description_generation_service()
    return service.generate_description(concept_id, force=force, store=store)


def is_placeholder_description(text: Optional[str]) -> bool:
    """Check if text appears to be a placeholder description."""
    return DescriptionGenerationService.is_placeholder_description(text)
