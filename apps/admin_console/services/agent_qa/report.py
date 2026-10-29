"""Report core: JSON + HTML for one check session."""

from __future__ import annotations

from dataclasses import asdict
from html import escape
from io import BytesIO
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

from PIL import Image

from apps.admin_console.core.redaction import redact_json
from apps.admin_console.services.agent_qa.flow import (
    FlowError,
    build_flow,
    redact_known_values,
    secret_values,
)
from apps.admin_console.services.agent_qa.frc import evaluate_first_run
from apps.admin_console.services.agent_qa.gate import evaluate_gate
from apps.admin_console.services.agent_qa.session import Session
from artemis.utils.private_screen import mask_password_observation
from artemis.utils.redaction import register_secrets

_CSS = """
body{font:14px/1.5 Inter,system-ui,sans-serif;background:#fafafa;color:#27272a;margin:0;padding:24px}
h1{font-size:1.5rem;color:#18181b;margin:0 0 4px} h2{font-size:1rem;margin:24px 0 8px;color:#18181b}
.chip{display:inline-block;padding:2px 10px;border-radius:4px;font-weight:600;font-size:.75rem}
.green{background:#dcfce7;color:#166534}.red{background:#fee2e2;color:#991b1b}
table{border-collapse:collapse;background:#fff;width:100%}td,th{border:1px solid #e4e4e7;padding:6px 10px;text-align:left}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}
.theme{background:#fff;border:1px solid #e4e4e7;border-radius:4px;padding:12px}
.step{display:flex;gap:8px;align-items:flex-start;margin:8px 0;border-top:1px solid #e4e4e7;padding-top:8px}
.step img{width:96px;border:1px solid #d4d4d8;border-radius:4px}.step figure{margin:0;font-size:.75rem}
code{font:.75rem 'JetBrains Mono',monospace}
"""


def build_report(session: Session) -> dict[str, Any]:
    """Gate result plus, per theme, the flow artifact and screen map.

    A theme whose flow fails validation is listed in ``flow_errors`` and blocks
    ``green``: a green verdict never rests on missing or blank screenshots.
    """
    gate = evaluate_gate(session)
    themes: dict[str, Any] = {}
    flow_errors: dict[str, str] = {}
    for theme in session.themes:
        try:
            flow = build_flow(session, theme)
            themes[theme] = {"flow": list(flow.steps), "screen_map": flow.screen_map}
        except FlowError as err:
            flow_errors[theme] = err.code
            themes[theme] = {"flow": [], "screen_map": {}}
    report = {
        "package": session.package,
        "green": gate.green and not flow_errors,
        "gate": asdict(gate),
        "themes": themes,
        "flow_errors": flow_errors,
    }
    if os.environ.get("ARTEMIS_SMARTQA_FRC") == "on" and session.first_run is not None:
        report["first_run_comprehension"] = evaluate_first_run(
            session.first_run, session.declared_screens
        )
    return redact_known_values(redact_json(report), secret_values(session))


def _rect(bounds: object, width: int, height: int) -> tuple[int, int, int, int] | None:
    if isinstance(bounds, str):
        match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds)
        if match is None:
            return None
        coordinates = tuple(map(int, match.groups()))
    elif (
        isinstance(bounds, list | tuple)
        and len(bounds) == 4
        and all(isinstance(value, int) and not isinstance(value, bool) for value in bounds)
    ):
        coordinates = tuple(bounds)
    else:
        return None
    left, top, right, bottom = coordinates
    left, top = max(0, left), max(0, top)
    right, bottom = min(width, right), min(height, bottom)
    return (left, top, right, bottom) if right > left and bottom > top else None


def _android_bounds(bounds: object, width: int, height: int) -> str | None:
    rectangle = _rect(bounds, width, height)
    if rectangle is None:
        return None
    left, top, right, bottom = rectangle
    return f"[{left},{top}][{right},{bottom}]"


def _masked_png(session: Session, step, source: Path) -> bytes:
    raw = source.read_bytes()
    with Image.open(BytesIO(raw)) as opened:
        width, height = opened.size
    elements = []
    if step.flag_secure:
        bounds = f"[0,0][{width},{height}]"
        elements.append({"password": True, "bounds": bounds})
    else:
        private_nodes = [
            node
            for node in step.text_nodes
            if node.get("password") in (True, "true") or node.get("secret") in (True, "true")
        ]
        action_private = step.action.get("secret") is True
        system_bounds = [session.status_bar_bounds, session.navigation_bar_bounds]
        private_bounds = [node.get("bounds") for node in private_nodes]
        if action_private:
            target = step.action.get("target")
            target_bounds = target.get("bounds") if isinstance(target, dict) else None
            if target_bounds is not None:
                private_bounds.append(target_bounds)
        if action_private and not any(private_bounds):
            elements.append({"password": True})
        for item in system_bounds:
            if not item:
                continue
            normalized = _android_bounds(item, width, height)
            elements.append({"password": True, **({"bounds": normalized} if normalized else {})})
        for item in private_bounds:
            if not item:
                elements.append({"password": True})
                continue
            normalized = _android_bounds(item, width, height)
            elements.append({"password": True, **({"bounds": normalized} if normalized else {})})
    if not elements:
        return raw
    screen = SimpleNamespace(
        ui_elements=elements,
        ui_hierarchy_xml=None,
        screenshot_bytes=raw,
        screenshot_base64="",
        width=width,
        height=height,
    )
    return mask_password_observation(screen).screenshot_bytes


def _write_screenshots(session: Session, report: dict[str, Any], out_dir: Path) -> None:
    session_root = session.base_dir.resolve()
    output_root = out_dir.resolve()
    for theme, data in report["themes"].items():
        steps = session.themes[theme].steps
        if len(steps) != len(data["flow"]):
            continue
        for step, record in zip(steps, data["flow"], strict=True):
            for key in ("before_png", "after_png"):
                relative = Path(record[key])
                source = (session_root / relative).resolve()
                destination = output_root / "smartqa" / relative
                try:
                    source.relative_to(session_root)
                    destination.resolve().relative_to(output_root)
                except ValueError as exc:
                    raise FlowError("flow_incomplete") from exc
                current = output_root
                for part in ("smartqa", *relative.parts):
                    current /= part
                    if current.is_symlink():
                        raise FlowError("flow_incomplete")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(_masked_png(session, step, source))


def _rule_rows(report: dict[str, Any]) -> str:
    rows = []
    for rule in report["gate"]["rules"]:
        mark = "✓ Pass" if rule["passed"] else "✕ Fail"
        detail = (
            "; ".join(escape(json.dumps(f, ensure_ascii=False)) for f in rule["findings"]) or "—"
        )
        info = (
            f" <em>(informational: {len(rule['informational'])})</em>"
            if rule["informational"]
            else ""
        )
        rows.append(
            f"<tr><td>{rule['rule']}</td><td>{escape(rule['name'])}</td><td>{mark}</td><td>{detail}{info}</td></tr>"
        )
    return "\n".join(rows)


def _theme_block(theme: str, data: dict[str, Any], error: str | None, prefix: str) -> str:
    out = [f'<section class="theme"><h2>{escape(theme.title())}</h2>']
    if error:
        out.append(f'<p><span class="chip red">{escape(error)}</span></p>')
    for step in data["flow"]:
        action = step["action"]
        target = action.get("target", {})
        label = f"{action['type']} {target.get('text') or target.get('resource_id') or ''}".strip()
        screenshot_label = (
            f" ({escape(step['screenshot_label'])})" if step.get("screenshot_label") else ""
        )
        figs = "".join(
            f'<figure><img src="{escape(prefix + step[key])}" alt="{name} step {step["step"]}"><figcaption>{name}{screenshot_label}</figcaption></figure>'
            for key, name in (("before_png", "Before"), ("after_png", "After"))
        )
        out.append(
            f'<div class="step"><div><strong>{step["step"]}</strong> {escape(label)}<br>'
            f"<code>{escape(step['screen_before'])} → {escape(step['screen_after'])}</code></div>{figs}</div>"
        )
    screens = "".join(
        f"<li><code>{escape(s)}</code>"
        + "".join(f" → {escape(a['to'])} ({escape(a['action'])})" for a in leaves)
        + "</li>"
        for s, leaves in data["screen_map"].items()
    )
    out.append(f"<h2>Screen map</h2><ul>{screens}</ul></section>")
    return "\n".join(out)


def _frc_block(frc: dict[str, Any]) -> str:
    rows = []
    for criterion, result in frc["criteria"].items():
        evidence = escape(json.dumps(result["evidence"], ensure_ascii=False))
        rows.append(
            f"<tr><td>{escape(criterion)}</td><td>{escape(result['verdict'])}</td>"
            f"<td><code>{evidence}</code></td></tr>"
        )
    calibration = frc.get("calibration")
    calibration_row = ""
    if calibration is not None:
        calibration_verdict = "pass" if calibration["passed"] else "fail"
        calibration_row = (
            f"<p>Fixture calibration: {calibration_verdict}; {calibration['matched_builds']}/"
            f"{calibration['total_builds']} labels; repeated hard-gate result stable: "
            f"{str(calibration['stable_gate_runs']).lower()}.</p>"
        )
    return (
        "<section><h2>First-run comprehension (advisory)</h2>"
        f"<p>Verdict: {escape(frc['verdict'])}; this result does not change the hard gate.</p>"
        "<table><tr><th>Criterion</th><th>Result</th><th>Evidence</th></tr>"
        f"{' '.join(rows)}</table>{calibration_row}</section>"
    )


def render_html(report: dict[str, Any], *, png_prefix: str = "") -> str:
    """Self-contained page (inline CSS, no script, no SVG); PNGs are linked relatively."""
    verdict = ("green", "GREEN") if report["green"] else ("red", "RED")
    blocks = "\n".join(
        _theme_block(theme, data, report["flow_errors"].get(theme), png_prefix)
        for theme, data in report["themes"].items()
    )
    frc = (
        _frc_block(report["first_run_comprehension"]) if "first_run_comprehension" in report else ""
    )
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>SmartQA report</title>'
        f"<style>{_CSS}</style></head><body>"
        f'<h1>Agent QA report <span class="chip {verdict[0]}">{verdict[1]}</span></h1>'
        f"<p><code>{escape(report['package'])}</code></p>"
        "<h2>Hard gate</h2><table><tr><th>#</th><th>Rule</th><th>Result</th><th>Findings</th></tr>"
        f"{_rule_rows(report)}</table>"
        f'{frc}<h2>Flow</h2><div class="grid">{blocks}</div></body></html>'
    )


def write_report(session: Session, out_dir: Path) -> dict[str, Any]:
    """Write ``report.json``, ``flow.json`` and ``report.html``; return the report."""
    values = secret_values(session)
    register_secrets(list(values))
    report = build_report(session)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_screenshots(session, report, out_dir)
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    flows = {theme: data["flow"] for theme, data in report["themes"].items()}
    (out_dir / "flow.json").write_text(
        json.dumps(flows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "report.html").write_text(
        render_html(report, png_prefix="smartqa/"), encoding="utf-8"
    )
    return report
