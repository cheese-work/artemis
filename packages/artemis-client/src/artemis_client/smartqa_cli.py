# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""``smartqa check``: the agent-facing command line for one SmartQA check."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from artemis_client.client import ArtemisClient
from artemis_client.errors import ArtemisClientError
from artemis_client.smartqa import (
    DEFAULT_MAX_APK_BYTES,
    PERSONAS,
    THEMES,
    CheckError,
    CheckOptions,
    run_check,
)

EXIT_RED, EXIT_USAGE = 1, 2
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smartqa")
    check = parser.add_subparsers(dest="command", required=True).add_parser(
        "check", help="run one SmartQA check on an APK"
    )
    check.add_argument("--apk", type=Path, required=True)
    check.add_argument("--manifest", type=Path, default=Path("smartqa.yml"))
    check.add_argument("--persona", default="first-time-user", choices=PERSONAS)
    check.add_argument("--themes", default=",".join(THEMES), help="comma list: light,dark")
    check.add_argument("--pool", help="device pool, for example x99-avd")
    check.add_argument("--device", help="exact device id (instead of --pool)")
    check.add_argument("--server", help="SmartQA base URL (default: $ARTEMIS_BASE_URL)")
    check.add_argument("--route", type=Path, help="scripted route file; replays without the LLM")
    check.add_argument("--max-apk-bytes", type=int, default=DEFAULT_MAX_APK_BYTES)
    return parser


def _credentials(env: Mapping[str, str]) -> tuple[str | None, dict[str, str]]:
    """Bearer token and/or the Cloudflare Access service-token header pair."""
    headers: dict[str, str] = {}
    client_id, secret = env.get("CF_ACCESS_CLIENT_ID"), env.get("CF_ACCESS_CLIENT_SECRET")
    if client_id and secret:
        headers = {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": secret}
    return env.get("ARTEMIS_TOKEN") or None, headers


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exit_:
        return EXIT_USAGE if exit_.code else 0
    server = args.server or env.get("ARTEMIS_BASE_URL")
    if not server:
        print("usage: --server or ARTEMIS_BASE_URL is required", file=sys.stderr)
        return EXIT_USAGE
    token, headers = _credentials(env)
    if not (token or headers) and urlsplit(server).hostname not in _LOOPBACK:
        print(
            "missing_token: set CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET (or "
            "ARTEMIS_TOKEN) for a non-local server",
            file=sys.stderr,
        )
        return EXIT_USAGE
    options = CheckOptions(
        apk=args.apk,
        manifest=args.manifest,
        themes=tuple(t.strip() for t in args.themes.split(",") if t.strip()),
        persona=args.persona,
        pool=args.pool,
        device_id=args.device,
        route=args.route,
        max_apk_bytes=args.max_apk_bytes,
    )
    client = ArtemisClient(server, token=token, headers=headers)
    try:
        result = asyncio.run(run_check(client, options))
    except CheckError as exc:
        print(str(exc), file=sys.stderr)
        return (
            EXIT_USAGE if exc.code in {"usage", "manifest_invalid", "route_invalid"} else EXIT_RED
        )
    except ArtemisClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RED
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.ok else EXIT_RED


if __name__ == "__main__":
    raise SystemExit(main())
