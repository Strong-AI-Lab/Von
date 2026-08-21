from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "utilities" / "invoke_internal_mcp_tool.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "invoke_internal_mcp_tool_under_test",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plain_python_delegates_internal_mcp_helper_through_pdm():
    module = _load_module()
    captured: dict[str, object] = {}

    @dataclass
    class _Completed:
        returncode: int

    def fake_runner(args: Sequence[str], *, cwd: Path) -> _Completed:
        captured["args"] = list(args)
        captured["cwd"] = cwd
        return _Completed(returncode=7)

    returncode = module._maybe_delegate_via_pdm(
        ["jira_get_myself"],
        environ={},
        prefix=str(PROJECT_ROOT.parent / "foreign-python"),
        which=lambda name: "pdm.exe" if name == "pdm" else None,
        runner=fake_runner,
    )

    assert returncode == 7
    assert captured["args"] == [
        "pdm.exe",
        "run",
        "python",
        str(SCRIPT_PATH),
        "jira_get_myself",
    ]
    assert captured["cwd"] == module.PROJECT_ROOT


def test_internal_mcp_helper_does_not_delegate_inside_project_environment():
    module = _load_module()

    def fail_runner(args: Sequence[str], *, cwd: Path) -> object:  # pragma: no cover
        raise AssertionError(f"unexpected runner: {list(args)} {cwd}")

    returncode = module._maybe_delegate_via_pdm(
        ["jira_get_myself"],
        environ={"PDM_PROJECT_ROOT": str(PROJECT_ROOT)},
        prefix="C:/Python311",
        which=lambda name: "pdm.exe",
        runner=fail_runner,
    )

    assert returncode is None


def test_internal_mcp_helper_pdm_delegation_can_be_disabled():
    module = _load_module()

    def fail_runner(args: Sequence[str], *, cwd: Path) -> object:  # pragma: no cover
        raise AssertionError(f"unexpected runner: {list(args)} {cwd}")

    returncode = module._maybe_delegate_via_pdm(
        ["jira_get_myself"],
        environ={"VON_INTERNAL_MCP_TOOL_NO_PDM_DELEGATE": "1"},
        prefix="C:/Python311",
        which=lambda name: "pdm.exe",
        runner=fail_runner,
    )

    assert returncode is None


def test_internal_mcp_helper_applies_external_model_dotenv_override_keys():
    module = _load_module()
    captured: dict[str, object] = {}

    def fake_apply(keys: Sequence[str]) -> dict[str, str]:
        captured["keys"] = tuple(keys)
        return {"OPENAI_API_KEY": "sk-test"}

    applied = module._apply_internal_mcp_dotenv_overrides(fake_apply)

    assert applied == {"OPENAI_API_KEY": "sk-test"}
    keys = captured["keys"]
    assert isinstance(keys, tuple)
    assert "OPENAI_API_KEY" in keys
    assert "OPENAI_API_KEY_FILE" in keys
    assert "VON_DEFAULT_OPENAI_MODEL" in keys
    assert "GEMINI_API_KEY" in keys
    assert "GEMINI_API_KEY_FILE" in keys
    assert "GOOGLE_API_KEY" in keys
    assert "GOOGLE_API_KEY_FILE" in keys
    assert "VON_DEFAULT_GEMINI_MODEL" in keys
