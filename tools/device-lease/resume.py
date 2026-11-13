"""DLP-1 Multica edge. All subprocess/network calls run outside broker transactions."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from broker import Refused


class MulticaClient:
    """CLI adapter; only the owner-side instance receives the ops profile name.

    Ops provisions and owns that profile on X99. No credential is passed to RPC
    clients. The default instance can register waits/comment, but cannot signal.
    """

    def __init__(self, *, profile=None, audit_path=None):
        self.profile = profile
        if profile is not None:
            if not isinstance(profile, str) or not profile.strip() or audit_path is None:
                raise ValueError("explicit ops profile and capability audit required")
            event = {
                "kind": "capability-change",
                "at": datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S ICT"),
                "capability": "Device offer property writer enabled",
                "profile": profile,
            }
            # Do not log credentials, subprocess output or the profile's contents.
            with Path(audit_path).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event) + "\n")

    def _call(self, *args):
        command = ["multica"]
        if self.profile is not None:
            command += ["--profile", self.profile]
        done = subprocess.run(
            command + list(args), capture_output=True, text=True, check=True, timeout=10
        )
        return json.loads(done.stdout) if done.stdout.strip() else None

    def set_offer(self, issue, property_id, value):
        if self.profile is None:
            raise RuntimeError("offer writes require the dedicated ops profile")
        self._call(
            "issue",
            "property",
            "set",
            issue,
            "--name",
            property_id,
            "--value",
            value,
            "--output",
            "json",
        )

    def read_offer(self, issue, property_id):
        values = self._call("issue", "property", "list", issue, "--output", "json")
        return next((v["value"] for v in values if v["property_id"] == property_id), None)

    def create_wakeup(self, issue, property_id, value):
        result = self._call(
            "issue",
            "wakeup",
            "create",
            issue,
            "--until-property",
            f"{property_id}={value}",
            "--expires-in",
            "60m",
            "--on-timeout",
            "wake",
            "--output",
            "json",
        )
        return result["id"]

    def comment(self, issue, text):
        # Multica requires a UTF-8 file inside the caller's workdir. Keep on failure.
        fd, name = tempfile.mkstemp(prefix=".device-wait-", suffix=".md", dir=Path.cwd())
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text + "\n")
        self._call("issue", "comment", "add", issue, "--content-file", name, "--output", "json")
        Path(name).unlink()

    def checkin(self, issue, wakeup_id, text):
        self._call("issue", "wakeup", "checkin", issue, wakeup_id, "--note", text)


class OfferWorker:
    """Foreground owner-loop step: snapshot, write, read back, then acknowledge.

    ponytail: retry backoff is process-local; a restart retries once immediately.
    Durable offers and sequences make that retry safe, without another job system.
    """

    def __init__(self, broker, client, property_id, *, monotonic=time.monotonic):
        self.broker, self.client, self.property_id = broker, client, property_id
        self.monotonic = monotonic
        self.retry = {}

    def run_once(self):
        offers = self.broker.pending_offers()
        current = {(t["ticket_id"], t["offer_sequence"]) for t in offers}
        self.retry = {k: v for k, v in self.retry.items() if k in current}
        for ticket in offers:
            key = ticket["ticket_id"], ticket["offer_sequence"]
            attempt, next_try = self.retry.get(key, (0, 0))
            if self.monotonic() < next_try:
                continue
            value = f"{key[0]}:{key[1]}"
            try:
                self.client.set_offer(ticket["issue_id"], self.property_id, value)
                if self.client.read_offer(ticket["issue_id"], self.property_id) != value:
                    raise ValueError("offer read-back mismatch")
                self.broker.start_offer_window(*key)
            except (OSError, ValueError, subprocess.SubprocessError, Refused):
                self.retry[key] = min(attempt + 1, 7), self.monotonic() + min(60 * 2**attempt, 3600)
            else:
                self.retry.pop(key, None)


def fallback_wait(
    broker, client, property_id, *, ticket_id, principal, agent_id, issue_id, wakeup_block=""
):
    """One timeout run: poll/refresh, register the next condition, post one line.

    The caller passes only the actual run's [WAKEUP] block, never issue history.
    An offered/cancelled ticket returns a next action for this same run instead.
    """
    ticket = broker.ticket_status(
        ticket_id=ticket_id, principal=principal, agent_id=agent_id, issue_id=issue_id
    )
    if ticket["state"] != "queued":
        return {
            "action": "claim" if ticket["state"] == "offered" else "reacquire",
            "ticket": ticket,
        }
    value = f"{ticket_id}:{ticket['offer_sequence'] + 1}"
    wakeup = client.create_wakeup(issue_id, property_id, value)
    text = (
        f"Device ticket {ticket_id} still queued (position {ticket['position']}); "
        f"next wakeup {wakeup}, timeout 60 min."
    )
    block = re.fullmatch(r"\s*\[WAKEUP\]\s*\n(.*?)\n\[/WAKEUP\]\s*", wakeup_block, re.S)
    checkin = (
        re.search(
            r"^multica issue wakeup checkin "
            + re.escape(issue_id)
            + r" ([A-Za-z0-9-]+)(?: --note .*)?$",
            block[1],
            re.M,
        )
        if block
        else None
    )
    if checkin:
        client.checkin(issue_id, checkin[1], text)
    else:
        client.comment(issue_id, text)
    return {"action": "wait", "ticket": ticket, "wakeup_id": wakeup}
