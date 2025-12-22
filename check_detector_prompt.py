#!/usr/bin/env python
"""Check text relations for detector prompt concept."""

from src.backend.services.text_value_service import get_texts_for_concept

concept_id = '#V#missing_tool_call_detection_prompt'
texts = get_texts_for_concept(concept_id)

print(f'Texts for {concept_id}:')
if not texts:
    print('  (no texts found)')
else:
    for t in texts:
        predicate = t.get('predicate')
        lang = t.get('lang')
        text = t.get('text', '')
        print(f'  predicate={predicate}, lang={lang}, text_len={len(text)}')
        if len(text) < 200:
            print(f'    text: {repr(text)}')
        else:
            print(f'    text: {repr(text[:200])}...')
