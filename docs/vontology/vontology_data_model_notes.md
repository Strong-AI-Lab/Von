# Vontology Data Model Notes

## Text values are not concept fields

Many human-facing strings (names, descriptions, prompt content, notes) are stored as **text relations** (via `text_value_service.py`) rather than as top-level fields on concept documents.

Practical implications:

- **Do not assume** a concept will have a `content` or `description` field, even if the concept “obviously” has text in the UI.
- Prefer retrieving text via `get_texts_for_concept(concept_id)` and filtering by predicate (e.g. `hasName`, `hasDescription`, `hasContent`).
- When writing/updating text, use `upsert_text_for_concept(...)` with the appropriate predicate rather than mutating concept document fields.

### Case study (JVNAUTOSCI-797 follow-up)

User-specific system prompts were linked correctly as concepts (`#V#von_llm_prompt` instances related via `#V#specific_to_von_user`), but the prompt body lived in a `hasContent` text relation.

Code that expected `concept["content"]` silently treated the prompt as missing. The fix was to fall back to text relations when `content` is absent.
