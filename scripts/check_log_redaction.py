import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import uuid

from artemis.utils.redaction import write_goal_file


def probe(path: str) -> None:
    from artemis import main
    from artemis.interfaces.cli import main as interface
    from artemis.utils.logger import get_logger

    original_app = interface.app

    def diagnostic_app():
        goal = sys.argv[2]
        print("event=s5_probe_started")
        logging.getLogger("s5-probe-root").warning("event=s5_goal_probe %s", goal)
        logger = get_logger("s5-probe-artemis")
        logger.info(f"event=s5_configured_secret_probe {os.environ['OPENAI_API_KEY']}")
        try:
            raise RuntimeError(goal)
        except RuntimeError:
            logger.error("event=s5_exception_probe", exc_info=True)
        midpoint = len(goal) // 2
        sys.stdout.write(goal[:midpoint])
        sys.stdout.flush()
        sys.stdout.write(goal[midpoint:] + "\n")
        try:
            original_app()
        finally:
            print("event=s5_probe_finished")

    interface.app = diagnostic_app
    sys.argv = ["artemis.main", "--goal-file", path, "--unknown-s5-option"]
    main.cli()


def check(output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    identifier = f"che1051-{uuid.uuid4().hex}"
    sentinel = f"s5-password-{uuid.uuid4().hex}"
    credential = f"s5-credential-{uuid.uuid4().hex}"
    goal = f"Login with password={sentinel}"
    path = write_goal_file(goal, directory=output)
    repository = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "ARTEMIS_TASK_WORKER": "1",
        "ARTEMIS_SESSION_ID": identifier,
        "ARTEMIS_FAKE_LLM": "1",
        "OPENAI_API_KEY": credential,
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONPATH": str(repository),
    }
    command = [
        "systemd-cat",
        "--identifier",
        identifier,
        sys.executable,
        str(Path(__file__).resolve()),
        "--probe",
        path,
    ]
    try:
        worker = subprocess.run(
            command, cwd=repository, env=environment, capture_output=True, text=True, timeout=60
        )
        journal = subprocess.run(
            ["journalctl", "--identifier", identifier, "--output=json", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        records = [json.loads(line) for line in journal.stdout.splitlines() if line.strip()]
        messages = "\n".join(str(record.get("MESSAGE", "")) for record in records)
        leaked = any(
            value in journal.stdout + worker.stdout + worker.stderr
            for value in (sentinel, credential, goal)
        )
        controls = all(
            event in messages
            for event in (
                "s5_probe_started",
                "s5_probe_finished",
                "s5_goal_probe",
                "s5_configured_secret_probe",
                "s5_exception_probe",
                "RuntimeError",
            )
        )
        report = {
            "result": "PASS"
            if controls and not leaked and worker.returncode == 2 and not Path(path).exists()
            else "FAIL",
            "scope": "Real Artemis worker CLI with diagnostic fault injection; startup validation failure; no device/model execution",
            "identifier": identifier,
            "worker_returncode": worker.returncode,
            "journal_records": len(records),
            "all_control_events_present": controls,
            "sentinel_occurrences": journal.stdout.count(sentinel),
            "configured_credential_occurrences": journal.stdout.count(credential),
            "goal_occurrences": journal.stdout.count(goal),
            "goal_file_deleted": not Path(path).exists(),
            "sentinel_absent_from_argv": sentinel not in " ".join(command),
            "sentinel_sha256": hashlib.sha256(sentinel.encode()).hexdigest(),
        }
        if not leaked:
            (output / "worker-journal.jsonl").write_text(journal.stdout, encoding="utf-8")
        (output / "redaction-report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, indent=2))
        return 0 if report["result"] == "PASS" else 1
    finally:
        Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        probe(sys.argv[2])
    else:
        raise SystemExit(
            check(Path(sys.argv[1] if len(sys.argv) > 1 else "log-redaction-evidence").resolve())
        )
