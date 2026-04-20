You are interpreting one uploaded file copy into candidate entity representations.

Original filename:
{original_filename}

Content type:
{content_type}

Interpretation summary JSON:
{interpretation_json}

Extracted text:
{extracted_text}

Task:
- Decide independently whether the file supports a person representation candidate.
- Decide independently whether the file supports a company representation candidate.
- Decide independently whether the file supports a meeting representation candidate.
- Use only information grounded in the supplied filename, interpretation summary, and extracted text.
- Preserve names, URLs, dates, and short evidence snippets close to the source wording instead of inventing normalised replacements.
- Leave fields empty and set `applicable` to `false` when the source does not support that entity type clearly enough.

Return strict JSON only with this shape:
{
  "schema_version": "file_copy_entity_representation_interpretation.v1",
  "person_candidate": {
    "applicable": false,
    "representation_mode": null,
    "person_name": "",
    "emails": [],
    "phone_numbers": [],
    "affiliations": [],
    "roles": [],
    "evidence_fragments": []
  },
  "company_candidate": {
    "applicable": false,
    "representation_mode": null,
    "company_name": "",
    "aliases": [],
    "urls": [],
    "descriptors": [],
    "evidence_fragments": []
  },
  "meeting_candidate": {
    "applicable": false,
    "representation_mode": null,
    "meeting_name": "",
    "datetime_candidates": [],
    "participants": [],
    "outcomes": [],
    "evidence_fragments": []
  }
}

Rules:
- Use `representation_mode` only when the source supports one, such as `cv`, `business_card`, `web_page`, `meeting`, `calendar`, or `transcript`.
- Do not invent people, companies, meetings, URLs, dates, or participants.
- Keep arrays short and grounded. Prefer the strongest few items rather than exhaustive weak guesses.
- Use empty strings, empty arrays, and `null` for unsupported fields.
- Do not add markdown fences, commentary, confidence values, or extra keys.
