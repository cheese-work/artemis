"""Trusted broker/R1 composition. Never registered on the cooperative holder RPC."""

import asyncio
import hashlib
import json
import sqlite3

from remote_attestation import attest, verified


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


class BrokerAdmission:
    def __init__(self, broker, *, authenticate, generation, inspect):
        self.broker = broker
        self.authenticate, self.generation, self.inspect = authenticate, generation, inspect

    def __call__(self, identity):
        """authenticate checks the external principal AND the Multica live run.

        inspect reads owner-controlled grants, a reviewed closure profile, frozen
        pre-lease baseline and effective isolation evidence. Unknown/unavailable
        reads raise or return None. All inspection is outside writer transactions.
        """
        try:
            if self.authenticate(identity) is not True:
                return None
            generation = self.generation()
            group = self.broker.status(
                lease_id=identity["lease"],
                principal=identity["principal"],
                run_id=identity["run"],
            )
            if group["state"] != "active" or group["binding"].get("mode") != "remote":
                return None
            with sqlite3.connect(self.broker.db_path) as conn:
                context = conn.execute(
                    "SELECT agent,issue FROM broker_context WHERE lease_id=?", (identity["lease"],)
                ).fetchone()
            if context != (identity["agent"], identity["issue"]):
                return None
            mappings, profiles, baselines, isolations = [], [], [], []
            for device in group["devices"]:
                canonical, serial = device["device_id"], device["endpoint"]
                policy = self.inspect(canonical)
                registered = self.broker.registry.resolve(serial)
                if (
                    registered is None
                    or registered.device_id != canonical
                    or device["restricted"] is not False
                    or not self.broker.registry.eligible(serial, policy["state"])
                    or policy["granted"] is not True
                    or policy["serial"] != serial
                    or not isinstance(policy["revision"], str)
                    or not policy["revision"]
                    or not isinstance(policy["isolation"], str)
                    or not policy["isolation"]
                ):
                    return None
                baseline = attest(policy["profile"], policy["baseline"])
                if baseline is None:
                    return None
                mappings.append((canonical, serial, policy["revision"]))
                profiles.append((canonical, digest(policy["profile"])))
                baselines.append((canonical, baseline))
                isolations.append((canonical, policy["isolation"]))
            # Reject a control/lease change during potentially external policy reads.
            if (
                generation != self.generation()
                or self.authenticate(identity) is not True
                or self.broker.status(
                    lease_id=identity["lease"],
                    principal=identity["principal"],
                    run_id=identity["run"],
                )
                != group
            ):
                return None
            return dict(
                identity,
                devices=tuple(mappings),
                expires=group["expires_at"],
                hard_deadline=group["hard_deadline"],
                generation=generation,
                state="active",
                granted=True,
                restricted=False,
                live=True,
                profile=digest(profiles),
                baseline=digest(baselines),
                isolation=digest(isolations),
            )
        except Exception:
            return None


class RemoteRecovery:
    """Compose into Broker.recovery_io; invoke from the broker's worker thread.

    owned.fence closes/stops only run-owned work and forwards with start identities.
    inspection is the independent AFTER-cleanup verifier, never the holder shell.
    Neither this adapter nor the broker grants reuse without leader verification.
    """

    def __init__(self, access, loop, owned, *, inspection):
        self.access, self.loop, self.owned = access, loop, owned
        self.inspection = inspection

    def fence(self, group, *, timeout):
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            raise RuntimeError("Recovery must run outside the gateway event loop")
        future = asyncio.run_coroutine_threadsafe(
            self.access.fence_lease(group["lease_id"]), self.loop
        )
        try:
            if future.result(timeout=timeout) is not True:
                return False
        except Exception:
            future.cancel()
            return False
        return self.owned.fence(group, timeout=timeout) is True

    def undo(self, group, change, *, timeout):
        return self.owned.undo(group, change, timeout=timeout)

    def verify_state(self, group, *, timeout):
        try:
            # Fencing retains the admission snapshots. Never re-learn a baseline
            # from release-time inspection or from the now-inactive live resolver.
            admitted = []
            with self.access.db() as conn:
                for row in conn.execute("SELECT identity,snapshot,phase FROM r1_keys"):
                    identity = json.loads(row["identity"])
                    if all(
                        identity[k] == group[v]
                        for k, v in (
                            ("lease", "lease_id"),
                            ("principal", "holder"),
                            ("run", "run_id"),
                        )
                    ):
                        frozen = json.loads(row["snapshot"])
                        if row["phase"] != "closed" or any(
                            frozen[k] != value for k, value in identity.items()
                        ):
                            return False
                        admitted.append(frozen)
            if not admitted:
                return False
            devices = {d["device_id"]: d["endpoint"] for d in group["devices"]}
            evidence = {}
            for canonical in devices:
                item = self.inspection(group, canonical, timeout=timeout)
                if not verified(item["profile"], item["baseline"], item["observed"]):
                    return False
                evidence[canonical] = (
                    digest(item["profile"]),
                    attest(item["profile"], item["baseline"]),
                )
            # Every rotation of this run/lease must agree with the frozen evidence.
            for frozen in admitted:
                if {d[0]: d[1] for d in frozen["devices"]} != devices:
                    return False
                profiles = [(d[0], evidence[d[0]][0]) for d in frozen["devices"]]
                baselines = [(d[0], evidence[d[0]][1]) for d in frozen["devices"]]
                if digest(profiles) != frozen["profile"] or digest(baselines) != frozen["baseline"]:
                    return False
            return True
        except Exception:
            return False
