"""Update the Vontology-stored user-specific prompt for Michael Witbrock.

This modifies the prompt concept `#V#general_von_chat_prompt_for_witbrock` by:
- deleting any existing hasContent/hasDescription text relations
- inserting a single hasContent text relation in en-NZ

This is intended to be run manually when iterating on assistant behaviour.
"""

from __future__ import annotations

import importlib
import os
import sys

# Allow running as a script from the repo root.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

bypass_access_control = importlib.import_module(
    "src.backend.security.access_control"
).bypass_access_control
_text_value_service = importlib.import_module("src.backend.services.text_value_service")
delete_text_relation = _text_value_service.delete_text_relation
delete_text_relation_by_predicate_and_text = (
    _text_value_service.delete_text_relation_by_predicate_and_text
)
get_texts_for_concept = _text_value_service.get_texts_for_concept
upsert_text_for_concept = _text_value_service.upsert_text_for_concept

PROMPT_CONCEPT_ID = "#V#general_von_chat_prompt_for_witbrock"
PROMPT_LANG = "en-NZ"

PROMPT_TEXT = """You are Von’s assistant for Michael Witbrock.

Operating mode
- Be decisive: when the user asks for an action and the goal is clear, do it end-to-end without asking for permission or pausing for confirmation.
- Ask a question only when a missing detail is truly blocking progress.
- Write all user-facing text in New Zealand English.

Tool use
- Prefer using available tools over speculation.
- When multiple tool calls are needed, batch them in a single assistant message as a JSON array of tool calls (in execution order).
- Keep batches small and purposeful (aim for up to 4 calls per batch).
- After tools run, provide a concise natural-language answer.
- Never output tool-call JSON as plain text; it must be an actual tool invocation.

Web extraction
- If a URL extract returns empty content or looks JS-rendered, use resilient extraction (search + alternate sources) rather than looping on the same failing extract.

Vontology hygiene
- Avoid duplicates: before creating a concept, search for an existing one by name and check likely synonyms.
- When creating or updating concepts, prefer canonical identifiers with the #V# prefix.
- When adding names, use en-NZ by default unless another language is explicitly requested.

Context discipline
- Keep context lean: do not paste large tool outputs back into the model; summarise and retain only what is needed to complete the task.
- Avoid repeating the same facts across turns.
"""


def main() -> int:
    prompt_text = PROMPT_TEXT.strip()
    if not prompt_text:
        raise ValueError("PROMPT_TEXT is empty")

    with bypass_access_control():
        existing = get_texts_for_concept(PROMPT_CONCEPT_ID, limit=200)
        existing_prompt_texts = [
            t
            for t in existing
            if t.get("predicate") in ("hasContent", "hasDescription")
        ]

        for t in existing_prompt_texts:
            relation_id = t.get("relation_id")
            if isinstance(relation_id, str) and relation_id.strip():
                delete_text_relation(PROMPT_CONCEPT_ID, relation_id)
                continue

            # Fallback for unexpected legacy rows missing relation_id.
            predicate = t.get("predicate")
            if predicate:  # Type guard: only delete if predicate exists
                delete_text_relation_by_predicate_and_text(
                    PROMPT_CONCEPT_ID,
                    predicate=predicate,
                    text=t.get("text") or "",
                    lang=t.get("lang") or "en",
                )

        res = upsert_text_for_concept(
            subject_concept_id=PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=prompt_text,
            lang=PROMPT_LANG,
            provenance={"source": "utilities/update_user_prompt_for_witbrock.py"},
        )

    print("Updated prompt concept.")
    print(f"concept_id={PROMPT_CONCEPT_ID}")
    print(f"predicate=hasContent lang={PROMPT_LANG}")
    print(
        f"relation_id={res.get('relation_id')} text_value_id={res.get('text_value_id')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
