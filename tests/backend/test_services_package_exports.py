from __future__ import annotations

import importlib
import sys


def test_services_package_exports_are_lazy() -> None:
    for module_name in list(sys.modules):
        if module_name == "src.backend.services" or module_name.startswith(
            "src.backend.services."
        ):
            sys.modules.pop(module_name, None)

    services_pkg = importlib.import_module("src.backend.services")

    assert "src.backend.services.concept_service" not in sys.modules
    assert "src.backend.services.testing_workflow_vontology_service" not in sys.modules

    concept_service = services_pkg.concept_service
    testing_workflow_vontology_service = services_pkg.testing_workflow_vontology_service

    assert concept_service.__name__ == "src.backend.services.concept_service"
    assert (
        testing_workflow_vontology_service.__name__
        == "src.backend.services.testing_workflow_vontology_service"
    )
