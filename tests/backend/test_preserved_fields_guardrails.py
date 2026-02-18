from pathlib import Path
import re

from src.backend.services import text_value_service
from src.backend.vontology import utils_vontology


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_PATH_PATTERN = re.compile(r"concept_data\.preserved_fields")

# Canonical exceptions for migration/decommission and explicit runtime guardrails.
ALLOWED_BACKEND_LEGACY_PATHS = {
    "src/backend/services/concept_service.py",
    "src/backend/services/preserved_fields_decommission_service.py",
    "src/backend/server/routes/settings_routes.py",
    "src/backend/utilities/migrate_preserved_fields_to_text_relations.py",
}


def _repo_files(root: Path, pattern: str) -> list[Path]:
    return sorted(path for path in root.rglob(pattern) if path.is_file())


def _relative_path(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def test_frontend_runtime_has_no_preserved_fields_references() -> None:
    """JVNAUTOSCI-532: frontend must not read deprecated preserved_fields data."""
    frontend_root = PROJECT_ROOT / "src" / "frontend" / "web" / "von_interface" / "static" / "js"
    offenders: list[str] = []
    for path in _repo_files(frontend_root, "*.js"):
        if LEGACY_PATH_PATTERN.search(path.read_text(encoding="utf-8")):
            offenders.append(_relative_path(path))

    assert offenders == [], (
        "Deprecated frontend preserved_fields usage detected. "
        f"Offending files: {offenders}"
    )


def test_backend_legacy_preserved_fields_paths_are_explicitly_allowlisted() -> None:
    """Keep legacy path usage restricted to migration + guardrail code paths only."""
    backend_root = PROJECT_ROOT / "src" / "backend"
    offenders: list[str] = []
    for path in _repo_files(backend_root, "*.py"):
        rel = _relative_path(path)
        if not LEGACY_PATH_PATTERN.search(path.read_text(encoding="utf-8")):
            continue
        if rel not in ALLOWED_BACKEND_LEGACY_PATHS:
            offenders.append(rel)

    assert offenders == [], (
        "Unexpected runtime references to concept_data.preserved_fields found. "
        f"Add migration to text relations or update allowlist intentionally: {offenders}"
    )


def test_get_concept_description_prefers_relation_over_preserved_fields(
    monkeypatch,
) -> None:
    """Relation-backed hasDescription should win whenever both sources exist."""

    def _fake_preferred(*args, **kwargs):
        return {"text": "Canonical relation description"}

    monkeypatch.setattr(
        text_value_service,
        "get_preferred_text_for_concept",
        _fake_preferred,
    )

    concept = {
        "concept_id": "#V#description_precedence_demo",
        "description": "Legacy top-level description",
        "concept_data": {"preserved_fields": {"description": "Legacy preserved description"}},
    }
    assert (
        utils_vontology.get_concept_description(concept)
        == "Canonical relation description"
    )


def test_get_concept_description_ignores_preserved_fields_only_payload(
    monkeypatch,
) -> None:
    """Preserved-field-only content must not be treated as canonical description."""

    def _fake_preferred(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        text_value_service,
        "get_preferred_text_for_concept",
        _fake_preferred,
    )

    concept = {
        "concept_id": "#V#description_preserved_only_demo",
        "concept_data": {"preserved_fields": {"description": "Legacy preserved description"}},
    }
    assert utils_vontology.get_concept_description(concept) is None
