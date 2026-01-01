#!/usr/bin/env python
"""Utility script: check the missing-tool-call detector prompt concept.

This is NOT a pytest test module.

Usage (PowerShell):
    pdm run python utilities/check_detector_prompt_concept.py
"""

from src.backend.services.concept_service import get_concept_by_concept_id
from src.backend.services.text_value_service import get_texts_for_concept


def main() -> int:
    action = get_concept_by_concept_id("#V#detect_missing_tool_call_action")
    print("✅ Detector action concept found")

    rels = action.get("relationships", {}) if isinstance(action, dict) else {}
    print(f"   Relationships: {list(rels.keys())}")

    uses_prompt_rels = rels.get("#V#uses_prompt", [])
    prompt_id = uses_prompt_rels[0] if uses_prompt_rels else None
    print(f"   Uses prompt: {prompt_id}")

    if not prompt_id:
        print("❌ Detector action has no uses_prompt relationship")
        return 1

    prompt_texts = get_texts_for_concept(prompt_id)
    content_texts = [t for t in prompt_texts if t.get("predicate") == "hasContent"]
    if not content_texts:
        available_predicates = {t.get("predicate") for t in prompt_texts}
        print("❌ Detector prompt has no hasContent text")
        print(f"   Available predicates: {available_predicates}")
        return 1

    text = content_texts[0].get("text")
    text_len = len(text) if isinstance(text, str) else 0
    print("✅ Detector prompt text found")
    print(f"   Length: {text_len} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
