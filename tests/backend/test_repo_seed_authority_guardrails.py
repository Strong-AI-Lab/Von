from src.backend.workflows.workflow_purity_report import (
    REPO_SEED_AUTHORITY_ALLOWED_PATHS,
    build_workflow_purity_report,
)


def test_repo_seed_authority_is_restricted_to_seed_support_paths() -> None:
    report = build_workflow_purity_report(registry=None)
    repo_seed_drift = report["details"]["repo_seed_authority_drift"]
    offending_paths = repo_seed_drift["offending_paths"]

    assert offending_paths == [], (
        "Repo seed workflow bundles must remain seed-only rather than becoming "
        "production authority. Keep direct repo-seed references confined to the "
        "seed/bootstrap support paths and resolve authoritative workflow/template "
        "state from Vontology first. "
        f"Allowed paths: {sorted(REPO_SEED_AUTHORITY_ALLOWED_PATHS)}. "
        f"Offending paths: {offending_paths}"
    )


def test_vontology_first_seed_fallback_contracts_have_no_violations() -> None:
    report = build_workflow_purity_report(registry=None)
    contracts = report["details"]["vontology_first_seed_fallback_contracts"]
    violations = contracts["violations"]

    assert violations == [], (
        "Vontology-first workflow authority regressed: repo-seed fallback appears "
        "before authoritative Vontology resolution in at least one guarded path.\n"
        f"{violations}"
    )


def test_naming_bootstrap_does_not_admit_runtime_seed_imports(tmp_path) -> None:
    bootstrap = "src/backend/services/conversation_naming_workflow_vontology_service.py"
    runtime = "src/backend/services/conversation_naming_service.py"
    source = "from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle\n"
    for relative_path in (bootstrap, runtime):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    report = build_workflow_purity_report(
        registry=None, project_root=tmp_path, baseline_path=tmp_path / "baseline.json"
    )
    drift = report["details"]["repo_seed_authority_drift"]
    assert drift["offending_paths"] == [runtime]
    assert report["counters"]["repo_seed_authority_drift_path_count"] == 1
    assert any(
        item["path"] == bootstrap and item["allowed"] for item in drift["matches"]
    )
