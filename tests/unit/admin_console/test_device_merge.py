"""CHE-1480 stage 1: device Merge and Keep separate (repository and admission serialization).

Contract: docs/device-identity.md, "Merge, Keep separate and Split".
"""

import json
import sqlite3
import threading

import pytest

from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.database.repositories.device_repository import DeviceRepository
from apps.admin_console.database.repositories.principal_repository import PrincipalRepository
from apps.admin_console.services import device_merge
from apps.admin_console.services.device_reservation import device_reservations
from artemis.data_engine.storage import StorageManager
from artemis.runtime.adb_endpoint import AdbEndpoint, AdbTarget
from artemis.runtime.run_device_binding import RunDeviceBinding

ISSUER = "https://team.cloudflareaccess.com"
HOST = "host-1"


@pytest.fixture(autouse=True)
def host_agent(monkeypatch):
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "1")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "data_engine.db"
    StorageManager(path, tmp_path)
    return path


@pytest.fixture
def repo(db):
    return DeviceRepository(db)


@pytest.fixture
def people(db):
    principals = PrincipalRepository(db)
    return {
        name: principals.ensure_user(ISSUER, name, f"{name}@example.test").id
        for name in ("alice", "bob", "carol")
    }


def _phone(repo, owner, serial, *, state="uncertain", hardware_hash=None):
    """A device with one host connection, as discovery leaves it."""
    device = repo.create_device(
        owner_principal_id=owner, label=serial, hardware_hash=hardware_hash, match_state=state
    )
    connection_id = repo.upsert_connection(
        device.device_id, kind="usb", source="host", host_id=HOST, serial=serial
    )
    return device.device_id, connection_id


def _events(db, kind):
    with sqlite3.connect(db) as conn:
        return [
            json.loads(row[0])
            for row in conn.execute("SELECT payload FROM device_events WHERE kind = ?", (kind,))
        ]


def _claim(repo, serial, session_id):
    """A run claim on the phone behind host connection ``serial``."""
    binding = RunDeviceBinding(
        AdbTarget(AdbEndpoint.create("127.0.0.1", 15037, host_id=HOST), serial, HOST)
    )
    return device_reservations.claim(binding, repo.db_path, session_id)


def _code(excinfo):
    return excinfo.value.code


def test_merge_across_owners_is_refused(repo, people, db):
    a, _ = _phone(repo, people["alice"], "a")
    b, _ = _phone(repo, people["bob"], "b")

    with pytest.raises(AdminAPIError) as refused:
        device_merge.merge(repo, a, b, actor_principal_id=people["alice"], now=1.0)

    assert _code(refused) == "device_identity_unresolved"
    assert repo.resolve(b) == b and _events(db, "merged") == []


@pytest.mark.parametrize("action", [device_merge.merge, device_merge.keep_separate])
def test_a_non_owner_is_refused(repo, people, db, action):
    a, _ = _phone(repo, people["alice"], "a")
    b, _ = _phone(repo, people["alice"], "b")

    with pytest.raises(AdminAPIError) as refused:
        action(repo, a, b, actor_principal_id=people["carol"], now=1.0)

    assert _code(refused) == "device_not_yours" and refused.value.status_code == 403
    assert repo.resolve(b) == b and repo.get(a).match_state == "uncertain"


def test_merge_joins_the_record_and_the_old_id_resolves_to_the_survivor(repo, people, db):
    alice = people["alice"]
    survivor, _ = _phone(repo, alice, "survivor")
    joined, joined_connection = _phone(repo, alice, "joined", hardware_hash="hw")

    merged = device_merge.merge(repo, survivor, joined, actor_principal_id=alice, now=5.0)

    assert merged.device_id == survivor and merged.match_state == "confirmed"
    assert merged.hardware_hash == "hw"  # the survivor learns the identity the joined record held
    assert repo.resolve(joined) == survivor
    assert repo.get(joined).device_id == survivor
    # the run snapshot's connection_id keeps working and now points at the survivor
    assert repo.connection(connection_id=joined_connection).device_id == survivor
    assert _events(db, "merged") == [
        {
            "device_id": survivor,
            "other_device_id": joined,
            "survivor": survivor,
            "actor_principal_id": alice,
        }
    ]


def test_merge_takes_aliased_ids_and_never_merges_a_device_with_itself(repo, people):
    alice = people["alice"]
    survivor, _ = _phone(repo, alice, "survivor")
    first, _ = _phone(repo, alice, "first")
    second, _ = _phone(repo, alice, "second")
    device_merge.merge(repo, survivor, first, actor_principal_id=alice, now=1.0)

    with pytest.raises(ValueError):
        device_merge.merge(repo, survivor, first, actor_principal_id=alice, now=2.0)
    device_merge.merge(repo, first, second, actor_principal_id=alice, now=3.0)  # via the alias

    assert repo.resolve(second) == survivor


def test_two_hardware_identities_are_two_phones(repo, people):
    alice = people["alice"]
    a, _ = _phone(repo, alice, "a", hardware_hash="hw-a")
    b, _ = _phone(repo, alice, "b", hardware_hash="hw-b")

    with pytest.raises(ValueError):
        device_merge.merge(repo, a, b, actor_principal_id=alice, now=1.0)

    assert repo.resolve(b) == b


@pytest.mark.parametrize(
    "claimed_serial,first,second", [("a", "a", "b"), ("b", "a", "b"), ("b", "b", "a")]
)
def test_actions_are_refused_while_either_device_is_claimed(
    repo, people, db, claimed_serial, first, second
):
    alice = people["alice"]
    ids = {}
    ids["a"], _ = _phone(repo, alice, "a", state="confirmed")
    ids["b"], _ = _phone(repo, alice, "b", state="confirmed")
    _claim(repo, claimed_serial, "run-1")

    for action in (device_merge.merge, device_merge.keep_separate):
        with pytest.raises(AdminAPIError) as refused:
            action(repo, ids[first], ids[second], actor_principal_id=alice, now=1.0)
        assert _code(refused) == "device_claimed"

    assert repo.resolve(ids["b"]) == ids["b"]
    assert _events(db, "merged") == [] and _events(db, "kept_separate") == []
    device_reservations.release("run-1")
    device_merge.merge(repo, ids[first], ids[second], actor_principal_id=alice, now=2.0)


def test_a_concurrent_merge_and_claim_never_double_book(repo, people):
    alice = people["alice"]
    for round_ in range(15):
        survivor, _ = _phone(repo, alice, f"s{round_}", state="confirmed")
        joined, _ = _phone(
            repo, alice, f"j{round_}", state="confirmed", hardware_hash=f"hw{round_}"
        )
        outcome = {}

        def merge():
            try:
                outcome["merge"] = device_merge.merge(
                    repo, survivor, joined, actor_principal_id=alice, now=1.0
                )
            except AdminAPIError as refused:
                outcome["merge"] = refused.code

        def claim():
            outcome["claim"] = _claim(repo, f"j{round_}", f"run-{round_}")

        threads = [threading.Thread(target=merge), threading.Thread(target=claim)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        if outcome["merge"] == "device_claimed":  # the claim won: the joined phone stays its own
            assert repo.resolve(joined) == joined
            assert outcome["claim"].key == f"device:{joined}"
        else:  # the merge won: the claim landed on the survivor, never on the merged-away id
            assert repo.resolve(joined) == survivor
            assert outcome["claim"].key == f"device:{survivor}"
        device_reservations.release(f"run-{round_}")


def test_keep_separate_confirms_both_and_the_pair_is_not_offered_again(repo, people, db):
    alice = people["alice"]
    held, _ = _phone(repo, alice, "held", state="confirmed", hardware_hash="hw")
    other, other_connection = _phone(repo, alice, "other", state="uncertain")

    def later_observation(device_serial):
        return repo.match_connection(
            kind="usb",
            source="host",
            host_id=HOST,
            serial=device_serial,
            hardware_hash="hw",
            readable=True,
        )

    device_merge.keep_separate(repo, other, held, actor_principal_id=alice, now=3.0)

    assert repo.get(other).match_state == "confirmed" and repo.get(held).match_state == "confirmed"
    match = later_observation("other")
    assert (match.device_id, match.outcome) == (other, "confirmed")
    assert repo.resolve(held) == held and repo.resolve(other) == other
    assert _events(db, "kept_separate") == [
        {
            "device_id": other,
            "other_device_id": held,
            "survivor": None,
            "actor_principal_id": alice,
        }
    ]
    # control: without the decision the same observation flags the pair again
    stranger, _ = _phone(repo, alice, "stranger", state="confirmed")
    assert later_observation("stranger").outcome == "uncertain"


def test_keep_separate_is_idempotent(repo, people, db):
    alice = people["alice"]
    a, _ = _phone(repo, alice, "a")
    b, _ = _phone(repo, alice, "b")

    device_merge.keep_separate(repo, a, b, actor_principal_id=alice, now=1.0)
    device_merge.keep_separate(repo, b, a, actor_principal_id=alice, now=2.0)

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM device_non_matches").fetchone() == (1,)
    assert len(_events(db, "kept_separate")) == 2


def test_a_kept_separate_pair_survives_a_merge_of_either_side(repo, people, db):
    alice = people["alice"]
    held, _ = _phone(repo, alice, "held", state="confirmed", hardware_hash="hw")
    other, _ = _phone(repo, alice, "other", state="uncertain")
    device_merge.keep_separate(repo, other, held, actor_principal_id=alice, now=1.0)
    # `other` is merged away into `survivor`; the decision about `other` follows it
    survivor, _ = _phone(repo, alice, "survivor", state="confirmed")
    device_merge.merge(repo, survivor, other, actor_principal_id=alice, now=2.0)

    match = repo.match_connection(
        kind="usb",
        source="host",
        host_id=HOST,
        serial="survivor",
        hardware_hash="hw",
        readable=True,
    )

    assert repo.resolve(other) == survivor
    assert match.outcome == "confirmed" and match.device_id == survivor
    assert repo.resolve(held) == held  # still its own phone: the pair was not offered again


def test_merging_the_two_sides_of_a_kept_pair_drops_the_decision(repo, people, db):
    alice = people["alice"]
    a, _ = _phone(repo, alice, "a")
    b, _ = _phone(repo, alice, "b")
    device_merge.keep_separate(repo, a, b, actor_principal_id=alice, now=1.0)

    device_merge.merge(
        repo, a, b, actor_principal_id=alice, now=2.0
    )  # the owner changed their mind

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM device_non_matches").fetchone() == (0,)
