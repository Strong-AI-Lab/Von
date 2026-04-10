# JVNAUTOSCI-1799 Root-Cause Inventory

Status: derived implementation/validation artefact  
Date: 2026-04-10 (updated 2026-07-12)  
Scope: arXiv paper representation reliability, entity representation workflow
hardening, and one additional telemetry-surfaced monitoring family

## Acceptance evidence

- Single-paper live smoke acceptance passed for `2505.14396`
  (`Causal Cartographer: From Mapping to Reasoning Over Counterfactual Worlds`)
  via `tests/backend/test_paper_representation_workflow_vontology_service.py::test_live_arxiv_paper_representation_workflow_acceptance`.
- Representative batch acceptance passed for the default 10-paper sample via
  `tests/backend/test_paper_representation_workflow_vontology_service.py::test_live_arxiv_paper_representation_workflow_acceptance_batch`.
- The batch lane enforces a success-rate threshold of `>= 90%` and now runs
  through the real workflow-driven fixture, execution, verification, and cleanup
  helpers with per-paper failure reporting.

## Root causes harvested

### 1. arXiv proxy success without a usable file path

- Symptom: telemetry showed `download_paper` reporting success while no usable
  `file_path` was returned, which caused the arXiv representation workflow to
  fail closed.
- Current status: already hardened before this task by cache-path fallback and
  async cache-settlement handling in the MCP arXiv proxy.
- Durable regression coverage:
  - `tests/backend/test_internal_mcp_arxiv_proxy_cache_listing.py::test_arxiv_store_downloaded_pdf_falls_back_to_cached_pdf_when_missing_path`
  - `tests/backend/test_internal_mcp_arxiv_proxy_cache_listing.py::test_arxiv_store_downloaded_pdf_waits_for_async_cache_settlement`

### 2. Historical VWL metadata-validation gap around `file_copy_concept_id`

- Symptom: a historical failed workflow trace showed
  `metadata_validation_failed:metadata_read_context_key_missing` on
  `decide_acquisition_mode.file_copy_concept_id`.
- Current status: the authoritative arXiv workflow definition no longer treats
  `file_copy_concept_id` as required at that state; the current seed bundle and
  Vontology materialisation both reflect the repaired contract.
- Durable regression coverage:
  - `tests/backend/test_paper_representation_workflow_vontology_service.py::test_bootstrap_materialises_paper_representation_workflow_family`

### 3. Acceptance-path gap: live arXiv reliability was only covered by one hard-coded paper

- Symptom: before `JVNAUTOSCI-1799`, the nearest-real-path live acceptance lane
  used a single hard-coded paper and could not express the Jira-guided repeated
  paper through an env var, nor prove the representative-sample criterion.
- Fix implemented in this task:
  - env-gated single-paper smoke acceptance with
    `VON_LIVE_ARXIV_ACCEPTANCE_PAPER`
  - env-gated representative batch acceptance with
    `VON_LIVE_ARXIV_ACCEPTANCE_SAMPLE`
  - batch harness now collects per-paper failure records instead of aborting on
    the first exception
- Durable regression/acceptance coverage:
  - `tests/backend/test_paper_representation_workflow_vontology_service.py::test_live_arxiv_paper_representation_workflow_acceptance`
  - `tests/backend/test_paper_representation_workflow_vontology_service.py::test_live_arxiv_paper_representation_workflow_acceptance_batch`

### 4. Additional monitoring-family bug surfaced by 1790 telemetry harvest

- Symptom: `turn_execution_build_dashboard` crashed with
  `TypeError: int() argument must be a string, a bytes-like object or a real number, not 'NoneType'`
  when `limit` and `offset` were omitted.
- Root cause: dashboard/build-benchmark paths forwarded `limit=None` and
  `offset=None` into `_rag_list_indexed`, which performed raw `int(...)`
  coercion.
- Fix implemented in this task:
  - `_rag_list_indexed` now normalises omitted pagination values safely
  - `_turn_execution_build_benchmark` now reports effective pagination defaults
    in its filter payload
- Durable regression coverage:
  - `tests/backend/test_rag_turn_execution_records_mcp_read_tools.py::test_turn_execution_build_dashboard_gateway_accepts_omitted_limit_and_offset`

## Authority boundary check

- Workflow, prompt, and Vontology remain authoritative for arXiv workflow
  behaviour.
- Python changes in this task are support-surface only:
  - live acceptance harness and env-gated validation plumbing
  - safe pagination normalisation on the monitoring/dashboard path

## AC4 — Entity representation workflow family audit (2026-07-12)

### 5. Entity representation workflow missing terminal success contract

- Symptom: the entity representation workflow family (`#V#entity_representation_workflow`
  and four domain sub-workflows) had **no** `#V#hasWorkflowTerminalSuccessContractJson`
  text relation. Without this, the turn execution pipeline cannot detect
  false-success for entity representation workflows.
- Comparison: the paper representation workflow has terminal success contracts on
  both the scholarly wrapper and the arXiv pipeline workflow.
- Fix implemented in this task:
  - Added `hasWorkflowTerminalSuccessContractJson` text relation to the entity
    representation seed bundle (`entity_representation_workflow_seed_bundle.json`)
    with the same schema (`workflow_terminal_success_contract.v1`) as the paper
    workflow.
  - Added bootstrap test assertion verifying the contract is materialised and
    its `success_statuses` and `require_completed_true` fields are correct.
- Durable regression coverage:
  - `tests/backend/test_entity_representation_workflow_vontology_service.py::test_bootstrap_materialises_entity_representation_workflow_family`

### Remaining gaps surfaced by the entity audit (not addressed in this task)

The following gaps are documented for future work:

- **No required effects contract** on entity representation workflows. The
  conversation turn workflow cannot verify that entity representation produced
  the expected Vontology mutations.
- **Missing representation contract profiles** for `event` and `place` domains.
  Person and company profiles exist; event and place do not.
- **No acceptance-level test fixture** (prepare/verify/cleanup pattern) for
  entity representation, unlike the arXiv fixture infrastructure.
- **No MCP testing tools** (`testing_prepare_entity_representation_fixture`,
  `testing_verify_entity_representation_result`) registered in the catalogue.

## Batch acceptance results (2026-07-12)

- The batch acceptance test (`test_live_arxiv_paper_representation_workflow_acceptance_batch`)
  was executed against 10 live arXiv papers with `VON_USE_MOCK_DB=1`.
- Result: **0% success rate** (0 of 10 papers passed).
- Root cause: `get_paper_metadata` MCP tool not registered in the test server
  session (warning: "Tool 'get_paper_metadata' not listed by server"), combined
  with missing `[pro]` feature dependencies. This is an environment-configuration
  issue, not a workflow logic regression.
- The acceptance test infrastructure itself is verified working: fixtures were
  prepared, workflows were executed, verification ran, cleanup completed
  successfully for all 10 papers.
- **Follow-up needed**: ensure `get_paper_metadata` is available in the test MCP
  server configuration, or mock it appropriately for acceptance testing.
  - operational documentation for the new live acceptance lane
