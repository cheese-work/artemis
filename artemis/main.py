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

"""Backward compatibility entrypoint for artemis CLI."""

import sys
from artemis.utils.redaction import bind_session, configure_logging, read_goal_file
import os


def cli():
    if "--goal-file" in sys.argv:
        position = sys.argv.index("--goal-file")
        if position + 1 >= len(sys.argv):
            raise SystemExit("--goal-file requires a private input file")
        goal = read_goal_file(sys.argv[position + 1])
        bind_session(os.environ.get("ARTEMIS_SESSION_ID"), goal)
        sys.argv[position : position + 2] = [goal]
    configure_logging(streams=True)
    from artemis.interfaces.cli.main import app

    # If invoked directly as python -m artemis.main without subcommand 'run',
    # check if first argument is a goal rather than a subcommand
    if len(sys.argv) > 1 and sys.argv[1] not in (
        "run",
        "init",
        "doctor",
        "batch",
        "bench",
        "mcp",
        "server",
        "trace",
        "--help",
        "-h",
    ):
        sys.argv.insert(1, "run")
    app()


if __name__ == "__main__":
    cli()
