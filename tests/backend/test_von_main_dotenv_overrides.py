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
        if not isinstance(call.func, ast.Name) or call.func.id != "_apply_dotenv_overrides":
            continue
        if not call.args:
            continue
        key_set = call.args[0]
        if not isinstance(key_set, ast.Set):
            raise AssertionError("_apply_dotenv_overrides keys are no longer a set literal")
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
