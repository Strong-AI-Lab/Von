from __future__ import annotations

import ast
from pathlib import Path


def _get_von_main_dotenv_override_keys() -> set[str]:
    source_path = (
        Path(__file__).resolve().parents[2] / "src" / "workflows" / "von" / "main.py"
    )
    module = ast.parse(source_path.read_text(encoding="utf-8"))

    for node in module.body:
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        if (
            not isinstance(call.func, ast.Name)
            or call.func.id != "_apply_dotenv_overrides"
        ):
            continue
        if not call.args:
            continue
        key_set = call.args[0]
        if not isinstance(key_set, ast.Set):
            raise AssertionError(
                "_apply_dotenv_overrides keys are no longer a set literal"
            )
        keys: set[str] = set()
        for elt in key_set.elts:
            if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
                raise AssertionError("dotenv override keys must remain string literals")
            keys.add(elt.value)
        return keys

    raise AssertionError("Could not find _apply_dotenv_overrides(...) call in von main")


def test_von_main_overrides_durable_workflow_enable_from_dotenv() -> None:
    keys = _get_von_main_dotenv_override_keys()

    assert "VON_DURABLE_WORKFLOWS_ENABLE" in keys


def test_von_main_overrides_swift_openstack_env_keys_from_dotenv() -> None:
    keys = _get_von_main_dotenv_override_keys()

    expected = {
        "VON_BLOB_STORE_BACKEND",
        "VON_SWIFT_CONTAINER",
        "VON_SWIFT_PREFIX",
        "OS_CLOUD",
        "OS_CLIENT_CONFIG_FILE",
        "OS_AUTH_URL",
        "OS_REGION_NAME",
        "OS_APPLICATION_CREDENTIAL_ID",
        "OS_APPLICATION_CREDENTIAL_SECRET",
    }

    assert expected.issubset(keys)


def test_von_main_overrides_gemini_credentials_from_dotenv() -> None:
    keys = _get_von_main_dotenv_override_keys()

    assert {
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_FILE",
        "GEMINI_API_BACKUP_KEY",
        "GEMINI_API_BACKUP_KEY_FILE",
        "GOOGLE_API_KEY",
        "GOOGLE_API_KEY_FILE",
        "GOOGLE_API_BACKUP_KEY",
        "GOOGLE_API_BACKUP_KEY_FILE",
        "VON_DEFAULT_GEMINI_MODEL",
    }.issubset(keys)


def test_von_main_overrides_openai_backup_credentials_from_dotenv() -> None:
    keys = _get_von_main_dotenv_override_keys()

    assert {
        "OPENAI_API_BACKUP_KEY",
        "OPENAI_API_BACKUP_KEY_FILE",
    }.issubset(keys)
