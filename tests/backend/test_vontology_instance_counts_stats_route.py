from __future__ import annotations

from flask import Flask

from src.backend.server.routes.vontology_routes import vontology_bp


def _create_client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test-secret"
    app.register_blueprint(vontology_bp, url_prefix="/vontology/api/vontology")
    return app.test_client()


def test_instance_counts_route_uses_status_aware_stats_payload(monkeypatch):
    client = _create_client()

    def _fake_stats(*_args, **_kwargs):
        return {
            "stats_status": "available",
            "stats_generated_at": "2026-02-10T08:00:00Z",
            "concept_stats": {
                "#V#animal": {
                    "kind": "type",
                    "stats_status": "available",
                    "stats_generated_at": "2026-02-10T08:00:00Z",
                    "direct_instance_count": 2,
                    "total_instance_count_in_subtree": 5,
                    "has_any_instances_in_subtree": True,
                    "direct_subtype_count": 1,
                    "total_subtype_count_in_subtree": 3,
                }
            },
        }

    monkeypatch.setattr(
        "src.backend.services.vontology_concept_stats_service.get_vontology_concept_stats",
        _fake_stats,
    )

    resp = client.get("/vontology/api/vontology/instance_counts?ids=%23V%23animal")
    assert resp.status_code == 200

    payload = resp.get_json()
    row = payload["instance_counts"]["#V#animal"]
    assert row["direct"] == 2
    assert row["indirect"] == 3
    assert row["total"] == 5
    assert row["descendant_count"] == 4
    assert row["stats_status"] == "available"


def test_instance_counts_route_fast_fail_when_stats_stale(monkeypatch):
    client = _create_client()

    def _fake_stats(*_args, **_kwargs):
        return {
            "stats_status": "stale",
            "stats_generated_at": "2026-02-10T08:00:00Z",
            "concept_stats": {
                "#V#animal": {
                    "kind": "type",
                    "stats_status": "stale",
                    "stats_generated_at": "2026-02-10T08:00:00Z",
                    "stats_unavailable": True,
                    "stats_reason": "cache_stale",
                }
            },
        }

    monkeypatch.setattr(
        "src.backend.services.vontology_concept_stats_service.get_vontology_concept_stats",
        _fake_stats,
    )

    resp = client.get(
        "/vontology/api/vontology/instance_counts?ids=%23V%23animal&rebuild=0"
    )
    assert resp.status_code == 200

    payload = resp.get_json()
    row = payload["instance_counts"]["#V#animal"]
    assert row["stats_status"] == "stale"
    assert row["stats_reason"] == "cache_stale"
    assert row["direct"] is None
    assert row["indirect"] is None
    assert row["total"] is None
