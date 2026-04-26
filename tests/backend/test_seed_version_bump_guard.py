"""Tests for the repo seed-bundle version-bump guard helper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.backend.workflows.repo_seed_bundle_version_guard import (
    WORKFLOW_BUNDLE_SCHEMA,
    check_seed_version_bumps,
)


def _write_bundle(path: Path, *, seed_version: object, family_id: str = "fam") -> str:
    payload = {
        "family_id": family_id,
        "schema_version": WORKFLOW_BUNDLE_SCHEMA,
        "seed_version": seed_version,
        "workflows": [],
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    path.write_text(text, encoding="utf-8")
    return text


def _resolver(mapping: dict[Path, str | None]):
    def _resolve(path: Path) -> str | None:
        return mapping.get(path)

    return _resolve


def test_unchanged_bundle_passes(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    text = _write_bundle(bundle, seed_version="2")

    issues = check_seed_version_bumps(
        [bundle],
        repo_root=tmp_path,
        baseline_resolver=_resolver({bundle: text}),
    )

    assert issues == []


def test_brand_new_bundle_passes(tmp_path: Path) -> None:
    bundle = tmp_path / "new_bundle.json"
    _write_bundle(bundle, seed_version="1")

    issues = check_seed_version_bumps(
        [bundle],
        repo_root=tmp_path,
        baseline_resolver=_resolver({bundle: None}),
    )

    assert issues == []


def test_changed_content_without_bump_fails(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    baseline_text = _write_bundle(bundle, seed_version="2", family_id="old")
    # Mutate content but keep seed_version the same.
    _write_bundle(bundle, seed_version="2", family_id="new")

    issues = check_seed_version_bumps(
        [bundle],
        repo_root=tmp_path,
        baseline_resolver=_resolver({bundle: baseline_text}),
    )

    assert len(issues) == 1
    assert "seed_version did not increase" in issues[0]
    assert "bundle.json" in issues[0]


def test_strictly_increasing_seed_version_passes(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    baseline_text = _write_bundle(bundle, seed_version="2", family_id="old")
    _write_bundle(bundle, seed_version="3", family_id="new")

    issues = check_seed_version_bumps(
        [bundle],
        repo_root=tmp_path,
        baseline_resolver=_resolver({bundle: baseline_text}),
    )

    assert issues == []


def test_decreasing_seed_version_fails(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    baseline_text = _write_bundle(bundle, seed_version="5", family_id="old")
    _write_bundle(bundle, seed_version="4", family_id="new")

    issues = check_seed_version_bumps(
        [bundle],
        repo_root=tmp_path,
        baseline_resolver=_resolver({bundle: baseline_text}),
    )

    assert len(issues) == 1
    assert "did not increase" in issues[0]


def test_missing_seed_version_on_changed_bundle_fails(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    baseline_text = _write_bundle(bundle, seed_version="2", family_id="old")
    bundle.write_text(
        json.dumps(
            {
                "family_id": "new",
                "schema_version": WORKFLOW_BUNDLE_SCHEMA,
                "workflows": [],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    issues = check_seed_version_bumps(
        [bundle],
        repo_root=tmp_path,
        baseline_resolver=_resolver({bundle: baseline_text}),
    )

    assert len(issues) == 1
    assert "seed_version is missing" in issues[0]


def test_non_workflow_schema_is_skipped(tmp_path: Path) -> None:
    bundle = tmp_path / "prompt_bundle.json"
    bundle.write_text(
        json.dumps({"schema_version": "workflow_authoring_prompt_seed_bundle.v1"}),
        encoding="utf-8",
    )

    issues = check_seed_version_bumps(
        [bundle],
        repo_root=tmp_path,
        baseline_resolver=_resolver({bundle: "{}"}),
    )

    assert issues == []


def test_deleted_bundle_is_skipped(tmp_path: Path) -> None:
    missing_path = tmp_path / "gone.json"

    issues = check_seed_version_bumps(
        [missing_path],
        repo_root=tmp_path,
        baseline_resolver=_resolver({missing_path: "{}"}),
    )

    assert issues == []


def test_baseline_without_parseable_version_accepts_any_integer(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    baseline_text = json.dumps(
        {
            "family_id": "old",
            "schema_version": WORKFLOW_BUNDLE_SCHEMA,
            "workflows": [],
        }
    )
    _write_bundle(bundle, seed_version="1", family_id="new")

    issues = check_seed_version_bumps(
        [bundle],
        repo_root=tmp_path,
        baseline_resolver=_resolver({bundle: baseline_text}),
    )

    assert issues == []


def test_real_repo_seed_bundles_pass_against_themselves() -> None:
    """Sanity check: every workflow seed bundle on disk is "unchanged" relative
    to itself, so the guard must report no issues. This guards against the
    helper itself misbehaving on real bundle shapes."""

    project_root = Path(__file__).resolve().parents[2]
    bundle_dir = project_root / "src/backend/workflows/repo_seed_bundles"
    bundles = sorted(bundle_dir.glob("*.json"))
    assert bundles, "expected at least one repo seed bundle"

    baseline_map: dict[Path, str | None] = {
        path: path.read_text(encoding="utf-8") for path in bundles
    }
    issues = check_seed_version_bumps(
        bundles,
        repo_root=project_root,
        baseline_resolver=_resolver(baseline_map),
    )
    assert issues == []
