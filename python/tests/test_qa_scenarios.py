from __future__ import annotations

from qa_scenarios import run_scenario_pack


def test_scenario_pack_reports_every_contract_as_a_pass():
    report = run_scenario_pack()

    assert report["summary"] == {"passed": 5, "failed": 0, "total": 5}
    assert [scenario["id"] for scenario in report["scenarios"]] == [
        "routing-folder-tree",
        "evidence-todo",
        "todo-persistence",
        "pptx-location",
        "workspace-boundary",
    ]
    assert all(scenario["status"] == "pass" for scenario in report["scenarios"])
