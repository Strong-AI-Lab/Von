import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TEMPLATE = (
    REPO_ROOT / "infra/openstack/templates/scripts/deploy_von_release.sh.tftpl"
)
CLOUD_INIT_TEMPLATE = (
    REPO_ROOT / "infra/openstack/templates/cloud-init/von_bootstrap.yaml.tftpl"
)
WORKFLOW_FILE = REPO_ROOT / ".github/workflows/openstack-deploy.yml"
TERRAFORM_VERSIONS_FILE = REPO_ROOT / "infra/openstack/versions.tf"
OPENSTACK_README = REPO_ROOT / "infra/openstack/README.md"
MONITOR_TEMPLATE = (
    REPO_ROOT / "infra/openstack/templates/scripts/von_monitor_health.sh.tftpl"
)
BACKUP_TEMPLATE = (
    REPO_ROOT / "infra/openstack/templates/scripts/von_backup_snapshot.sh.tftpl"
)
RESTORE_DRILL_TEMPLATE = (
    REPO_ROOT / "infra/openstack/templates/scripts/von_restore_drill.sh.tftpl"
)
RUNBOOK_DOC = REPO_ROOT / "docs/engineering/openstack_operations_runbooks.md"
OPENSTACK_ENV_EXAMPLES = [
    REPO_ROOT / "infra/openstack/environments/dev/dev.tfvars.example",
    REPO_ROOT / "infra/openstack/environments/staging/staging.tfvars.example",
    REPO_ROOT / "infra/openstack/environments/prod/prod.tfvars.example",
]
OPENSTACK_MODULE_DIRS = [
    REPO_ROOT / "infra/openstack/modules/compute_instance",
    REPO_ROOT / "infra/openstack/modules/floating_ip",
    REPO_ROOT / "infra/openstack/modules/persistent_volume",
    REPO_ROOT / "infra/openstack/modules/security_group",
]
SCRIPT_TEMPLATES = [
    DEPLOY_TEMPLATE,
    MONITOR_TEMPLATE,
    BACKUP_TEMPLATE,
    RESTORE_DRILL_TEMPLATE,
    REPO_ROOT / "infra/openstack/templates/scripts/von_ops_common.sh.tftpl",
]


def test_deploy_template_includes_health_gate_and_rollback_paths() -> None:
    content = DEPLOY_TEMPLATE.read_text(encoding="utf-8")
    assert "wait_for_health" in content
    assert "VON_BOOTSTRAP_HEALTHCHECK_ATTEMPTS:-120" in content
    assert "rollback_release" in content
    assert "healthcheck_failed_rollback_succeeded" in content
    assert "healthcheck_failed_rollback_failed" in content


def test_deploy_template_supports_artifact_and_audit_logging() -> None:
    content = DEPLOY_TEMPLATE.read_text(encoding="utf-8")
    assert "--artifact-path" in content
    assert "DEPLOY_AUDIT_LOG_PATH" in content
    assert "JSONL audit event" in content


def test_deploy_template_prepares_runtime_environment_for_artifact_releases() -> None:
    content = DEPLOY_TEMPLATE.read_text(encoding="utf-8")
    assert "prepare_release_environment()" in content
    assert 'prepare_release_environment "$NEW_RELEASE"' in content
    assert 'python3 -m venv "$release_path/.venv"' in content
    assert 'pip install -e "$release_path"' in content


def test_cloud_init_bootstrap_executes_non_interactive_bootstrap_deploy() -> None:
    content = CLOUD_INIT_TEMPLATE.read_text(encoding="utf-8")
    assert "/usr/local/bin/deploy_von_release.sh" in content
    assert "--bootstrap" in content
    assert "/usr/local/bin/von_monitor_health.sh" in content
    assert "/usr/local/bin/von_backup_snapshot.sh" in content
    assert "von-restore-drill.timer" in content
    assert "GOOGLE_OAUTH_STRICT_STARTUP" in content
    assert "GOOGLE_OAUTH_CLIENT_SECRET_FILE" in content
    assert "FLASK_SESSION_COOKIE_SECURE" in content
    assert "VON_DURABLE_WORKFLOWS_ENABLE=1" in content
    assert "VON_DURABLE_WORKFLOWS_BLOCKING_STARTUP=0" in content
    assert "VON_MONGO_STRICT_STARTUP" in content
    assert "VON_MONGO_STARTUP_PROBE" in content
    assert "VON_MONGO_REQUIRE_TLS" in content
    assert "MONGO_ALLOW_LOCAL_FALLBACK" in content
    assert "VON_MONGO_ALLOWED_HOST_SUFFIXES" in content
    assert "MONGO_URI_FILE" in content
    assert "OPENAI_API_KEY_FILE" in content
    assert "GEMINI_API_KEY_FILE" in content
    assert "META_API_KEY_FILE" in content
    assert "OPENROUTER_API_KEY_FILE" in content


def test_cloud_init_bootstrap_can_seed_scoped_llm_setting() -> None:
    content = CLOUD_INIT_TEMPLATE.read_text(encoding="utf-8")
    assert "scripts/bootstrap_llm_setting.py" in content
    assert "--provider '${default_llm_provider}'" in content
    assert "--model '${default_llm_model}'" in content
    assert "--organisation-concept-id" in content
    assert "--user-concept-id" in content
    assert "'${openai_api_key_file}'" in content
    assert "'${gemini_api_key_file}'" in content
    assert "'${meta_api_key_file}'" in content
    assert "'${openrouter_api_key_file}'" in content
    assert 'lower(default_llm_provider) == "gemini"' in content
    assert "VON_DEFAULT_GEMINI_MODEL" in content


def test_cloud_init_user_schema_uses_supported_service_account_fields() -> None:
    content = CLOUD_INIT_TEMPLATE.read_text(encoding="utf-8")
    assert "homedir: ${app_dir}" in content
    assert "home: ${app_dir}" not in content
    assert "create_home:" not in content


def test_cloud_init_secret_files_are_readable_by_service_group_only() -> None:
    content = CLOUD_INIT_TEMPLATE.read_text(encoding="utf-8")
    assert "install -d -m 0750 -o root -g ${service_group} /etc/von/secrets" in content
    assert "chown root:${service_group}" in content
    assert "chmod 0640" in content
    assert "owner: root:${service_group}" not in content
    assert re.search(r"(?m)^\s*owner: root:root\n\s*permissions: \"0600\"", content)


def test_cloud_init_template_indents_generated_file_content_blocks() -> None:
    content = CLOUD_INIT_TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"(?m)^\$\{indent\(6, [a-z_]+_content\)\}", content)
    assert "      ${indent(6, service_unit_content)}" in content


def test_workflow_contains_ci_gates_and_manual_deploy_trigger() -> None:
    content = WORKFLOW_FILE.read_text(encoding="utf-8")
    assert "workflow_dispatch" in content
    assert "CI Gates and Artefact" in content
    assert "Deploy to OpenStack host" in content


def test_terraform_versions_support_cross_variable_validation() -> None:
    workflow = WORKFLOW_FILE.read_text(encoding="utf-8")
    versions = TERRAFORM_VERSIONS_FILE.read_text(encoding="utf-8")
    readme = OPENSTACK_README.read_text(encoding="utf-8")

    workflow_match = re.search(r'terraform_version: "(\d+\.\d+\.\d+)"', workflow)
    required_match = re.search(r'required_version = ">= (\d+\.\d+\.\d+)"', versions)
    readme_match = re.search(r'Terraform `>= (\d+\.\d+\.\d+)`', readme)

    assert workflow_match is not None
    assert required_match is not None
    assert readme_match is not None

    workflow_version = tuple(map(int, workflow_match.group(1).split(".")))
    required_version = tuple(map(int, required_match.group(1).split(".")))
    documented_version = tuple(map(int, readme_match.group(1).split(".")))

    assert required_version >= (1, 9, 0)
    assert workflow_version >= required_version
    assert documented_version == required_version


def test_monitoring_template_covers_required_failure_modes() -> None:
    content = MONITOR_TEMPLATE.read_text(encoding="utf-8")
    assert "auth_failures" in content
    assert "db_failures" in content
    assert "tls_expiry" in content
    assert "HEALTHCHECK_URL" in content


def test_backup_and_restore_drill_templates_exist() -> None:
    assert BACKUP_TEMPLATE.exists()
    assert RESTORE_DRILL_TEMPLATE.exists()
    backup_content = BACKUP_TEMPLATE.read_text(encoding="utf-8")
    restore_content = RESTORE_DRILL_TEMPLATE.read_text(encoding="utf-8")
    assert "von-backup-" in backup_content
    assert "restore drill" in restore_content.lower()


def test_operations_runbook_doc_exists_with_incident_sections() -> None:
    assert RUNBOOK_DOC.exists()
    content = RUNBOOK_DOC.read_text(encoding="utf-8")
    assert "Service Outage" in content
    assert "Auth Failure" in content
    assert "DB Credential Rotation" in content
    assert "/admin/db/health?probe=rw" in content
    assert "Rebuild from IaC" in content


def test_bash_parameter_expansions_are_escaped_for_terraform_templates() -> None:
    terraform_vars = {
        "alert_webhook_url",
        "app_port",
        "audit_log_path",
        "backup_audit_log_path",
        "backup_directory",
        "backup_retention_days",
        "bootstrap_repo_ref",
        "bootstrap_repo_url",
        "central_log_audit_log_path",
        "central_log_directory",
        "current_symlink",
        "deploy_audit_log_path",
        "deploy_log_path",
        "enable_https",
        "env_file",
        "healthcheck_path",
        "log_collection_lookback_minutes",
        "monitor_auth_failure_threshold",
        "monitor_db_failure_threshold",
        "monitor_log_lookback_minutes",
        "monitoring_events_log_path",
        "release_root",
        "restore_drill_audit_log_path",
        "service_group",
        "service_name",
        "service_user",
        "tls_cert_path",
        "tls_expiry_warning_days",
    }
    allowed_interpolations = {f"${{{name}}}" for name in terraform_vars}

    for template in SCRIPT_TEMPLATES:
        content = template.read_text(encoding="utf-8")
        for match in re.finditer(r"(?<!\$)\$\{[^}]+\}", content):
            interpolation = match.group(0)
            assert interpolation in allowed_interpolations, (
                f"{template} contains unescaped Bash interpolation "
                f"{interpolation}; use $${{...}} for shell expansion."
            )


def test_openstack_modules_pin_provider_source() -> None:
    for module_dir in OPENSTACK_MODULE_DIRS:
        content = (module_dir / "versions.tf").read_text(encoding="utf-8")
        assert 'source  = "terraform-provider-openstack/openstack"' in content
        assert 'version = "~> 2.1"' in content


def test_openstack_stack_does_not_manage_provider_default_egress_rule() -> None:
    content = (REPO_ROOT / "infra/openstack/main.tf").read_text(encoding="utf-8")
    assert "managed_allowed_egress_cidrs" in content
    assert 'trimspace(cidr) != "0.0.0.0/0"' in content


def test_openstack_environment_examples_do_not_null_secret_runtime_inputs() -> None:
    secret_names = [
        "bootstrap_flask_secret_key",
        "bootstrap_google_oauth_client_id",
        "bootstrap_google_oauth_client_secret",
        "bootstrap_openai_api_key",
        "bootstrap_gemini_api_key",
        "bootstrap_meta_api_key",
        "bootstrap_openrouter_api_key",
        "bootstrap_mongo_uri",
    ]
    secret_null_assignment = re.compile(
        rf"(?m)^\s*({'|'.join(secret_names)})\s*=\s*null\b"
    )

    for example_path in OPENSTACK_ENV_EXAMPLES:
        content = example_path.read_text(encoding="utf-8")
        assert not secret_null_assignment.search(content), (
            f"{example_path} assigns a secret bootstrap variable to null; "
            "tfvars overrides TF_VAR_* runtime injection."
        )
