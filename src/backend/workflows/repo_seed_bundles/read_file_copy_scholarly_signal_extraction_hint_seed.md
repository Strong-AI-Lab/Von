You are extracting high-confidence scholarly representation signals from read_file_copy tool output for a represented scholarly paper file copy. Return only JSON.

The tool payload may include concept_id, original_filename, content_type, text, text_extraction, and text_extraction_error. Treat payload.text as the only source for paper claims. Use concept_id, original_filename, and source identifiers only as provenance cues.

Required JSON schema:
{
  "claims": [
    {
      "name": "short claim name under 90 characters",
      "description": "grounded statement of the claim, method, result, limitation, or dataset use",
      "evidence": "short excerpt or section cue from the paper content",
      "confidence": "high|medium|low"
    }
  ],
  "datasets": ["dataset names explicitly mentioned"],
  "baselines": ["baseline names explicitly mentioned"],
  "methods": ["method names explicitly mentioned"],
  "code_links": ["URLs explicitly present in the paper text"],
  "citation_seeds": ["paper or author references explicitly present and relevant"],
  "kb_concepts": [
    {
      "name": "short claim name under 90 characters",
      "kind": "instance",
      "description": "grounded statement suitable for a #V#claim instance",
      "notes": "evidence cue and source file copy concept id"
    }
  ]
}

Extraction policy:
- Create at most three kb_concepts, and only for high-confidence claims that are directly supported by payload.text.
- Keep kb_concepts compatible with create_concepts under #V#claim: each item needs name, kind=instance, description, and notes.
- Do not invent datasets, baselines, code links, citations, or claims.
- If payload.text is absent, empty, or unreadable, return empty arrays.
