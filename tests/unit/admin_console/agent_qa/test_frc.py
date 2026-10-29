from copy import deepcopy

from apps.admin_console.services.agent_qa.report import build_report, render_html
from apps.admin_console.services.agent_qa.session import load_session
from tests.support.agent_qa_fixtures import write_session

CALIBRATION_IDS = (
    "rc33",
    "che-2081-fixed",
    "tinos-current",
    "guide-removed",
    "primary-action-unlabelled",
    "developer-labels-injected",
    "demo-five-taps-deep",
    "unchanged-control",
)
CALIBRATION_LABELS = {
    "rc33": "no",
    "che-2081-fixed": "yes",
    "tinos-current": "yes",
    "guide-removed": "no",
    "primary-action-unlabelled": "no",
    "developer-labels-injected": "no",
    "demo-five-taps-deep": "no",
    "unchanged-control": "yes",
}


def _first_run_fixture():
    evidence = {
        "C1": "Welcome to Sample.",
        "C2": "Explore native pools.",
        "C3": "Browse native selectors.",
    }
    return {
        "persona": "first-time user, no developer knowledge, opened the app from a tester invite",
        "fresh_install": True,
        "theme": "light",
        "screens": [
            {
                "id": "Home",
                "text": ["Welcome to Sample.", "Explore native pools.", "Open pager."],
                "screenshot": "light/1-after.png",
            },
            {
                "id": "NativePool",
                "text": ["Browse native selectors."],
                "screenshot": "light/2-after.png",
            },
            {
                "id": "Pager",
                "text": ["Swipe to explore pages."],
                "screenshot": "light/4-after.png",
            },
        ],
        "samples": {
            criterion: [
                {"judge_id": f"judge-{index + 1}", "passed": True, "evidence": quote}
                for index in range(5)
            ]
            for criterion, quote in evidence.items()
        },
        "reachability": {
            "Home": [],
            "NativePool": [
                {
                    "from": "Home",
                    "to": "NativePool",
                    "label": "Explore native pools.",
                    "visible": True,
                }
            ],
            "Pager": [{"from": "Home", "to": "Pager", "label": "Open pager.", "visible": True}],
        },
    }


def _set_judgements(first_run, criterion, passed, evidence):
    first_run["samples"][criterion] = [
        {"judge_id": f"judge-{index + 1}", "passed": passed, "evidence": evidence}
        for index in range(5)
    ]


def _calibration_fixture(tinos_label="yes"):
    builds = []
    for build_id in CALIBRATION_IDS:
        first_run = deepcopy(_first_run_fixture())
        expected = CALIBRATION_LABELS[build_id] or tinos_label
        if build_id == "rc33":
            _set_judgements(first_run, "C1", False, "Welcome to Sample.")
        elif build_id == "guide-removed":
            first_run["screens"][0]["text"][0] = "Sample"
            _set_judgements(first_run, "C1", False, "Sample")
        elif build_id == "primary-action-unlabelled":
            first_run["screens"][0]["text"][1] = "Continue"
            _set_judgements(first_run, "C2", False, "Continue")
        elif build_id == "developer-labels-injected":
            first_run["screens"][1]["text"].append("rc33")
            _set_judgements(first_run, "C3", False, "rc33")
        elif build_id == "demo-five-taps-deep":
            first_run["reachability"]["Pager"] = [
                {"from": "Home", "to": "Pager", "label": "Open pager.", "visible": False},
                {"from": "Home", "to": "Route1", "label": "Open route 1.", "visible": True},
                {"from": "Route1", "to": "Route2", "label": "Open route 2.", "visible": True},
                {"from": "Route2", "to": "Route3", "label": "Open route 3.", "visible": True},
                {"from": "Route3", "to": "Route4", "label": "Open route 4.", "visible": True},
                {"from": "Route4", "to": "Pager", "label": "Open pager.", "visible": True},
            ]
        builds.append({"id": build_id, "expected": expected, "first_run": first_run})
    return {
        "builds": builds,
        "repeat_gate_results": {
            "rc33": ["red", "red", "red"],
            "che-2081-fixed": ["green", "green", "green"],
        },
    }


def test_frc_flag_is_off_by_default_and_keeps_legacy_report(tmp_path, monkeypatch):
    monkeypatch.delenv("ARTEMIS_SMARTQA_FRC", raising=False)
    with_fixture = write_session(tmp_path / "fixture", first_run=_first_run_fixture())
    without_fixture = write_session(tmp_path / "legacy")

    report = build_report(load_session(with_fixture))
    legacy_report = build_report(load_session(without_fixture))

    assert report == legacy_report


def test_enabled_frc_is_advisory_in_report_html_and_does_not_change_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_SMARTQA_FRC", "on")
    root = write_session(tmp_path, first_run=_first_run_fixture())

    report = build_report(load_session(root))

    assert report["green"] is True
    assert report["first_run_comprehension"]["verdict"] == "yes"
    assert report["first_run_comprehension"]["advisory"] is True
    assert report["first_run_comprehension"]["criteria"]["C4"]["taps"]["Pager"] == 1
    assert report["first_run_comprehension"]["criteria"]["C1"]["evidence"][0]["quote"] == (
        "Welcome to Sample."
    )
    assert "First-run comprehension (advisory)" in render_html(report)


def test_frc_uses_five_sample_agreement_and_marks_uncertain_criteria(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_SMARTQA_FRC", "on")
    first_run = _first_run_fixture()
    first_run["samples"]["C2"] = [
        {"judge_id": f"judge-{index + 1}", "passed": index < 3, "evidence": "Explore native pools."}
        for index in range(5)
    ]
    root = write_session(tmp_path, first_run=first_run)

    report = build_report(load_session(root))

    criterion = report["first_run_comprehension"]["criteria"]["C2"]
    assert criterion["verdict"] == "judge_uncertain"
    assert criterion["passed_samples"] == 3
    assert criterion["total_samples"] == 5
    assert report["first_run_comprehension"]["verdict"] == "no"
    assert report["green"] is True


def test_frc_duplicate_judges_do_not_count_as_independent_samples(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_SMARTQA_FRC", "on")
    first_run = _first_run_fixture()
    first_run["samples"]["C1"] = [
        {**sample, "judge_id": "same-judge"} for sample in first_run["samples"]["C1"]
    ]
    root = write_session(tmp_path, first_run=first_run)

    report = build_report(load_session(root))

    criterion = report["first_run_comprehension"]["criteria"]["C1"]
    assert criterion["verdict"] == "judge_uncertain"
    assert criterion["passed_samples"] == 0


def test_frc_accepts_four_of_five_agreeing_judges(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_SMARTQA_FRC", "on")
    first_run = _first_run_fixture()
    first_run["samples"]["C1"][-1]["passed"] = False
    root = write_session(tmp_path, first_run=first_run)

    report = build_report(load_session(root))

    criterion = report["first_run_comprehension"]["criteria"]["C1"]
    assert criterion["verdict"] == "yes"
    assert criterion["passed_samples"] == 4
    assert criterion["total_samples"] == 5


def test_frc_reachability_uses_visible_labels_and_three_tap_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_SMARTQA_FRC", "on")
    first_run = _first_run_fixture()
    first_run["reachability"] = {
        "NativePool": [
            {"from": "Home", "to": "NativePool", "label": "Explore native pools.", "visible": True}
        ],
        "Pager": [
            {"from": "Home", "to": "Pager", "label": "Open pager.", "visible": False},
            {"from": "Home", "to": "Route1", "label": "Open route 1.", "visible": True},
            {"from": "Route1", "to": "Route2", "label": "Open route 2.", "visible": True},
            {"from": "Route2", "to": "Route3", "label": "Open route 3.", "visible": True},
            {"from": "Route3", "to": "Pager", "label": "Open pager.", "visible": True},
        ],
    }
    root = write_session(tmp_path, first_run=first_run)

    report = build_report(load_session(root))

    reachability = report["first_run_comprehension"]["criteria"]["C4"]
    assert reachability["verdict"] == "no"
    assert reachability["taps"]["Pager"] == 4


def test_frc_calibration_requires_eight_matches_and_stable_gate_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_SMARTQA_FRC", "on")
    first_run = _first_run_fixture()
    first_run["calibration"] = _calibration_fixture()
    root = write_session(tmp_path, first_run=first_run)

    report = build_report(load_session(root))

    calibration = report["first_run_comprehension"]["calibration"]
    assert calibration["passed"] is True
    assert calibration["matched_builds"] == 8
    assert calibration["total_builds"] == 8
    assert calibration["stable_gate_runs"] is True
    assert len(calibration["build_results"]) == 8
    assert "Fixture calibration: pass" in render_html(report)
    assert report["green"] is True


def test_frc_calibration_fails_on_label_mismatch_or_unstable_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_SMARTQA_FRC", "on")
    first_run = _first_run_fixture()
    calibration_input = _calibration_fixture()
    _set_judgements(calibration_input["builds"][0]["first_run"], "C1", True, "Welcome to Sample.")
    calibration_input["repeat_gate_results"]["rc33"] = ["red", "green", "red"]
    first_run["calibration"] = calibration_input
    root = write_session(tmp_path, first_run=first_run)

    report = build_report(load_session(root))

    calibration = report["first_run_comprehension"]["calibration"]
    assert calibration["passed"] is False
    assert calibration["matched_builds"] == 7
    assert calibration["stable_gate_runs"] is False
    assert report["green"] is True


def test_frc_calibration_waits_for_tinos_label(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_SMARTQA_FRC", "on")
    first_run = _first_run_fixture()
    calibration_input = _calibration_fixture()
    calibration_input["builds"][2]["expected"] = None
    first_run["calibration"] = calibration_input
    root = write_session(tmp_path, first_run=first_run)

    report = build_report(load_session(root))

    assert report["first_run_comprehension"]["calibration"]["passed"] is False
