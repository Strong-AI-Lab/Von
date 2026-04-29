# Vontology Control-Flow Review Memory

Operational memory for Codex review runs that look for Python code taking over
workflow, prompt, or Vontology authority.

These notes are not production behaviour, policy, prompts, workflow definitions,
or Vontology content. They are review aids only. Direct code evidence, current
Vontology/workflow artefacts, and live telemetry override these notes.

Do not store secrets, private user data, raw prompt payloads, OAuth material, or
private corpus content here.

Current files:

- `run_log.md` records concise run outcomes and evidence anchors.
- `detection_heuristics.md` records review heuristics that helped find drift.
- `confirmed_non_issues.md` records inspected candidates that should not be
  repeatedly filed without new evidence.
- `ownership_boundaries.md` records recurring support-vs-authority boundaries.
- `jira_index.md` records relevant Jira epics/tasks so future scans can update
  existing issues instead of creating duplicates.
