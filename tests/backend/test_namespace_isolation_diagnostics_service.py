import pytest

from src.backend.services import namespace_isolation_diagnostics_service as diagnostics


@pytest.fixture(autouse=True)
def _reset_namespace_isolation_state():
    diagnostics.reset_namespace_isolation_diagnostics()
    yield
    diagnostics.reset_namespace_isolation_diagnostics()


def test_record_namespace_context_observation_counts_mismatch_and_missing_components():
    emitted = diagnostics.record_namespace_context_observation(
        flow="test.generate",
        namespace=None,
        namespace_source="missing",
        user_concept_id=None,
        organisation_concept_id=None,
        mismatch_detected=True,
        details={"stage": "unit_test"},
    )

    assert emitted == 3

    snapshot = diagnostics.get_namespace_isolation_diagnostics_snapshot(
        include_recent_events=True
    )
    counters = snapshot["counters"]
    assert counters["events_total"] == 3
    assert counters["namespace_mismatch_events"] == 1
    assert counters["missing_component_events"] == 2
    assert counters["missing_namespace_events"] == 1
    assert counters["org_scope_downgrade_events"] == 0

    event_type_counts = snapshot["event_type_counts"]
    assert event_type_counts["namespace_mismatch"] == 1
    assert event_type_counts["missing_component.namespace"] == 1
    assert event_type_counts["missing_component.user_concept_id"] == 1


def test_record_namespace_context_observation_tracks_org_scope_events():
    diagnostics.record_namespace_context_observation(
        flow="test.rag",
        namespace="#V#user@org",
        namespace_source="request.namespace",
        user_concept_id="#V#user",
        organisation_concept_id=None,
        mismatch_detected=False,
    )
    diagnostics.record_namespace_context_observation(
        flow="test.rag",
        namespace="#V#user",
        namespace_source="request.namespace",
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        mismatch_detected=False,
    )

    snapshot = diagnostics.get_namespace_isolation_diagnostics_snapshot()
    counters = snapshot["counters"]
    assert counters["events_total"] == 2
    assert counters["missing_component_events"] == 1
    assert counters["org_scope_downgrade_events"] == 1

    event_type_counts = snapshot["event_type_counts"]
    assert event_type_counts["missing_component.organisation_concept_id"] == 1
    assert event_type_counts["org_scope_downgrade"] == 1


def test_namespace_isolation_snapshot_respects_recent_event_limit():
    diagnostics.record_namespace_isolation_event(
        flow="flow_a",
        event_type="namespace_mismatch",
        namespace="#V#a@b",
    )
    diagnostics.record_namespace_isolation_event(
        flow="flow_b",
        event_type="missing_component.namespace",
        namespace=None,
    )
    diagnostics.record_namespace_isolation_event(
        flow="flow_c",
        event_type="org_scope_downgrade",
        namespace="#V#a",
    )

    snapshot = diagnostics.get_namespace_isolation_diagnostics_snapshot(
        include_recent_events=True,
        recent_limit=2,
    )

    recent_events = snapshot["recent_events"]
    assert len(recent_events) == 2
    assert [event["flow"] for event in recent_events] == ["flow_b", "flow_c"]
