# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Trace inspection and replay commands (artemis trace)."""

import datetime
import json
from pathlib import Path
from typing import Annotated

from artemis.config import settings
from artemis.config.constants import DATA_ENGINE_DB_FILENAME
from artemis.data_engine.reaction_time import read_session_reaction_time
from artemis.utils.logger import get_logger
from rich.console import Console
from rich.table import Table
import typer

logger = get_logger(__name__)
trace_app = typer.Typer(help="Inspect, list, and query task execution traces.")


@trace_app.command("list")
def list_traces(
    traces_path: Annotated[
        Path | None,
        typer.Option("--path", "-p", help="Custom traces directory path."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Max sessions to list.")] = 20,
) -> None:
    """List recorded execution trace sessions."""
    base_dir = traces_path or settings.TRACES_PATH
    if not base_dir.exists():
        typer.secho(f"No traces found at: {base_dir}", fg=typer.colors.YELLOW)
        return

    sessions = [d for d in base_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    sessions.sort(key=lambda x: x.stat().st_mtime, reverse=True)

    console = Console()
    table = Table(title=f"Recorded Traces ({len(sessions)} total)")
    table.add_column("Session Name", style="cyan")
    table.add_column("Last Modified", style="green")
    table.add_column("Artifacts", style="white")

    for s in sessions[:limit]:
        artifacts = []
        if (s / "recording.mp4").exists() or (s / "recording.mkv").exists():
            artifacts.append("🎬 Video")
        if (s / "steps.json").exists():
            artifacts.append("📋 Steps")
        if (s / "notes").exists():
            artifacts.append("📝 Notes")
        mtime = time_str = time_to_str(s.stat().st_mtime)
        table.add_row(s.name, mtime, ", ".join(artifacts) or "Screenshots")

    console.print(table)


def time_to_str(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


@trace_app.command("view")
def view_trace(
    session_name: Annotated[str, typer.Argument(help="Name of the trace session directory.")],
    traces_path: Annotated[Path | None, typer.Option("--path", "-p")] = None,
) -> None:
    """View step-by-step summary of a recorded trace session."""
    base_dir = traces_path or settings.TRACES_PATH
    session_dir = base_dir / session_name

    if not session_dir.exists():
        typer.secho(f"Session '{session_name}' not found in {base_dir}", fg=typer.colors.RED)
        raise typer.Exit(1)

    steps_file = session_dir / "steps.json"
    console = Console()

    if steps_file.exists():
        try:
            steps_data = json.loads(steps_file.read_text(encoding="utf-8"))
            table = Table(title=f"Trace Steps: {session_name}")
            table.add_column("Step", justify="right", style="cyan")
            table.add_column("Action", style="green")
            table.add_column("Reasoning / Motivation", style="white")

            for step in steps_data:
                table.add_row(
                    str(step.get("step", "-")),
                    step.get("action", "-"),
                    step.get("motivation", step.get("thought", "-")),
                )
            console.print(table)
            return
        except Exception as e:
            logger.warning(f"Could not parse steps.json: {e}")

    # If steps.json not present, list files
    typer.secho(f"Session files in {session_dir}:", fg=typer.colors.CYAN)
    for f in session_dir.iterdir():
        typer.echo(f"  - {f.name}")


def _fmt_seconds(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "-"


def _fmt_count(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "-"


def _fmt_ratio_pct(value: float | None) -> str:
    return f"{value * 100.0:.1f}%" if value is not None else "-"


@trace_app.command("timing")
def timing_trace(
    session_id: Annotated[
        str, typer.Argument(help="Session ID (trace directory name) to analyze.")
    ],
    traces_path: Annotated[Path | None, typer.Option("--path", "-p")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON output.")] = False,
) -> None:
    """Report per-step reaction time and Operator "re-asking itself" bounces.

    Reaction time per step is the last action-trace end minus the
    ``phase:perception`` span start (documented fallbacks when a phase is
    missing — see ``artemis/data_engine/reaction_time.py``). A bounce is any
    Operator tool-loop iteration whose outcome is neither ``executed`` nor
    ``no_tool_call`` — the Operator re-asking itself instead of finishing
    the turn.
    """
    base_dir = traces_path or settings.TRACES_PATH
    db_path = base_dir / DATA_ENGINE_DB_FILENAME
    if not db_path.exists():
        typer.secho(f"No trace database found at: {db_path}", fg=typer.colors.YELLOW)
        raise typer.Exit(1)

    report = read_session_reaction_time(db_path, base_dir, session_id)

    if as_json:
        typer.echo(json.dumps(report.model_dump(), indent=2))
        return

    if report.step_count == 0:
        typer.secho(
            f"No steps found for session '{session_id}' in {base_dir}", fg=typer.colors.YELLOW
        )
        return

    console = Console()

    summary = Table(title=f"Reaction Time Summary: {session_id}")
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", style="white")
    summary.add_row("Steps", str(report.step_count))
    summary.add_row("Steps with reaction time", str(report.steps_with_reaction_time))
    summary.add_row("p50 reaction time (s)", _fmt_seconds(report.p50_reaction_time_s))
    summary.add_row("p90 reaction time (s)", _fmt_seconds(report.p90_reaction_time_s))
    summary.add_row(
        "Median operator iterations/step", _fmt_count(report.median_operator_iterations)
    )
    summary.add_row("Steps with >=1 bounce", f"{report.pct_steps_with_bounce:.1f}%")
    summary.add_row("Total reaction time (s)", f"{report.total_reaction_time_s:.2f}")
    summary.add_row("Total bounce time (s)", f"{report.total_bounce_time_s:.2f}")
    summary.add_row(
        "Bounce share of reaction time", _fmt_ratio_pct(report.bounce_share_of_reaction_time)
    )
    console.print(summary)

    phase_table = Table(title="Phase Split by Step")
    phase_table.add_column("Step", justify="right", style="cyan")
    phase_table.add_column("Reaction (s)", justify="right")
    phase_table.add_column("Iterations", justify="right")
    phase_table.add_column("Bounces", justify="right")
    phase_table.add_column("Phase breakdown", style="white")
    for step in report.steps:
        phases = ", ".join(f"{k}={v:.2f}s" for k, v in sorted(step.phase_breakdown.items()))
        phase_table.add_row(
            str(step.step_number),
            _fmt_seconds(step.reaction_time_s),
            str(step.operator_iterations),
            str(step.bounce_count),
            phases or "-",
        )
    console.print(phase_table)

    if report.bounce_histogram:
        hist_table = Table(title="Operator Iteration Outcome Histogram")
        hist_table.add_column("Outcome", style="magenta")
        hist_table.add_column("Count", justify="right")
        for outcome, count in sorted(report.bounce_histogram.items(), key=lambda kv: -kv[1]):
            hist_table.add_row(outcome, str(count))
        console.print(hist_table)
