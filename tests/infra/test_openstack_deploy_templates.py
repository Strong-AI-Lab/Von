from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TEMPLATE = (
    REPO_ROOT / "infra/openstack/templates/scripts/deploy_von_release.sh.tftpl"
)
CLOUD_INIT_TEMPLATE = (
    REPO_ROOT / "infra/openstack/templates/cloud-init/von_bootstrap.yaml.tftpl"
)
WORKFLOW_FILE = REPO_ROOT / ".github/workflows/openstack-deploy.yml"


def test_deploy_template_includes_health_gate_and_rollback_paths() -> None:
    content = DEPLOY_TEMPLATE.read_text(encoding="utf-8")
    assert "wait_for_health" in content
    assert "rollback_release" in content
    assert "healthcheck_failed_rollback_succeeded" in content
    assert "healthcheck_failed_rollback_failed" in content


def test_deploy_template_supports_artifact_and_audit_logging() -> None:
    content = DEPLOY_TEMPLATE.read_text(encoding="utf-8")
    assert "--artifact-path" in content
    assert "DEPLOY_AUDIT_LOG_PATH" in content
    assert "JSONL audit event" in content


def test_cloud_init_bootstrap_executes_non_interactive_bootstrap_deploy() -> None:
    content = CLOUD_INIT_TEMPLATE.read_text(encoding="utf-8")
    assert "/usr/local/bin/deploy_von_release.sh" in content
    assert "--bootstrap" in content


def test_workflow_contains_ci_gates_and_manual_deploy_trigger() -> None:
    content = WORKFLOW_FILE.read_text(encoding="utf-8")
    assert "workflow_dispatch" in content
    assert "CI Gates and Artefact" in content
    assert "Deploy to OpenStack host" in content
