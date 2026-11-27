#!/usr/bin/env python3
"""Create deterministic event and removal counts from an FA-SVC logcat capture."""

import argparse
import hashlib
import json
import re
from pathlib import Path


EVENT_LINE = re.compile(
    r"origin=(?P<origin>[^,\]\s]+),name=(?P<name>[^,\]\s]+),"
    r"params=Bundle\[(?P<params>.*)\]\s*$"
)


def read_events(log_bytes: bytes) -> list[dict[str, object]]:
    text = log_bytes.decode("utf-8", errors="strict")
    events = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if "Logging event" not in line:
            continue
        match = EVENT_LINE.search(line)
        if match is None:
            raise ValueError(f"malformed FA-SVC event at line {line_number}")
        events.append(
            {
                "line": line_number,
                "origin": match.group("origin"),
                "name": match.group("name"),
                "params": match.group("params"),
            }
        )
    return events


def build_ledger(events: list[dict[str, object]], receipt: dict) -> dict:
    rows = []
    for item in receipt["coverage"]["event_assertions"]:
        name = item["event"]
        observations = [event for event in events if event["name"] == name]
        rows.append(
            {
                "assertion_id": item["assertion_id"],
                "event": name,
                "count": len(observations),
                "observations": observations,
            }
        )
    for item in receipt["coverage"]["removal_assertions"]:
        names = set(item["removed_names"])
        observations = [
            event
            for event in events
            if event["origin"] == "app" and event["name"] in names
        ]
        rows.append(
            {
                "assertion_id": item["assertion_id"],
                "removed_names": item["removed_names"],
                "count": len(observations),
                "observations": observations,
            }
        )
    return {"event_stream": events, "rows": rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    log_bytes = args.log.read_bytes()
    events = read_events(log_bytes)
    receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    ledger = build_ledger(events, receipt)
    ledger["log_sha256"] = hashlib.sha256(log_bytes).hexdigest()
    args.output.write_text(
        json.dumps(ledger, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
