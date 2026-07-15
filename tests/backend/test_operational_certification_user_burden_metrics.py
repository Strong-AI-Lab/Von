from src.backend.services.operational_certification_contract_service import (
    aggregate_user_burden_metrics,
)


def _authenticated_metrics(*, follow_up_count: int) -> dict[str, object]:
    return {
        "follow_up_request_count": follow_up_count,
        "follow_up_request_count_applicable": True,
        "follow_up_request_count_complete": True,
        "follow_up_request_count_evidence_kind": "completion_gate_proxy",
        "clarification_count": follow_up_count,
        "clarification_count_applicable": True,
        "clarification_count_complete": True,
        "clarification_count_evidence_kind": "follow_up_request_proxy",
        "correction_count": None,
        "correction_count_applicable": True,
        "correction_count_complete": False,
        "correction_count_evidence_kind": "missing",
    }


def test_user_burden_aggregate_preserves_complete_proxy_provenance() -> None:
    aggregate = aggregate_user_burden_metrics(
        [
            _authenticated_metrics(follow_up_count=1),
            _authenticated_metrics(follow_up_count=0),
        ]
    )

    assert aggregate["follow_up_request_count"] == 1
    assert aggregate["follow_up_request_count_complete"] is True
    assert aggregate["follow_up_request_count_evidence_kind"] == (
        "completion_gate_proxy"
    )
    assert aggregate["clarification_count"] == 1
    assert aggregate["correction_count"] is None
    assert aggregate["correction_count_complete"] is False
    measurements = aggregate["measurements"]
    assert measurements["follow_up_request_count"] == {
        "status": "complete",
        "applicable": True,
        "complete": True,
        "evidence_kind": "completion_gate_proxy",
        "evidence_kinds": ["completion_gate_proxy"],
        "applicable_observation_count": 2,
        "complete_observation_count": 2,
        "missing_or_incomplete_observation_count": 0,
        "invalid_value_observation_count": 0,
    }
    assert measurements["clarification_count"]["evidence_kind"] == (
        "follow_up_request_proxy"
    )
    assert measurements["correction_count"]["status"] == "incomplete"
    assert measurements["correction_count"]["complete"] is False


def test_user_burden_aggregate_does_not_treat_unproven_legacy_zero_as_measured() -> (
    None
):
    aggregate = aggregate_user_burden_metrics(
        [{"clarification_count": 0, "correction_count": 0}]
    )

    assert aggregate["follow_up_request_count"] is None
    assert aggregate["clarification_count"] is None
    assert aggregate["correction_count"] is None
    assert all(
        measurement["status"] == "incomplete"
        for measurement in aggregate["measurements"].values()
    )


def test_user_burden_aggregate_keeps_durable_measurements_not_applicable() -> None:
    durable_metrics = {
        "follow_up_request_count": None,
        "follow_up_request_count_applicable": False,
        "follow_up_request_count_complete": True,
        "follow_up_request_count_evidence_kind": "not_applicable",
        "clarification_count": None,
        "clarification_count_applicable": False,
        "clarification_count_complete": True,
        "clarification_count_evidence_kind": "not_applicable",
        "correction_count": None,
        "correction_count_applicable": False,
        "correction_count_complete": True,
        "correction_count_evidence_kind": "not_applicable",
    }

    aggregate = aggregate_user_burden_metrics([durable_metrics, durable_metrics])

    assert aggregate["follow_up_request_count"] is None
    assert aggregate["clarification_count"] is None
    assert aggregate["correction_count"] is None
    assert all(
        measurement["status"] == "not_applicable"
        and measurement["complete"] is True
        and measurement["applicable"] is False
        for measurement in aggregate["measurements"].values()
    )
