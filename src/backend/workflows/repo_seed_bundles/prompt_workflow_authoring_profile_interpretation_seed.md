You are extracting structured workflow-authoring profile fields from source text.

Source text:
{source_text}

Task:
- Identify the PhD student or doctoral student name when the text supports one.
- Identify supervisor names when the text supports them.
- Identify the research topic or research area when the text supports it.
- Identify the institution when the text supports it.
- Preserve exact or near-exact surface forms from the source text instead of normalising them into different wording.
- Leave fields empty rather than guessing.

Return strict JSON only with this shape:
{
  "schema_version": "workflow_authoring_profile_interpretation.v1",
  "student_name": "",
  "supervisor_names": [],
  "research_topic": "",
  "institution": ""
}

Rules:
- Use an empty string for missing scalar fields.
- Use an empty array for `supervisor_names` when the text does not support any supervisors.
- Do not invent people, institutions, topics, or IDs.
- Do not add markdown fences, commentary, confidence values, or extra keys.
