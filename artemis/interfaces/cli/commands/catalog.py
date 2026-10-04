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

"""Run catalog maintenance commands (artemis catalog)."""

from pathlib import Path
import sqlite3
from typing import Annotated

from artemis.config import DB_PATH
from artemis.data_engine import run_catalog
from rich.console import Console
import typer

catalog_app = typer.Typer(help="Maintain the run catalog (search index over past runs).")
console = Console()
DbOption = Annotated[Path, typer.Option("--db", help="Path to data_engine.db.")]


def _require_catalog(conn: sqlite3.Connection) -> None:
    if not run_catalog.catalog_ready(conn):
        console.print("Catalog not installed; run `artemis catalog migrate`.")
        raise typer.Exit(1)


@catalog_app.command("migrate")
def migrate(db: DbOption = DB_PATH) -> None:
    """Install the catalog (online backup first, then schema and backfill). Safe to repeat."""
    report = run_catalog.migrate(db)
    backup = f", backup {report.backup_path}" if report.backup_path else ""
    console.print(
        f"Catalog ready: {report.backfilled} runs backfilled{backup}, search={report.search_mode}"
    )


@catalog_app.command("backfill")
def backfill(db: DbOption = DB_PATH) -> None:
    """Create catalog rows for sessions that have none."""
    with sqlite3.connect(db, timeout=30.0) as conn:
        _require_catalog(conn)
        console.print(f"{run_catalog.backfill(conn)} runs backfilled")


@catalog_app.command("rebuild")
def rebuild(db: DbOption = DB_PATH) -> None:
    """Drop and rebuild the search index from the catalog tables."""
    with sqlite3.connect(db, timeout=30.0) as conn:
        _require_catalog(conn)
        console.print(
            f"{run_catalog.rebuild(conn)} runs indexed (search={run_catalog.search_mode(conn)})"
        )
