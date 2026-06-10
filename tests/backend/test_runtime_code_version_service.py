from __future__ import annotations

import pytest

from src.backend.services import runtime_code_version_service as version_service


_VERSION_ENV_VARS = (
    "VON_BUILD_VERSION",
    "VON_CODE_VERSION",
    "VON_BUILD_COMMIT",
    "VON_GIT_COMMIT",
    "VON_BUILD_BRANCH",
    "VON_GIT_BRANCH",
    "VON_BUILD_DIRTY",
    "VON_GIT_DIRTY",
)


@pytest.fixture(autouse=True)
def clear_version_cache(monkeypatch: pytest.MonkeyPatch):
    version_service.clear_runtime_code_version_cache()
    for key in _VERSION_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    yield
    version_service.clear_runtime_code_version_cache()


def test_git_version_leads_with_commit_timestamp(monkeypatch: pytest.MonkeyPatch):
    responses = {
        ("rev-parse", "HEAD"): "abc123def456abc123def456abc123def456abcd",
        ("rev-parse", "--short=12", "HEAD"): "abc123def456",
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("status", "--porcelain", "--untracked-files=no"): "",
        ("show", "-s", "--format=%cI", "HEAD"): "2026-06-10T08:37:05-04:00",
    }

    monkeypatch.setattr(
        version_service,
        "_run_git_command",
        lambda *args: responses.get(tuple(args)),
    )

    info = version_service.get_runtime_code_version_info()

    assert info["version"] == "v20260610_0837_backend+gabc123def456"
    assert info["version_base"] == "v20260610_0837_backend"
    assert info["source"] == "git"
    assert info["git_commit_timestamp"] == "2026-06-10T08:37:05-04:00"
    assert info["legacy_app_version"] == "v20250421_1015_backend"


def test_git_version_marks_dirty_tree(monkeypatch: pytest.MonkeyPatch):
    responses = {
        ("rev-parse", "HEAD"): "abc123def456abc123def456abc123def456abcd",
        ("rev-parse", "--short=12", "HEAD"): "abc123def456",
        ("rev-parse", "--abbrev-ref", "HEAD"): "feature/version",
        ("status", "--porcelain", "--untracked-files=no"): " M src/file.py",
        ("show", "-s", "--format=%cI", "HEAD"): "2026-06-10T12:37:05Z",
    }

    monkeypatch.setattr(
        version_service,
        "_run_git_command",
        lambda *args: responses.get(tuple(args)),
    )

    info = version_service.get_runtime_code_version_info()

    assert info["version"] == "v20260610_1237_backend+gabc123def456.dirty"
    assert info["git_dirty"] is True


def test_static_fallback_is_explicitly_unversioned(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(version_service, "_run_git_command", lambda *args: None)

    info = version_service.get_runtime_code_version_info()

    assert info["version"] == "v0_unversioned_backend"
    assert info["version_base"] == "v0_unversioned_backend"
    assert info["source"] == "static"
    assert info["legacy_app_version"] == "v20250421_1015_backend"


def test_env_version_still_takes_precedence(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_BUILD_VERSION", "v20260610_release+gabc123def456")
    monkeypatch.setenv("VON_BUILD_COMMIT", "abc123def456abc123def456")
    monkeypatch.setenv("VON_BUILD_BRANCH", "release")
    monkeypatch.setenv("VON_BUILD_DIRTY", "false")

    info = version_service.get_runtime_code_version_info()

    assert info["version"] == "v20260610_release+gabc123def456"
    assert info["version_base"] == "v20260610_release+gabc123def456"
    assert info["source"] == "env"
    assert info["git_short_commit"] == "abc123def456"
    assert info["git_dirty"] is False
