"""Offline cross-repo fixture called by Multica's TestBindingNestedNamespace.

Only a synthetic inventory and a temporary DB are used. Never calls ADB,
recovery I/O, an installed broker, or a Multica service.
"""

import threading
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools/device-lease"))
from broker import Broker, Device, DeviceRegistry
from managed_runs import ManagedRuns


def main():
    phase, directory, binding_id, database = sys.argv[1:]
    registry = DeviceRegistry(
        [Device("fixture", ("fixture-usb",), restricted=False, identity_confirmed=True)]
    )
    guard = threading.RLock()
    broker = Broker(
        database,
        registry,
        admitted={"fixture": "fixture-usb"},
        managed_runs=ManagedRuns(directory),
        admission_sources={"artemis": lambda: [], "android": lambda: []},
        admission_guard=lambda: guard,
    )
    if phase == "active":
        record = broker.managed_runs.resolve(
            binding_id, principal="agent", agent_id="agent", issue_id="CHE-fixture", run_id="run-1"
        )
        assert record["pid"] > 1
        group = broker.acquire(
            principal="agent",
            agent_id="agent",
            issue_id="CHE-fixture",
            run_id="run-1",
            purpose="CHE-fixture",
            idempotency_key="contract",
            binding={"mode": "managed", "binding_id": binding_id},
        )
        assert group["state"] == "active"
        Path(database + ".lease").write_text(group["lease_id"])
        print("HOST_CONTRACT_ACTIVE: launcher record resolved; synthetic group active")
    else:
        assert phase == "revoked"
        broker.maintain()
        group = broker.status(
            lease_id=Path(database + ".lease").read_text(), principal="agent", run_id="run-1"
        )
        assert group["state"] == "fencing"
        print("HOST_CONTRACT_REVOKED: synthetic group fenced")


if __name__ == "__main__":
    main()
