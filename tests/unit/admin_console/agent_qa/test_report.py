import json

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
    assert 'src="../s/light/1-before.png"' in html
