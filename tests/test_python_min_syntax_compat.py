from pathlib import Path

from scripts.check_python_min_syntax import (
    REPO_ROOT,
    check_python_files,
    list_tracked_python_files,
    minimum_feature_version_from_pyproject,
)


def test_minimum_feature_version_comes_from_pyproject() -> None:
    assert minimum_feature_version_from_pyproject(REPO_ROOT) == (3, 11)


def test_guard_catches_exception_syntax_newer_than_python_311(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad_exception_syntax.py"
    bad_file.write_text(
        "try:\n"
        "    pass\n"
        "except json.JSONDecodeError, ValueError:\n"
        "    pass\n",
        encoding="utf-8",
    )

    errors = check_python_files([bad_file], feature_version=(3, 11))

    assert len(errors) == 1
    assert "except" in errors[0].message


def test_tracked_python_files_parse_with_minimum_supported_grammar() -> None:
    errors = check_python_files(
        list_tracked_python_files(REPO_ROOT),
        feature_version=minimum_feature_version_from_pyproject(REPO_ROOT),
    )

    assert errors == []
