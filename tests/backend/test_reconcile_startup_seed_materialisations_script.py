from __future__ import annotations

import json

from scripts import reconcile_startup_seed_materialisations as script


def test_release_reconciliation_runs_selected_families(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        script,
        "_RECONCILERS",
        {
            script.CONCEPT_SUMMARY_FIELDS: lambda: (
                calls.append("summary") or {"ready": True}
            ),
            script.PUBLICATION_SCOPE_PROFILES: lambda: (
                calls.append("publication") or {"ready": True}
            ),
        },
    )

    report = script.reconcile_startup_seed_materialisations([script.ALL_FAMILIES])

    assert report["success"] is True
    assert report["state"] == "ready"
    assert calls == ["summary", "publication"]


def test_release_reconciliation_returns_nonzero_when_a_family_is_unavailable(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        script,
        "_RECONCILERS",
        {
            script.CONCEPT_SUMMARY_FIELDS: lambda: {"ready": True},
            script.PUBLICATION_SCOPE_PROFILES: lambda: {
                "ready": False,
                "reason": "receipt_persist_failed",
            },
        },
    )

    exit_code = script.main(["--family", script.ALL_FAMILIES])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["state"] == "unavailable"


def test_release_reconciliation_emits_prefixed_machine_receipt(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        script,
        "_RECONCILERS",
        {script.CONCEPT_SUMMARY_FIELDS: lambda: {"ready": True}},
    )

    assert script.main(["--machine-readable"]) == 0
    output = capsys.readouterr().out.strip()

    assert output.startswith(script.MACHINE_RECEIPT_PREFIX)
    payload = json.loads(output.removeprefix(script.MACHINE_RECEIPT_PREFIX))
    assert payload["success"] is True
