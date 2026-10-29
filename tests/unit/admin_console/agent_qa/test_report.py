import json
import shutil
import subprocess

from PIL import Image, ImageDraw
import pytest

from apps.admin_console.services.agent_qa.report import build_report, render_html, write_report
from apps.admin_console.services.agent_qa.session import load_session
from tests.support.agent_qa_fixtures import write_session


def test_report_json_has_verdict_gate_flow_and_screen_map_per_theme(tmp_path):
    report = build_report(load_session(write_session(tmp_path)))
    assert report["green"] is True
    assert [r["rule"] for r in report["gate"]["rules"]] == [1, 2, 3, 4]
    assert sorted(report["themes"]) == ["dark", "light"]
    light = report["themes"]["light"]
    assert len(light["flow"]) == 6 and sorted(light["screen_map"]) == [
        "Home",
        "NativePool",
        "Pager",
    ]
    assert report["flow_errors"] == {}


def test_incomplete_flow_is_reported_and_blocks_green(tmp_path):
    root = write_session(tmp_path)
    (root / "dark" / "3-after.png").unlink()
    report = build_report(load_session(root))
    assert report["flow_errors"] == {"dark": "flow_incomplete"}
    assert report["themes"]["dark"]["flow"] == []
    assert report["green"] is False


def test_failed_rule_makes_report_red(tmp_path):
    root = write_session(tmp_path)
    data = json.loads((root / "session.json").read_text())
    data["themes"]["light"]["logcat"] = "ANR in com.example.sample (x)"
    (root / "session.json").write_text(json.dumps(data))
    report = build_report(load_session(root))
    assert report["green"] is False
    assert next(r for r in report["gate"]["rules"] if r["rule"] == 2)["passed"] is False


def test_html_is_self_contained_escaped_and_svg_free(tmp_path):
    root = write_session(tmp_path)
    data = json.loads((root / "session.json").read_text())
    data["themes"]["light"]["steps"][1]["action"]["target"]["text"] = "<script>alert(1)</script>"
    (root / "session.json").write_text(json.dumps(data))
    html = render_html(build_report(load_session(root)))
    assert "<script" not in html.lower() and "<svg" not in html.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert 'src="light/1-before.png"' in html and "Before" in html and "After" in html
    assert "GREEN" in html or "Green" in html


def test_write_report_writes_json_flow_and_html(tmp_path):
    root = write_session(tmp_path / "s")
    out = tmp_path / "out"
    report = write_report(load_session(root), out)
    assert json.loads((out / "report.json").read_text()) == report
    flow = json.loads((out / "flow.json").read_text())
    assert [len(flow[t]) for t in ("light", "dark")] == [6, 6]
    html = (out / "report.html").read_text()
    assert 'src="smartqa/light/1-before.png"' in html


def _write_t4_session(root, sentinel, *, secret_fields=True):
    session_root = write_session(root)
    session_data = json.loads((session_root / "session.json").read_text())
    session_data["status_bar_bounds"] = [0, 0, 120, 5]
    session_data["navigation_bar_bounds"] = [0, 55, 120, 60]
    for theme in ("light", "dark"):
        steps = session_data["themes"][theme]["steps"]
        for index, field, flag in ((0, "text", "password"), (1, "value", "secret")):
            step = steps[index]
            bounds = [0, 8, 120, 35]
            marked = secret_fields
            step["action"] = {
                "type": "type",
                "secret": marked,
                field: sentinel,
                "target": {"text": "Account field", "bounds": bounds},
            }
            step["text_nodes"] = [
                {
                    "kind": "text",
                    "text": sentinel,
                    "password": marked and flag == "password",
                    "secret": marked and flag == "secret",
                    "bounds": bounds,
                }
            ]
            for side in ("before", "after"):
                screenshot = session_root / theme / f"{index + 1}-{side}.png"
                with Image.open(screenshot) as image:
                    ImageDraw.Draw(image).text((1, 11), sentinel, fill=(0, 0, 0))
                    image.save(screenshot)
        steps[2]["flag_secure"] = True
    (session_root / "session.json").write_text(json.dumps(session_data), encoding="utf-8")
    return session_root


def test_t4_secret_values_are_redacted_and_screenshots_masked_before_write(library, qa, tmp_path):
    sentinel = "SMARTQA-SENTINEL-7f3c"
    session_root = _write_t4_session(tmp_path / "recorded", sentinel)

    sid = library.seed("fixture smartqa run")
    run_root = library.traces / sid
    report = write_report(load_session(session_root), run_root)
    library.write(sid, "stdout.log", f"typed fixture value {sentinel}\n")
    library.write(sid, "notes/llm-request.json", json.dumps({"content": sentinel}))

    action = report["themes"]["light"]["flow"][0]["action"]
    assert action["text"] == "[redacted]"
    assert action["text_length_class"] == "long"
    assert report["themes"]["light"]["flow"][2]["screenshot_label"] == "FLAG_SECURE"
    for name in ("flow.json", "report.json", "report.html"):
        assert sentinel not in (run_root / name).read_text(encoding="utf-8")
    assert "FLAG_SECURE" in (run_root / "report.html").read_text(encoding="utf-8")

    image_paths = sorted((run_root / "smartqa").rglob("*.png"))
    assert len(image_paths) == 24
    for image_path in image_paths:
        with Image.open(image_path) as image:
            assert set(image.crop((0, 0, 120, 5)).getdata()) == {(0, 0, 0)}
            assert set(image.crop((0, 55, 120, 60)).getdata()) == {(0, 0, 0)}
            if image_path.name.startswith(("1-", "2-", "3-")):
                assert set(image.crop((0, 8, 120, 35)).getdata()) == {(0, 0, 0)}

    import io
    import zipfile
    from apps.admin_console.services import run_bundle

    bundle = run_bundle.prepare(sid)
    try:
        bundle_data = bundle.path.read_bytes()
    finally:
        bundle.finish()
    with zipfile.ZipFile(io.BytesIO(bundle_data)) as archive:
        assert "flow.json" in archive.namelist()
        assert any(name.endswith("/1-before.png") for name in archive.namelist())
        for name in archive.namelist():
            if name.endswith((".json", ".html", ".log")):
                assert sentinel not in archive.read(name).decode("utf-8", "replace")
        for name in archive.namelist():
            if name.endswith(("/1-before.png", "/1-after.png", "/2-before.png", "/2-after.png")):
                with Image.open(io.BytesIO(archive.read(name))) as image:
                    assert set(image.crop((0, 8, 120, 35)).getdata()) == {(0, 0, 0)}


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")
def test_t4_tesseract_checks_masked_pngs_and_unmarked_negative_control(tmp_path):
    sentinel = "SMARTQA-SENTINEL-7f3c"
    secret_root = _write_t4_session(tmp_path / "secret", sentinel)
    secret_output = tmp_path / "secret-output"
    write_report(load_session(secret_root), secret_output)
    for image_path in (secret_output / "smartqa").rglob("*.png"):
        text = subprocess.run(
            ["tesseract", str(image_path), "stdout"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert sentinel not in text

    control_root = _write_t4_session(tmp_path / "negative-control", sentinel, secret_fields=False)
    control_output = tmp_path / "negative-control-output"
    write_report(load_session(control_root), control_output)
    text = subprocess.run(
        ["tesseract", str(control_output / "smartqa/light/1-before.png"), "stdout"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert sentinel in text


def test_empty_session_report_is_red(tmp_path):
    root = write_session(tmp_path)
    data = json.loads((root / "session.json").read_text())
    data.update(declared_screens=[], themes={})
    (root / "session.json").write_text(json.dumps(data))
    report = build_report(load_session(root))
    assert report["green"] is False


def test_missing_after_png_under_text_node_gives_red_flow_incomplete_report(tmp_path):
    root = write_session(tmp_path)
    data = json.loads((root / "session.json").read_text())
    data["themes"]["dark"]["steps"][2]["text_nodes"] = [
        {"kind": "text", "text": "x", "bounds": [0, 0, 120, 60], "size_pt": 12}
    ]
    (root / "session.json").write_text(json.dumps(data))
    (root / "dark" / "3-after.png").unlink()
    report = build_report(load_session(root))
    assert report["green"] is False
    assert report["flow_errors"] == {"dark": "flow_incomplete"}
    contrast = next(r for r in report["gate"]["rules"] if r["rule"] == 3)
    assert contrast["findings"][0]["error"] == "screenshot_unreadable"
