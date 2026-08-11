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


def test_advisory_findings_are_named_without_calling_them_regressions(capsys) -> None:
    _print_regression_summary(
        {
            "summary_text": "Workflow purity: synthesized_launch_contract_count=2",
            "baseline": {
                "comparison": {
                    "regression_detected": False,
                    "advisory_findings_detected": True,
                    "blocking_increased_counters": {},
                    "advisory_increased_counters": {},
                    "advisory_observed_counters": {
                        "synthesized_launch_contract_count": {
                            "current": 2,
                            "reason": "nonzero_complete_registry_observation",
                        }
                    },
                    "blocking_missing_counter_keys": [],
                    "advisory_missing_counter_keys": [],
                }
            },
        }
    )

    output = capsys.readouterr().out
    assert "Advisory counter findings:" in output
    assert "synthesized_launch_contract_count: current=2" in output
    assert "Regressed counters:" not in output
    assert "Workflow purity gate passed with advisory findings." in output


def test_quiet_on_pass_still_surfaces_advisory_outcome(capsys) -> None:
    _print_regression_summary(
        {
            "summary_text": "Workflow purity advisory",
            "baseline": {
                "comparison": {
                    "regression_detected": False,
                    "advisory_findings_detected": True,
                }
            },
        },
        quiet_on_pass=True,
    )

    assert (
        capsys.readouterr().out
        == "Workflow purity gate passed with advisory findings.\n"
    )
