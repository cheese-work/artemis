"""Hard gate, rules 1-4: crash, ANR, contrast, coverage."""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from PIL import UnidentifiedImageError

from apps.admin_console.services.agent_qa.contrast import measure_node, required_ratio
from apps.admin_console.services.agent_qa.session import Session

THEMES = ("light", "dark")
RULE_NAMES = {1: "crash", 2: "anr", 3: "contrast", 4: "coverage"}


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule: int
    name: str
    passed: bool
    findings: list[dict] = field(default_factory=list)
    informational: list[dict] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GateResult:
    green: bool
    rules: list[RuleResult]


def _rule(n: int, findings: list[dict], informational: list[dict] | None = None) -> RuleResult:
    return RuleResult(n, RULE_NAMES[n], not findings, findings, informational or [])


def _crash_entries(log: str, package: str) -> int:
    pkg = re.escape(package)
    java = re.compile(rf"Process:\s*{pkg}(?=[,:\s]|$)", re.MULTILINE)
    native = re.compile(rf">>>\s*{pkg}(?::\S+)?\s*<<<")
    blocks = re.split(r"FATAL EXCEPTION", log)[1:]
    return sum(1 for b in blocks if java.search(b)) + len(native.findall(log))


def _crash(session: Session) -> RuleResult:
    findings = [
        {"theme": theme, "count": n}
        for theme, run in session.themes.items()
        if (n := _crash_entries(run.logcat_crash, session.package))
    ]
    return _rule(1, findings)


def _anr(session: Session) -> RuleResult:
    pattern = re.compile(rf"ANR in {re.escape(session.package)}(?![\w.])")
    findings = [
        {"theme": theme, "count": n}
        for theme, run in session.themes.items()
        if (n := len(pattern.findall(run.logcat)))
    ]
    return _rule(2, findings)


def _contrast(session: Session) -> RuleResult:
    findings: list[dict] = []
    informational: list[dict] = []
    for theme, run in session.themes.items():
        for step in run.steps:
            for node in step.text_nodes:
                item = {"theme": theme, "step": step.step, "text": node.get("text", "")}
                try:
                    ratio = measure_node(session.base_dir / step.after_png, node)
                except ValueError:
                    item["error"] = "bounds_invalid"
                except (OSError, UnidentifiedImageError):
                    item["error"] = "screenshot_unreadable"
                else:
                    required = required_ratio(node)
                    if ratio is None:
                        item["error"] = "contrast_unmeasurable"
                    elif ratio >= required:
                        continue
                    else:
                        item |= {"ratio": round(ratio, 2), "required": required}
                (informational if node.get("kind") == "image" else findings).append(item)
    return _rule(3, findings, informational)


def _coverage(session: Session) -> RuleResult:
    declared = [s["id"] for s in session.declared_screens]
    findings: list[dict] = []
    if not declared:
        findings.append({"error": "no_declared_screens"})
    for theme in THEMES:
        run = session.themes.get(theme)
        if not (run and run.steps):
            findings.append({"theme": theme, "error": "no_captures"})
        seen = {s for st in (run.steps if run else ()) for s in (st.screen_before, st.screen_after)}
        findings += [
            {"theme": theme, "screen": screen} for screen in declared if screen not in seen
        ]
    return _rule(4, findings)


def evaluate_gate(session: Session) -> GateResult:
    """Green only when rules 1-4 all pass. Image-only contrast findings are informational."""
    rules = [_crash(session), _anr(session), _contrast(session), _coverage(session)]
    return GateResult(all(r.passed for r in rules), rules)
