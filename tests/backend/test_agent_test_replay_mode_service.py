from __future__ import annotations

from src.backend.services.agent_test_replay_mode_service import (
    resolve_agent_test_trusted_capability_names,
)


_TRUSTED_SCOPE = {
    "user_concept_id": "#V#user",
    "organisation_concept_id": "#V#org",
    "namespace": "#V#user@org",
}


def _trusted_capability_environ() -> dict[str, str]:
    return {
        "VON_AGENT_TEST_INSTANCE": "1",
        "VON_AGENT_TEST_TRUSTED_CAPABILITY_GRANTS": (
            "jira_update_issue,jira_get_issue,jira_get_issue"
        ),
        "VON_AGENT_TEST_TRUSTED_CAPABILITY_GRANT_USER": "#V#user",
        "VON_AGENT_TEST_TRUSTED_CAPABILITY_GRANT_ORGANISATION": "#V#org",
        "VON_AGENT_TEST_TRUSTED_CAPABILITY_GRANT_NAMESPACE": "#V#user@org",
    }


def test_agent_test_trusted_capabilities_require_exact_server_scope() -> None:
    assert resolve_agent_test_trusted_capability_names(
        **_TRUSTED_SCOPE,
        environ=_trusted_capability_environ(),
    ) == ("jira_get_issue", "jira_update_issue")

    mismatches = []
    for key in _TRUSTED_SCOPE:
        scope = dict(_TRUSTED_SCOPE)
        scope[key] = scope[key] + "_other"
        mismatches.append((scope, _trusted_capability_environ()))
    missing_scope = _trusted_capability_environ()
    missing_scope.pop("VON_AGENT_TEST_TRUSTED_CAPABILITY_GRANT_NAMESPACE")
    mismatches.append((dict(_TRUSTED_SCOPE), missing_scope))
    outside_agent_test = _trusted_capability_environ()
    outside_agent_test.pop("VON_AGENT_TEST_INSTANCE")
    mismatches.append((dict(_TRUSTED_SCOPE), outside_agent_test))
    malformed_names = _trusted_capability_environ()
    malformed_names["VON_AGENT_TEST_TRUSTED_CAPABILITY_GRANTS"] = (
        "jira_get_issue,not a capability"
    )
    mismatches.append((dict(_TRUSTED_SCOPE), malformed_names))

    for scope, environ in mismatches:
        assert resolve_agent_test_trusted_capability_names(
            **scope,
            environ=environ,
        ) == ()
