"""Integration-fixture teardown must never delete resources it did not create (CHE-1291)."""

import pytest

from tests.integration.preview_host.owned import Owned


class FakeDocker:
    def __init__(self, fail_on: str | None = None):
        self.calls: list[tuple[str, ...]] = []
        self.fail_on = fail_on

    def __call__(self, *args: str, stdin: str | None = None, check: bool = True) -> str:
        self.calls.append(args)
        if self.fail_on and args[:2] == tuple(self.fail_on.split()):
            raise RuntimeError("name already in use")
        return "sha256:abc" if args[:2] == ("image", "inspect") else f"id-{len(self.calls)}"


def test_failed_network_create_leaves_everything_else_alone():
    docker = FakeDocker(fail_on="network create")
    owned = Owned(docker)
    with pytest.raises(RuntimeError):
        owned.network(["docker", "network", "create", "artemis-preview-pr1"])
    owned.cleanup()
    assert docker.calls == [("network", "create", "artemis-preview-pr1")]  # no rm of any kind


def test_cleanup_removes_only_recorded_ids_in_reverse_order():
    docker = FakeDocker()
    owned = Owned(docker)
    net = owned.network(["docker", "network", "create", "n"])
    box = owned.container(["docker", "create", "--name", "c", "img"])
    docker.calls.clear()
    owned.cleanup()
    assert docker.calls == [("rm", "-f", "-v", box), ("network", "rm", net)]


def test_failed_container_create_removes_only_the_network_that_was_created():
    docker = FakeDocker(fail_on="create --name")
    owned = Owned(docker)
    net = owned.network(["docker", "network", "create", "n"])
    with pytest.raises(RuntimeError):
        owned.container(["docker", "create", "--name", "taken", "img"])
    docker.calls.clear()
    owned.cleanup()
    assert docker.calls == [("network", "rm", net)]
