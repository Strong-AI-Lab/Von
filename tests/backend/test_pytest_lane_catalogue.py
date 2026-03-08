from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from src.backend.utils import pytest_lane_catalogue
from src.backend.utils.pytest_lane_catalogue import (
    LANE_BACKEND_MCP,
    LANE_BACKEND_ROUTES,
    LANE_BACKEND_WORKFLOWS,
    MARKER_COST_HEAVY,
    MARKER_MANUAL_ONLY,
    MARKER_TRAIT_EXTERNAL_LIKE,
    MARKER_TRAIT_INTEGRATION_SURFACE,
    SUITE_BACKEND_HEAVY,
    SUITE_BACKEND_INTEGRATION,
    classify_test_path,
    get_git_changed_paths,
    recommend_for_changed_paths,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_classify_test_path_marks_mcp_and_external_like() -> None:
    classification = classify_test_path("tests/backend/test_internal_mcp_jira_tools.py")

    assert classification is not None
    assert classification.primary_suite_id == LANE_BACKEND_MCP
    assert "lane_backend_mcp" in classification.markers
    assert MARKER_COST_HEAVY in classification.markers
    assert MARKER_TRAIT_EXTERNAL_LIKE in classification.markers


def test_classify_test_path_marks_route_integration_surface() -> None:
    classification = classify_test_path(
        "tests/backend/test_von_generate_bare_arxiv_url_integration.py"
    )

    assert classification is not None
    assert classification.primary_suite_id == LANE_BACKEND_ROUTES
    assert "lane_backend_routes" in classification.markers
    assert MARKER_TRAIT_INTEGRATION_SURFACE in classification.markers
    assert MARKER_TRAIT_EXTERNAL_LIKE in classification.markers


def test_classify_test_path_marks_manual_suite() -> None:
    classification = classify_test_path("tests/manual/test_rag_indexing.py")

    assert classification is not None
    assert classification.primary_suite_id == "manual"
    assert MARKER_MANUAL_ONLY in classification.markers


def test_recommend_for_changed_paths_matches_internal_mcp_catalogue() -> None:
    recommendation = recommend_for_changed_paths(
        repo_root=REPO_ROOT,
        changed_paths=["src/backend/integrations/internal_mcp/catalogue.py"],
        risk="normal",
    )

    assert recommendation.primary_suites == (LANE_BACKEND_MCP,)
    assert "tests/backend/test_internal_mcp_catalogue_builds.py" in recommendation.direct_test_targets


def test_recommend_for_changed_paths_adds_high_risk_route_overlays() -> None:
    recommendation = recommend_for_changed_paths(
        repo_root=REPO_ROOT,
        changed_paths=["src/backend/server/routes/von_routes.py"],
        risk="high",
    )

    assert LANE_BACKEND_ROUTES in recommendation.primary_suites
    assert SUITE_BACKEND_INTEGRATION in recommendation.overlay_suites
    assert SUITE_BACKEND_HEAVY in recommendation.overlay_suites


def test_get_git_changed_paths_includes_worktree_deltas(monkeypatch) -> None:
    outputs = iter(
        (
            "src/backend/workflows/workflow_gap_recovery_workflow.py\n",
            "tests/backend/test_pytest_lane_catalogue.py\n",
            "docs/engineering/pytest_lane_strategy.md\n",
            "scripts/pytest_lanes.py\n",
        )
    )

    def fake_run(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(stdout=next(outputs))

    monkeypatch.setattr(pytest_lane_catalogue.subprocess, "run", fake_run)

    changed_paths = get_git_changed_paths(REPO_ROOT)

    assert changed_paths == [
        "src/backend/workflows/workflow_gap_recovery_workflow.py",
        "tests/backend/test_pytest_lane_catalogue.py",
        "docs/engineering/pytest_lane_strategy.md",
        "scripts/pytest_lanes.py",
    ]


def test_pytest_lanes_cli_aggregate_plan_runs() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/pytest_lanes.py", "aggregate-plan"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "backend-core" in completed.stdout
    assert "backend-workflows" in completed.stdout
