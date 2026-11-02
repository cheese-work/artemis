from __future__ import annotations

from collections import deque
from typing import Any

PERSONA = "first-time user, no developer knowledge, opened the app from a tester invite"
CALIBRATION_LABELS = {
    "rc33": "no",
    "che-2081-fixed": "yes",
    "tinos-current": None,
    "guide-removed": "no",
    "primary-action-unlabelled": "no",
    "developer-labels-injected": "no",
    "demo-five-taps-deep": "no",
    "unchanged-control": "yes",
}
STABILITY_BUILDS = ("rc33", "che-2081-fixed")


def evaluate_first_run(
    first_run: dict[str, Any], declared_screens: tuple[dict[str, str], ...]
) -> dict[str, Any]:
    if not isinstance(first_run, dict):
        first_run = {}
    screens = first_run.get("screens", [])
    first_three = screens[:3] if isinstance(screens, list) else []
    valid_input = (
        first_run.get("fresh_install") is True
        and first_run.get("theme") == "light"
        and first_run.get("persona") == PERSONA
        and len(first_three) == 3
        and all(isinstance(screen, dict) and screen.get("screenshot") for screen in first_three)
    )
    if valid_input:
        first_screen_text = _screen_text(first_three[:1])
        first_three_text = _screen_text(first_three)
        samples = first_run.get("samples", {})
        criteria = {
            "C1": _judge_criterion(samples, "C1", first_screen_text),
            "C2": _judge_criterion(samples, "C2", first_screen_text),
            "C3": _judge_criterion(samples, "C3", first_three_text),
            "C4": _reachability(
                first_run.get("reachability", {}), first_three[0], declared_screens
            ),
        }
        unavailable_reason = None
    else:
        criteria = {
            criterion: _uncertain_criterion("first_run_fixture_incomplete")
            for criterion in ("C1", "C2", "C3")
        }
        criteria["C4"] = {
            "verdict": "no",
            "limit_taps": 3,
            "taps": {},
            "evidence": {},
            "reason": "first_run_fixture_incomplete",
        }
        unavailable_reason = "first_run_fixture_incomplete"

    result = {
        "verdict": "yes" if all(item["verdict"] == "yes" for item in criteria.values()) else "no",
        "advisory": True,
        "criteria": criteria,
        "unavailable_reason": unavailable_reason,
    }
    calibration = first_run.get("calibration")
    if calibration is not None:
        result["calibration"] = evaluate_calibration(calibration, declared_screens)
    return result


def _screen_text(screens: list[dict[str, Any]]) -> tuple[str, ...]:
    texts = []
    for screen in screens:
        screen_text = screen.get("text", [])
        if not isinstance(screen_text, list):
            screen_text = []
        for value in screen_text:
            if isinstance(value, str):
                texts.append(value)
        elements = screen.get("elements", [])
        if not isinstance(elements, list):
            elements = []
        for element in elements:
            if isinstance(element, dict) and isinstance(element.get("text"), str):
                texts.append(element["text"])
    return tuple(texts)


def _judge_criterion(
    samples_by_criterion: Any, criterion: str, visible_text: tuple[str, ...]
) -> dict[str, Any]:
    samples = (
        samples_by_criterion.get(criterion, []) if isinstance(samples_by_criterion, dict) else []
    )
    valid_samples = (
        isinstance(samples, list)
        and len(samples) == 5
        and all(
            isinstance(sample, dict)
            and isinstance(sample.get("judge_id"), str)
            and sample["judge_id"].strip()
            and type(sample.get("passed")) is bool
            and isinstance(sample.get("evidence"), str)
            and sample["evidence"].strip()
            and any(sample["evidence"] in text for text in visible_text)
            for sample in samples
        )
        and len({sample["judge_id"].strip() for sample in samples}) == 5
    )
    if not valid_samples:
        return _uncertain_criterion("five_independent_quoted_samples_required")

    passed_samples = sum(sample["passed"] for sample in samples)
    verdict = "yes" if passed_samples >= 4 else "no" if passed_samples <= 1 else "judge_uncertain"
    return {
        "verdict": verdict,
        "passed_samples": passed_samples,
        "total_samples": 5,
        "evidence": [
            {
                "judge_id": sample["judge_id"],
                "passed": sample["passed"],
                "quote": sample["evidence"],
            }
            for sample in samples
        ],
    }


def _uncertain_criterion(reason: str) -> dict[str, Any]:
    return {
        "verdict": "judge_uncertain",
        "passed_samples": 0,
        "total_samples": 5,
        "evidence": [],
        "reason": reason,
    }


def _reachability(
    reachability: Any,
    home_screen: dict[str, Any],
    declared_screens: tuple[dict[str, str], ...],
) -> dict[str, Any]:
    home = home_screen.get("id")
    declared_ids = [
        screen.get("id")
        for screen in declared_screens
        if isinstance(screen, dict) and isinstance(screen.get("id"), str) and screen.get("id")
    ]
    adjacency: dict[str, list[tuple[str, str]]] = {}
    if isinstance(reachability, dict):
        edge_lists = reachability.values()
    elif isinstance(reachability, list):
        edge_lists = (reachability,)
    else:
        edge_lists = ()
    for edges in edge_lists:
        if not isinstance(edges, list):
            continue
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source, target, label = edge.get("from"), edge.get("to"), edge.get("label")
            if (
                edge.get("visible") is True
                and isinstance(source, str)
                and isinstance(target, str)
                and isinstance(label, str)
                and label.strip()
            ):
                adjacency.setdefault(source, []).append((target, label))

    distances: dict[str, int] = {}
    paths: dict[str, list[str]] = {}
    if isinstance(home, str) and home:
        distances[home] = 0
        paths[home] = []
        pending = deque([home])
        while pending:
            source = pending.popleft()
            for target, label in adjacency.get(source, []):
                if target in distances:
                    continue
                distances[target] = distances[source] + 1
                paths[target] = [*paths[source], label]
                pending.append(target)

    taps = {screen_id: distances.get(screen_id) for screen_id in declared_ids}
    evidence = {screen_id: paths.get(screen_id, []) for screen_id in declared_ids}
    passed = bool(declared_ids) and all(
        tap_count is not None and tap_count <= 3 for tap_count in taps.values()
    )
    return {
        "verdict": "yes" if passed else "no",
        "limit_taps": 3,
        "taps": taps,
        "evidence": evidence,
    }


def evaluate_calibration(
    calibration: Any, declared_screens: tuple[dict[str, str], ...]
) -> dict[str, Any]:
    builds = calibration.get("builds", []) if isinstance(calibration, dict) else []
    repeat_results = (
        calibration.get("repeat_gate_results", {}) if isinstance(calibration, dict) else {}
    )
    by_id = {
        build.get("id"): build
        for build in (builds if isinstance(builds, list) else [])
        if isinstance(build, dict) and isinstance(build.get("id"), str)
    }
    exact_build_set = (
        isinstance(builds, list) and set(by_id) == set(CALIBRATION_LABELS) and len(builds) == 8
    )
    matched_builds = 0
    labels_valid = exact_build_set
    build_results = []
    if exact_build_set:
        for build_id, required_label in CALIBRATION_LABELS.items():
            build = by_id[build_id]
            expected = build.get("expected")
            candidate = build.get("first_run")
            observed_result = evaluate_first_run(candidate, declared_screens)
            observed = observed_result["verdict"]
            fixture_valid = observed_result["unavailable_reason"] is None
            if expected not in ("yes", "no") or not fixture_valid:
                labels_valid = False
            if required_label is not None and expected != required_label:
                labels_valid = False
            matched = fixture_valid and expected == observed
            if matched:
                matched_builds += 1
            build_results.append(
                {"id": build_id, "expected": expected, "observed": observed, "matched": matched}
            )

    stable_by_build = {}
    for build_id in STABILITY_BUILDS:
        results = repeat_results.get(build_id) if isinstance(repeat_results, dict) else None
        stable_by_build[build_id] = (
            isinstance(results, list)
            and len(results) == 3
            and all(result in ("green", "red") for result in results)
            and len(set(results)) == 1
        )
    stable_gate_runs = all(stable_by_build.values())
    return {
        "passed": labels_valid and matched_builds == 8 and stable_gate_runs,
        "matched_builds": matched_builds,
        "total_builds": 8,
        "stable_gate_runs": stable_gate_runs,
        "build_results": build_results,
        "stable_by_build": stable_by_build,
    }
