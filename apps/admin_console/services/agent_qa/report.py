"""Report core: JSON + HTML for one check session."""

from __future__ import annotations

from dataclasses import asdict
from html import escape
import json
import os
from pathlib import Path
from typing import Any

from apps.admin_console.services.agent_qa.flow import FlowError, build_flow
from apps.admin_console.services.agent_qa.gate import evaluate_gate
from apps.admin_console.services.agent_qa.session import Session

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
    return {
        "package": session.package,
        "green": gate.green and not flow_errors,
        "gate": asdict(gate),
        "themes": themes,
        "flow_errors": flow_errors,
    }


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
        figs = "".join(
            f'<figure><img src="{escape(prefix + step[key])}" alt="{name} step {step["step"]}"><figcaption>{name}</figcaption></figure>'
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


def render_html(report: dict[str, Any], *, png_prefix: str = "") -> str:
    """Self-contained page (inline CSS, no script, no SVG); PNGs are linked relatively."""
    verdict = ("green", "GREEN") if report["green"] else ("red", "RED")
    blocks = "\n".join(
        _theme_block(theme, data, report["flow_errors"].get(theme), png_prefix)
        for theme, data in report["themes"].items()
    )
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>SmartQA report</title>'
        f"<style>{_CSS}</style></head><body>"
        f'<h1>Agent QA report <span class="chip {verdict[0]}">{verdict[1]}</span></h1>'
        f"<p><code>{escape(report['package'])}</code></p>"
        "<h2>Hard gate</h2><table><tr><th>#</th><th>Rule</th><th>Result</th><th>Findings</th></tr>"
        f"{_rule_rows(report)}</table>"
        f'<h2>Flow</h2><div class="grid">{blocks}</div></body></html>'
    )


def write_report(session: Session, out_dir: Path) -> dict[str, Any]:
    """Write ``report.json``, ``flow.json`` and ``report.html``; return the report."""
    report = build_report(session)
    out_dir.mkdir(parents=True, exist_ok=True)
    rel = os.path.relpath(session.base_dir, out_dir)
    prefix = "" if rel == "." else rel + "/"
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    flows = {theme: data["flow"] for theme, data in report["themes"].items()}
    (out_dir / "flow.json").write_text(
        json.dumps(flows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "report.html").write_text(render_html(report, png_prefix=prefix), encoding="utf-8")
    return report
