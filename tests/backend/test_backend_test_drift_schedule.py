from datetime import UTC, datetime, timedelta

from scripts.backend_test_drift_schedule import notification, should_run


def report(*failures):
    return {
        "full_run": True,
        "status": "new_failures" if failures else "no_new_failures",
        "pytest_exit_code": 1 if failures else 0,
        "observed_failures": list(failures),
        "new_failures": list(failures),
    }


def test_unchanged_completed_commit_skips_only_scheduled_runs():
    state = {"tested_sha": "abc", "report": report("failure")}
    assert not should_run("schedule", "abc", state)
    assert should_run("workflow_dispatch", "abc", state)
    assert should_run("schedule", "def", state)
    assert should_run("schedule", "abc", {})
    state["report"]["status"] = "run_broken"
    assert should_run("schedule", "abc", state)


def test_slice_does_not_establish_full_suite_completion():
    result = report("failure")
    result["full_run"] = False
    assert should_run("schedule", "abc", {"tested_sha": "abc", "report": result})


def test_changed_failure_identity_not_just_count_alerts_and_recovery_alerts():
    now = datetime.now(UTC)
    state = {"notified_report": report("old"), "last_notified_at": now.isoformat()}
    assert notification(report("new"), state, now) == "changed"
    assert notification(report(), state, now) == "changed"
    assert notification(report("old"), state, now) == ""


def test_weekly_reminder_can_use_reused_report_without_rerunning_suite():
    now = datetime.now(UTC)
    state = {
        "notified_report": report("old"),
        "last_notified_at": (now - timedelta(days=7)).isoformat(),
    }
    assert notification(report("old"), state, now) == "reminder"
    state["last_notified_at"] = (now - timedelta(days=6)).isoformat()
    assert notification(report("old"), state, now) == ""


def test_missing_notification_receipt_retries_and_broken_run_always_alerts():
    now = datetime.now(UTC)
    state = {"report": report("new")}
    assert notification(report("new"), state, now) == "changed"
    assert notification({"status": "run_broken"}, state, now) == "broken"
    assert notification(report(), {}, now) == ""


def test_state_restore_ignores_other_branches_and_expired_artifacts(monkeypatch):
    import io
    import json
    import zipfile
    from types import SimpleNamespace

    from scripts import backend_test_drift_schedule as mod

    archive = io.BytesIO()
    state = {
        "schema_version": "backend_drift_schedule.v1",
        "tested_sha": "abc",
        "report": report("old"),
    }
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(mod.STATE_FILE, json.dumps(state))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[-1].endswith("/zip"):
            return SimpleNamespace(stdout=archive.getvalue())
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "artifacts": [
                        {
                            "id": 3,
                            "expired": False,
                            "workflow_run": {"head_branch": "experiment"},
                        },
                        {
                            "id": 2,
                            "expired": True,
                            "workflow_run": {"head_branch": "main"},
                        },
                        {
                            "id": 1,
                            "expired": False,
                            "workflow_run": {"head_branch": "main"},
                        },
                    ]
                }
            ).encode()
        )

    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod.restore_state("owner/repo", "main") == state
    assert len(calls) == 2
    assert calls[-1][-1].endswith("/1/zip")


def test_unreadable_state_runs_again(monkeypatch):
    from scripts import backend_test_drift_schedule as mod

    def unavailable(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(mod.subprocess, "run", unavailable)
    assert mod.restore_state("owner/repo", "main") == {}


def test_finish_reuses_report_and_records_only_successful_notification(
    monkeypatch, tmp_path
):
    import json

    from scripts import backend_test_drift_schedule as mod

    state = {
        "tested_sha": "abc",
        "report": report("old"),
        "notified_report": report("old"),
        "last_notified_at": "2020-01-01T00:00:00+00:00",
        "report_run_url": "https://example.test/original",
    }
    (tmp_path / mod.STATE_FILE).write_text(json.dumps(state))
    monkeypatch.setattr(
        mod.sys, "argv", ["schedule", "finish", "--directory", str(tmp_path)]
    )
    for key, value in {
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "2",
        "GITHUB_SHA": "abc",
        "RAN_SUITE": "false",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }.items():
        monkeypatch.setenv(key, value)
    calls = []

    def not_sent(command, **kwargs):
        calls.append(command)

    monkeypatch.setattr(mod.subprocess, "run", not_sent)
    assert mod.main() == 0
    assert (
        json.loads((tmp_path / mod.STATE_FILE).read_text())["last_notified_at"]
        == state["last_notified_at"]
    )
    assert calls[0][2] == "reminder"

    def sent(command, **kwargs):
        from pathlib import Path

        Path(kwargs["env"]["NIGHTLY_NOTIFICATION_RECEIPT"]).write_text("sent")

    monkeypatch.setattr(mod.subprocess, "run", sent)
    assert mod.main() == 0
    saved = json.loads((tmp_path / mod.STATE_FILE).read_text())
    assert saved["last_notified_at"] != state["last_notified_at"]
    assert saved["tested_sha"] == "abc"


def test_broken_manual_rerun_invalidates_previous_skip_evidence(monkeypatch, tmp_path):
    import json

    from scripts import backend_test_drift_schedule as mod

    (tmp_path / mod.STATE_FILE).write_text(
        json.dumps({"tested_sha": "abc", "report": report("old")})
    )
    monkeypatch.setattr(
        mod.sys, "argv", ["schedule", "finish", "--directory", str(tmp_path)]
    )
    for key, value in {
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "2",
        "GITHUB_SHA": "abc",
        "RAN_SUITE": "true",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: None)
    assert mod.main() == 0
    saved = json.loads((tmp_path / mod.STATE_FILE).read_text())
    assert should_run("schedule", "abc", saved)
