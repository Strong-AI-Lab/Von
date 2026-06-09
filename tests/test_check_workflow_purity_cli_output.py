from __future__ import annotations

from scripts.check_workflow_purity import _print_regression_summary


def test_quiet_on_pass_hides_counter_summary(capsys) -> None:
    _print_regression_summary(
        {
            "summary_text": "Workflow purity: built_in_registration_count=0",
            "baseline": {"comparison": {"regression_detected": False}},
        },
        quiet_on_pass=True,
    )

    assert capsys.readouterr().out == "Workflow purity gate passed.\n"


def test_quiet_on_pass_keeps_failure_details(capsys) -> None:
    _print_regression_summary(
        {
            "summary_text": "Workflow purity: built_in_registration_count=1",
            "baseline": {
                "comparison": {
                    "regression_detected": True,
                    "increased_counters": {
                        "built_in_registration_count": {
                            "baseline": 0,
                            "current": 1,
                            "delta": 1,
                        }
                    },
                }
            },
        },
        quiet_on_pass=True,
    )

    output = capsys.readouterr().out
    assert "Workflow purity: built_in_registration_count=1" in output
    assert "Regressed counters:" in output
    assert "Workflow purity gate failed." in output
