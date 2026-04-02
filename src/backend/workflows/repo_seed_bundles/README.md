These files are repo-side seed bundles, not authoritative workflow sources.

Authority rules:
- Canonical workflow logic, template logic, routing metadata, and prompt logic belong in Vontology-native artefacts.
- These files may be used only to seed missing Vontology state, support deterministic migration/bootstrap flows, or provide reviewable fixture material.
- Do not add new production workflow authority here. If a workflow capability can be represented in Vontology/VWL, implement that representation first and keep repo files seed-only.
- Family-specific startup services such as `paper_representation_workflow_vontology_service.py` may load these bundles only to publish or repair missing Vontology materialisation and surface drift diagnostics. Live request routing and workflow execution must not depend on reading workflow authority from these files.
