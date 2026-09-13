import json
from datetime import datetime, timezone

import pytest

from scripts import codex_von_maintenance as maintenance


def test_attributed_record_and_eta_survive_backend_absence(tmp_path):
    config = {"public_status_root": str(tmp_path), "maintenance_agent": "Codex DGX",
              "maintenance_estimate_seconds": 180}
    receipt = {"requested_commit": "b" * 40}
    now = datetime(2026, 9, 12, 22, tzinfo=timezone.utc)
    planned = maintenance.publish(config, receipt, "planned", now=now)
    record = json.loads((tmp_path / "maintenance.json").read_text())
    assert record == planned["record"]
    assert record["agent"] == "Codex DGX"
    assert record["estimated_ready_at"] == "2026-09-12T22:03:00+00:00"
    assert "hostname" not in record
    ready = maintenance.publish(config, receipt, "ready", now=now)
    assert ready["record"]["estimated_ready_at"] is None
    assert ready["record"]["state"] == "ready"


def test_no_inferred_eta_and_no_unconfigured_publication(tmp_path):
    receipt = {"requested_commit": "b" * 40}
    assert maintenance.publish({}, receipt, "planned") == {"status": "not_configured"}
    result = maintenance.publish({"public_status_root": str(tmp_path),
                                  "maintenance_agent": "Codex DGX",
                                  "health_timeout_seconds": 960}, receipt, "planned")
    assert result["record"]["estimated_ready_at"] is None


def test_operator_identity_required_and_write_failure_is_explicit(tmp_path):
    receipt = {"requested_commit": "b" * 40}
    with pytest.raises(ValueError):
        maintenance.publish({"public_status_root": str(tmp_path)}, receipt, "planned")
    maintenance.record_publication({"public_status_root": str(tmp_path)}, receipt, "ready")
    assert receipt["maintenance_publication"]["status"] == "failed"
