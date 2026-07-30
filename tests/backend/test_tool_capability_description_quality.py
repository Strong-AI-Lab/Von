from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
from src.backend.services.tool_metadata_service import (
    is_usable_tool_capability_description,
)


def test_every_registered_tool_has_a_usable_capability_description() -> None:
    catalogue = build_default_catalogue()
    failures = []
    for name in catalogue.list_methods():
        description = catalogue.get(name).description
        if not is_usable_tool_capability_description(
            description,
            tool_name=name,
        ):
            failures.append(name)

    assert failures == []
