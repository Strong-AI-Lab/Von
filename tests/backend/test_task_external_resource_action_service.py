"""Provider-neutral task external-resource action service coverage."""

from __future__ import annotations

from types import ModuleType

import pytest

from src.backend.services.task_external_resource_action_service import (
    TaskExternalResourceActionAccessError,
    TaskExternalResourceActionNotFoundError,
    TaskExternalResourceActionResolutionError,
    list_task_external_resource_actions,
    resolve_task_external_resource_action,
)


def _provider(
    name: str,
    actions: object,
    *,
    resolved_target: object = "https://example.test/resource",
) -> ModuleType:
    provider = ModuleType(name)

    def list_actions(_task):
        if isinstance(actions, BaseException):
            raise actions
        return actions

    def resolve_action(_task, _action_id):
        if isinstance(resolved_target, BaseException):
            raise resolved_target
        return resolved_target

    provider.list_actions = list_actions
    provider.resolve_action = resolve_action
    return provider


def _open_action(action_id: str, label: str) -> dict[str, str]:
    return {
        "action_id": action_id,
        "kind": "open_resource",
        "label": label,
    }


def test_list_actions_combines_multiple_provider_actions() -> None:
    actions = list_task_external_resource_actions(
        {"task_concept_id": "#V#task_1"},
        providers=(
            _provider("provider_one", [_open_action("one.open", "Open one")]),
            _provider("provider_two", [_open_action("two.open", "Open two")]),
        ),
    )

    assert actions == [
        _open_action("one.open", "Open one"),
        _open_action("two.open", "Open two"),
    ]


def test_list_actions_filters_invalid_and_duplicate_provider_actions() -> None:
    actions = list_task_external_resource_actions(
        {"task_concept_id": "#V#task_1"},
        providers=(
            _provider(
                "provider_one",
                [
                    _open_action("shared.open", "Open shared"),
                    {"action_id": "not valid", "kind": "open_resource", "label": "Bad"},
                    {
                        "action_id": "wrong.kind",
                        "kind": "submit",
                        "label": "Wrong kind",
                    },
                    {"action_id": "missing.label", "kind": "open_resource"},
                ],
            ),
            _provider("provider_two", [_open_action("shared.open", "Duplicate")]),
        ),
    )

    assert actions == [_open_action("shared.open", "Open shared")]


def test_list_actions_isolates_provider_listing_failure() -> None:
    actions = list_task_external_resource_actions(
        {"task_concept_id": "#V#task_1"},
        providers=(
            _provider("broken_provider", RuntimeError("unavailable")),
            _provider(
                "healthy_provider", [_open_action("healthy.open", "Open healthy")]
            ),
        ),
    )

    assert actions == [_open_action("healthy.open", "Open healthy")]


def test_resolve_action_accepts_https_target() -> None:
    target = resolve_task_external_resource_action(
        {"task_concept_id": "#V#task_1"},
        "provider.open",
        providers=(
            _provider(
                "provider",
                [_open_action("provider.open", "Open resource")],
                resolved_target="https://example.test/resources/one?view=full",
            ),
        ),
    )

    assert target == "https://example.test/resources/one?view=full"


@pytest.mark.parametrize(
    "target",
    [
        "http://example.test/resource",
        "ftp://example.test/resource",
        "https://user:password@example.test/resource",
    ],
)
def test_resolve_action_rejects_non_https_or_credentialed_targets(target: str) -> None:
    with pytest.raises(TaskExternalResourceActionResolutionError) as error:
        resolve_task_external_resource_action(
            {"task_concept_id": "#V#task_1"},
            "provider.open",
            providers=(
                _provider(
                    "provider",
                    [_open_action("provider.open", "Open resource")],
                    resolved_target=target,
                ),
            ),
        )

    assert error.value.reason_code == "external_resource_target_invalid"


def test_resolve_action_preserves_provider_access_error() -> None:
    access_error = TaskExternalResourceActionAccessError(
        reason_code="provider_access_denied",
        safe_message="You cannot open this resource.",
    )

    with pytest.raises(TaskExternalResourceActionAccessError) as error:
        resolve_task_external_resource_action(
            {"task_concept_id": "#V#task_1"},
            "provider.open",
            providers=(
                _provider(
                    "provider",
                    [_open_action("provider.open", "Open resource")],
                    resolved_target=access_error,
                ),
            ),
        )

    assert error.value is access_error


def test_resolve_action_reports_not_found_when_no_provider_advertises_it() -> None:
    with pytest.raises(TaskExternalResourceActionNotFoundError) as error:
        resolve_task_external_resource_action(
            {"task_concept_id": "#V#task_1"},
            "provider.missing",
            providers=(
                _provider("provider", [_open_action("provider.open", "Open resource")]),
            ),
        )

    assert error.value.reason_code == "external_resource_action_not_found"
