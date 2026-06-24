from __future__ import annotations

from scripts import mongo_slow_log_summary


def test_parse_mongo_slow_and_command_failed_rows() -> None:
    slow = mongo_slow_log_summary.parse_slow_log_line(
        "WARNING: [mongo_slow] cmd=find db=von_db coll=text_values "
        "filter_shape=_id{$in} sort_shape= projection_shape= n_returned=10 "
        "duration_ms=1117.8 request_id=1602487374"
    )
    failed = mongo_slow_log_summary.parse_slow_log_line(
        "WARNING: [mongo_command_failed] cmd=find db=von_db coll=workflow_instances "
        "filter_shape=namespace,status sort_shape=created_at projection_shape= "
        "duration_ms=10002.7 request_id=431777653 failure_summary={}"
    )

    assert slow is not None
    assert slow.kind == "mongo_slow"
    assert slow.group_key[:3] == ("find", "von_db", "text_values")
    assert slow.n_returned == 10
    assert failed is not None
    assert failed.kind == "mongo_command_failed"
    assert failed.duration_ms == 10002.7


def test_build_slow_log_summary_aggregates_shapes_and_routes(tmp_path) -> None:
    log_path = tmp_path / "von.log"
    log_path.write_text(
        "\n".join(
            [
                "WARNING: [mongo_slow] cmd=find db=von_db coll=text_values "
                "filter_shape=_id{$in} sort_shape= projection_shape= n_returned=10 "
                "duration_ms=1000.0 request_id=1",
                "WARNING: [mongo_slow] cmd=find db=von_db coll=text_values "
                "filter_shape=_id{$in} sort_shape= projection_shape= n_returned=12 "
                "duration_ms=1500.0 request_id=2",
                "WARNING: [slow_request] GET settings.get_all_settings took "
                "4550.6ms (threshold=1000ms) status=200",
            ]
        ),
        encoding="utf-8",
    )

    report = mongo_slow_log_summary.build_slow_log_summary([log_path])

    assert report["schema_version"] == "mongo_slow_log_summary.v1"
    assert report["matched_line_count"] == 3
    mongo_row = report["mongo_shape_totals"][0]
    assert mongo_row["collection"] == "text_values"
    assert mongo_row["count"] == 2
    assert mongo_row["total_duration_ms"] == 2500.0
    assert mongo_row["max_n_returned"] == 12
    route_row = report["slow_route_totals"][0]
    assert route_row["endpoint"] == "settings.get_all_settings"


def test_render_markdown_contains_only_structural_columns(tmp_path) -> None:
    log_path = tmp_path / "von.log"
    log_path.write_text(
        "WARNING: [slow_request] GET von.history_sessions took "
        "2257.8ms (threshold=1000ms) status=200\n",
        encoding="utf-8",
    )
    report = mongo_slow_log_summary.build_slow_log_summary([log_path])

    markdown = mongo_slow_log_summary.render_markdown(report)

    assert "| Method | Endpoint | Status | Count | Total ms | Max ms |" in markdown
    assert "von.history_sessions" in markdown
