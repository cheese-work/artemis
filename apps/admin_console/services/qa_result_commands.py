import argparse
from datetime import datetime
import json
from pathlib import Path

from apps.admin_console.core.access_control import config_from_environment
from apps.admin_console.services import qa_results
from artemis.config import DB_PATH, TRACES_PATH


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("a timezone offset is required")
        return parsed.timestamp()
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local-only SmartQA result maintenance")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--traces", type=Path, default=TRACES_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    activation = commands.add_parser("activate", help="Record the immutable deploy snapshot")
    activation.add_argument("--at", type=timestamp)
    for command in ("backfill", "replay"):
        maintenance = commands.add_parser(command)
        maintenance.add_argument(
            "--since", type=timestamp, default=qa_results.DEFAULT_BACKFILL_SINCE
        )
        maintenance.add_argument("--until", type=timestamp)
        maintenance.add_argument("--limit", type=int, default=100)
    options = parser.parse_args(argv)
    try:
        if options.command == "activate":
            result = qa_results.activate(
                options.db, config_from_environment().admin_emails, now=options.at
            )
        elif options.command == "backfill":
            result = qa_results.backfill(
                options.db,
                options.traces,
                since=options.since,
                until=options.until,
                limit=options.limit,
            )
        else:
            result = {
                "replayed": qa_results.replay_abandoned(
                    options.db, since=options.since, until=options.until, limit=options.limit
                )
            }
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
