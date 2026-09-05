from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_services_package_exports_are_lazy() -> None:
    # A fresh interpreter observes lazy imports without evicting service modules
    # still referenced by other collected tests or their monkeypatch fixtures.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib
import sys

services_pkg = importlib.import_module("src.backend.services")
assert "src.backend.services.concept_service" not in sys.modules
assert "src.backend.services.testing_workflow_vontology_service" not in sys.modules
assert services_pkg.concept_service.__name__ == "src.backend.services.concept_service"
assert services_pkg.testing_workflow_vontology_service.__name__ == (
    "src.backend.services.testing_workflow_vontology_service"
)
""",
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
