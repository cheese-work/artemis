"""Offline Jev replay; never controls a device or writes the trace store."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

from artemis.config import settings
from artemis.config.constants import DATA_ENGINE_DB_FILENAME
from artemis.data_engine.jev_replay import load_steps, replay
from artemis.services.jev import DEFAULT_BASE_URL, JevClient
from rich.console import Console
from rich.table import Table
import typer

jev_app = typer.Typer(help="Offline Jev decision evaluation.")


@jev_app.command("replay")
def replay_command(
    sessions: Annotated[
        str | None, typer.Option("--sessions", help="Comma-separated session IDs or prefixes.")
    ] = None,
    all_sessions: Annotated[
        bool, typer.Option("--all", help="Replay every session in the store.")
    ] = False,
    traces_path: Annotated[
        Path | None, typer.Option("--path", help="Trace store directory.")
    ] = None,
    threshold: Annotated[float, typer.Option("--threshold")] = 0.9,
    runs: Annotated[int, typer.Option("--runs")] = 1,
    as_json: Annotated[
        bool, typer.Option("--json", help="Output JSON with per-step results.")
    ] = False,
    cost_per_call: Annotated[
        float, typer.Option("--cost-per-call", help="Estimated USD per Jev call.")
    ] = 0.00001,
    base_url: Annotated[
        str | None, typer.Option("--base-url", help="System One API base URL.")
    ] = None,
    key_file: Annotated[
        Path, typer.Option("--key-file", help="Jev API key file, used if not configured.")
    ] = Path.home() / ".config/artemis/openrouter.key",
) -> None:
    """Replay recorded decisions without taking any device actions."""
    if (not all_sessions and not sessions) or (all_sessions and sessions):
        raise typer.BadParameter("Select either --sessions or --all")
    if not 0 <= threshold <= 1 or runs < 1 or cost_per_call < 0:
        raise typer.BadParameter("Expected threshold in [0, 1], runs >= 1, cost >= 0")
    db_path = (traces_path or settings.TRACES_PATH) / DATA_ENGINE_DB_FILENAME
    if not db_path.is_file():
        raise typer.BadParameter(f"Trace database not found: {db_path}")
    api_key = settings.TYPESAFE_API_KEY
    secret = api_key.get_secret_value() if api_key else ""
    using_key_file = not secret and key_file.is_file()
    if using_key_file:
        if key_file.stat().st_mode & 0o077:
            raise typer.BadParameter("Key file must be owner-only (chmod 600)")
        secret = key_file.read_text(encoding="utf-8").strip()
    if not secret:
        raise typer.BadParameter("Configure TYPESAFE_API_KEY or provide --key-file")
    selected = [part.strip() for part in sessions.split(",") if part.strip()] if sessions else None
    rows = load_steps(db_path, selected)
    client = JevClient(
        secret,
        base_url=base_url
        or settings.TYPESAFE_BASE_URL
        or ("https://openrouter.ai/api/v1" if using_key_file else DEFAULT_BASE_URL),
        model=settings.ARTEMIS_JEV_FAST_LANE_MODEL,
        timeout=settings.ARTEMIS_JEV_TIMEOUT_SECONDS,
    )
    report = asyncio.run(replay(rows, client, threshold, runs, cost_per_call))
    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return
    result = report["metrics"]
    table = Table(title="Jev replay (verified frontier actions only)")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for name, value in (
        ("Verified / total steps", f"{result['graded']} / {result['steps']}"),
        ("Fast lane", f"{result['fast_laned']} ({result['fast_lane_pct']:.1f}%)"),
        (
            "Confident wrong / fast-laned",
            f"{result['confident_wrong']} ({result['confident_wrong_pct']:.1f}%)",
        ),
        ("Latency p50 / p90 (s)", f"{result['latency_p50_s']} / {result['latency_p90_s']}"),
        ("Calls / estimated USD", f"{result['calls']} / ${result['estimated_cost_usd']:.5f}"),
    ):
        table.add_row(name, value)
    for move, counts in result["per_move_type"].items():
        table.add_row(
            f"{move} accuracy",
            f"{counts['correct']}/{counts['fast_laned']} ({counts['accuracy_pct']:.1f}%)",
        )
    Console().print(table)
