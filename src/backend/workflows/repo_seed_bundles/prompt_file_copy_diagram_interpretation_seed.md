You are interpreting OCR and prose segments extracted from an uploaded file copy.

Segments:
{segments_json}

Allowed organisation names for relation focus (may be empty):
{allowed_organisation_names_json}

Task:
- Identify candidate organisation names explicitly visible in the provided segments.
- Identify candidate relations between those organisations when the segment text visibly suggests a link, arrow, association, or similar diagram relationship.
- Treat the result as human-review-required candidate interpretation, not as verified fact.
- Prefer exact surface forms from the segments, including mixed capitalisation or less-canonical names, rather than normalising them into a different name.
- Use only the provided segment IDs in `segment_refs`.
- If a candidate is supported by multiple segments, include all relevant `segment_refs`.
- If the evidence is weak, ambiguous, or not clearly organisation-like, omit it rather than guessing.
- If there are no reliable candidates, return empty lists.

Return strict JSON only with this shape:
{
  "schema_version": "file_copy_diagram_interpretation.v1",
  "organisation_candidates": [
    {
      "name": "exact organisation-like surface form",
      "segment_refs": ["segment id"],
      "evidence_excerpt": "brief supporting excerpt"
    }
  ],
  "relationship_candidates": [
    {
      "source_name": "exact source organisation surface form",
      "target_name": "exact target organisation surface form",
      "relation_hint": "directed_link | association | unknown",
      "segment_refs": ["segment id"],
      "evidence_excerpt": "brief supporting excerpt"
    }
  ]
}

Rules:
- Preserve organisation names exactly as they appear in the segments when practical.
- Do not invent unseen organisations, unseen relations, or numeric confidence values.
- Use `directed_link` only when the segment visibly implies directionality, such as an arrow.
- Use `association` for undirected or generic diagram links.
- Use `unknown` only when a relation seems present but the relation type is not clearer than that.
- Keep the output concise and bounded to at most {max_candidates} organisation candidates and {max_candidates} relation candidates.
