from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "migrate_jvnautosci_2591_failure_case_routing.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "migrate_jvnautosci_2591_failure_case_routing",
    _SCRIPT_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def test_rewrite_adds_failure_specific_gate_and_preserves_authority() -> None:
    original = {
        "schema_version": "workflow_discovery_exemplars.v1",
        "examples": ["Learn from that failed answer."],
        "keywords": ["failure case learning"],
        "routing_notes": ["Keep this existing note."],
    }

    rewritten, changed = mod.rewrite_failure_case_discovery_exemplars(original)

    assert changed is True
    assert rewritten["examples"] == original["examples"]
    assert rewritten["keywords"] == original["keywords"]
    assert rewritten["routing_notes"][0] == "Keep this existing note."
    assert set(mod.REQUIRED_QUERY_CUES).issubset(
        rewritten["required_query_cues"]
    )


def test_rewrite_is_idempotent() -> None:
    first, changed = mod.rewrite_failure_case_discovery_exemplars({})
    second, changed_again = mod.rewrite_failure_case_discovery_exemplars(first)

    assert changed is True
    assert changed_again is False
    assert second == first
