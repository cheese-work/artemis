import json

from PIL import Image
import pytest

from apps.admin_console.services.agent_qa.contrast import (
    contrast_ratio,
    measure_node,
    required_ratio,
)
from apps.admin_console.services.agent_qa.gate import evaluate_gate
from apps.admin_console.services.agent_qa.session import load_session
from tests.support.agent_qa_fixtures import PACKAGE, write_png, write_session


def _edit(root, fn):
    path = root / "session.json"
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data))
    return load_session(root)


def _rule(result, n):
    return next(r for r in result.rules if r.rule == n)


def test_clean_session_is_green_with_four_passing_rules(tmp_path):
    result = evaluate_gate(load_session(write_session(tmp_path)))
    assert result.green
    assert [(r.rule, r.passed) for r in result.rules] == [
        (1, True),
        (2, True),
        (3, True),
        (4, True),
    ]


FATAL = f"""E AndroidRuntime: FATAL EXCEPTION: main
E AndroidRuntime: Process: {PACKAGE}, PID: 4242
E AndroidRuntime: java.lang.IllegalStateException: boom
"""
OTHER_FATAL = FATAL.replace(PACKAGE, "com.other.app")


def test_rule1_crash_in_app_package_fails(tmp_path):
    session = _edit(
        write_session(tmp_path), lambda d: d["themes"]["dark"].update(logcat_crash=FATAL)
    )
    result = evaluate_gate(session)
    assert not result.green
    rule = _rule(result, 1)
    assert not rule.passed and rule.findings[0]["theme"] == "dark"


def test_rule1_native_crash_in_app_package_fails(tmp_path):
    native = f"F libc: Fatal signal 11 (SIGSEGV), code 1, fault addr 0x0 in tid 9 ({PACKAGE})\nF DEBUG: >>> {PACKAGE} <<<\n"
    session = _edit(
        write_session(tmp_path), lambda d: d["themes"]["light"].update(logcat_crash=native)
    )
    assert not _rule(evaluate_gate(session), 1).passed


def test_rule1_crash_of_another_package_is_ignored(tmp_path):
    session = _edit(
        write_session(tmp_path), lambda d: d["themes"]["light"].update(logcat_crash=OTHER_FATAL)
    )
    assert _rule(evaluate_gate(session), 1).passed


def test_rule2_anr_in_app_package_fails_but_prefix_package_does_not(tmp_path):
    anr = f"I ActivityManager: ANR in {PACKAGE} ({PACKAGE}/.Main)\n"
    prefix = (
        f"I ActivityManager: ANR in {PACKAGE}.other (x)\nI ActivityManager: ANR in {PACKAGE}extra\n"
    )
    assert not _rule(
        evaluate_gate(
            _edit(write_session(tmp_path / "a"), lambda d: d["themes"]["light"].update(logcat=anr))
        ),
        2,
    ).passed
    assert _rule(
        evaluate_gate(
            _edit(
                write_session(tmp_path / "b"), lambda d: d["themes"]["light"].update(logcat=prefix)
            )
        ),
        2,
    ).passed


def test_wcag_ratio_known_values():
    assert contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0)
    assert contrast_ratio((119, 119, 119), (255, 255, 255)) == pytest.approx(4.48, abs=0.01)
    assert contrast_ratio((255, 255, 255), (255, 255, 255)) == pytest.approx(1.0)


def test_required_ratio_normal_large_and_bold_thresholds():
    assert required_ratio({"size_pt": 12}) == 4.5
    assert required_ratio({"size_pt": 18}) == 3.0
    assert required_ratio({"size_pt": 14, "bold": True}) == 3.0
    assert required_ratio({"size_pt": 14}) == 4.5
    assert required_ratio({}) == 4.5


def test_measure_node_samples_pixels_inside_bounds(tmp_path):
    png = tmp_path / "x.png"
    write_png(png, bg=(255, 255, 255), fg=(0, 0, 0))
    assert measure_node(png, {"bounds": [0, 0, 120, 60]}) == pytest.approx(21.0)
    assert (
        measure_node(png, {"bounds": [70, 40, 110, 55]}) is None
    )  # only background: nothing to measure


def _node(**kw):
    return {"kind": "text", "text": "Start", "bounds": [0, 0, 120, 60], "size_pt": 12, **kw}


def test_rule3_low_contrast_text_fails_with_finding(tmp_path):
    root = write_session(tmp_path, themes=("light",))
    write_png(root / "light" / "1-after.png", bg=(255, 255, 255), fg=(200, 200, 200))
    session = _edit(root, lambda d: d["themes"]["light"]["steps"][0].update(text_nodes=[_node()]))
    rule = _rule(evaluate_gate(session), 3)
    assert not rule.passed
    finding = rule.findings[0]
    assert (finding["theme"], finding["step"], finding["text"], finding["required"]) == (
        "light",
        1,
        "Start",
        4.5,
    )
    assert finding["ratio"] < 4.5


def test_rule3_large_text_uses_lower_threshold(tmp_path):
    root = write_session(tmp_path, themes=("light",))
    write_png(root / "light" / "1-after.png", bg=(255, 255, 255), fg=(140, 140, 140))  # about 3.4:1
    small = _edit(root, lambda d: d["themes"]["light"]["steps"][0].update(text_nodes=[_node()]))
    assert not _rule(evaluate_gate(small), 3).passed
    large = _edit(
        root, lambda d: d["themes"]["light"]["steps"][0].update(text_nodes=[_node(size_pt=18)])
    )
    assert _rule(evaluate_gate(large), 3).passed


def test_rule3_image_only_nodes_are_informational(tmp_path):
    root = write_session(tmp_path, themes=("light",))
    write_png(root / "light" / "1-after.png", bg=(255, 255, 255), fg=(200, 200, 200))
    session = _edit(
        root, lambda d: d["themes"]["light"]["steps"][0].update(text_nodes=[_node(kind="image")])
    )
    rule = _rule(evaluate_gate(session), 3)
    assert rule.passed and rule.informational and not rule.findings


def test_rule4_declared_screen_missing_in_dark_fails(tmp_path):
    root = write_session(tmp_path, themes=("light", "dark"))
    session = _edit(
        root,
        lambda d: d["themes"]["dark"]["steps"].__setitem__(
            slice(None), d["themes"]["dark"]["steps"][:2]
        ),
    )
    rule = _rule(evaluate_gate(session), 4)
    assert not rule.passed
    assert {(f["theme"], f["screen"]) for f in rule.findings} == {("dark", "Pager")}


def test_rule4_theme_with_no_steps_misses_every_screen(tmp_path):
    session = _edit(write_session(tmp_path), lambda d: d["themes"].pop("dark"))
    rule = _rule(evaluate_gate(session), 4)
    assert not rule.passed and {f["theme"] for f in rule.findings} == {"dark"}


def _text_crop(path, fg):
    """Light-grey text rectangle on white; ``fg`` is the true text colour."""
    write_png(path, bg=(255, 255, 255), fg=fg)


def test_measure_node_ignores_a_single_stray_high_contrast_pixel(tmp_path):
    png = tmp_path / "x.png"
    _text_crop(png, (200, 200, 200))  # about 1.67:1 text
    with Image.open(png) as img:
        img.putpixel((100, 50), (0, 0, 0))  # one non-text pixel
        img.save(png)
    ratio = measure_node(png, {"bounds": [0, 0, 120, 60]})
    assert ratio == pytest.approx(contrast_ratio((200, 200, 200), (255, 255, 255)), abs=0.01)


def test_rule3_stray_black_pixel_does_not_turn_low_contrast_text_green(tmp_path):
    root = write_session(tmp_path, themes=("light",))
    png = root / "light" / "1-after.png"
    _text_crop(png, (200, 200, 200))
    with Image.open(png) as img:
        img.putpixel((100, 50), (0, 0, 0))
        img.save(png)
    session = _edit(root, lambda d: d["themes"]["light"]["steps"][0].update(text_nodes=[_node()]))
    assert not _rule(evaluate_gate(session), 3).passed


@pytest.mark.parametrize("bounds", [[0, 0, 200, 60], [-10, 0, 120, 60], [50, 10, 50, 40], [0, 0]])
def test_rule3_invalid_bounds_fail_instead_of_being_padded(tmp_path, bounds):
    root = write_session(tmp_path, themes=("light",))
    _text_crop(root / "light" / "1-after.png", (200, 200, 200))
    session = _edit(
        root,
        lambda d: d["themes"]["light"]["steps"][0].update(text_nodes=[_node(bounds=bounds)]),
    )
    rule = _rule(evaluate_gate(session), 3)
    assert not rule.passed and rule.findings[0]["error"] == "bounds_invalid"


def test_rule3_unreadable_screenshot_is_a_finding_not_an_exception(tmp_path):
    root = write_session(tmp_path, themes=("light",))
    (root / "light" / "1-after.png").unlink()
    session = _edit(root, lambda d: d["themes"]["light"]["steps"][0].update(text_nodes=[_node()]))
    rule = _rule(evaluate_gate(session), 3)
    assert not rule.passed and rule.findings[0]["error"] == "screenshot_unreadable"


def test_rule4_empty_declaration_and_empty_themes_are_not_green(tmp_path):
    session = _edit(
        write_session(tmp_path),
        lambda d: d.update(declared_screens=[], themes={}),
    )
    result = evaluate_gate(session)
    rule = _rule(result, 4)
    assert not result.green and not rule.passed
    assert {f.get("error") for f in rule.findings} >= {"no_declared_screens", "no_captures"}


def test_rule4_declared_screens_empty_with_captures_is_not_green(tmp_path):
    session = _edit(write_session(tmp_path), lambda d: d.update(declared_screens=[]))
    result = evaluate_gate(session)
    assert not result.green
    assert _rule(result, 4).findings[0]["error"] == "no_declared_screens"
