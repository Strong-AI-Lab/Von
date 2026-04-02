These files are repo-side seed bundles, not authoritative workflow sources.

Authority rules:
- Canonical workflow logic, template logic, routing metadata, and prompt logic belong in Vontology-native artefacts.
- These files may be used only to seed missing Vontology state, support deterministic migration/bootstrap flows, or provide reviewable fixture material.
- Do not add new production workflow authority here. If a workflow capability can be represented in Vontology/VWL, implement that representation first and keep repo files seed-only.
- Family-specific startup services such as `paper_representation_workflow_vontology_service.py` may load these bundles only to publish or repair missing Vontology materialisation and surface drift diagnostics. Live request routing and workflow execution must not depend on reading workflow authority from these files.
- When a seeded workflow family changes in Vontology, refresh the repo snapshot explicitly instead of hand-editing the JSON. Example:
  `pdm run python scripts/workflow_repo_seed_bundle_export.py export --asset-path src/backend/workflows/repo_seed_bundles/paper_representation_workflow_seed_bundle.json`
- To check whether a bundle is stale without rewriting it, use:
  `pdm run python scripts/workflow_repo_seed_bundle_export.py diff --asset-path src/backend/workflows/repo_seed_bundles/paper_representation_workflow_seed_bundle.json`
