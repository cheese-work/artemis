"""Remove only the Docker objects this test run created (CHE-1291).

Resources are recorded by ID at creation and removed by ID. A create that fails
records nothing, so teardown never touches a container, network or image that a
concurrent or earlier run already owns under the same name.
"""

from collections.abc import Callable


class Owned:
    def __init__(self, docker: Callable[..., str]) -> None:
        self.docker = docker
        self.created: list[tuple[str, str]] = []

    def _record(self, kind: str, output: str) -> str:
        self.created.append((kind, output))
        return output

    def network(self, argv: list[str]) -> str:
        return self._record("network", self.docker(*argv[1:]))

    def container(self, argv: list[str]) -> str:
        return self._record("container", self.docker(*argv[1:]))

    def image(self, tag: str, dockerfile: str) -> str:
        self.docker("build", "-q", "-t", tag, "-", stdin=dockerfile)
        return self._record("image", self.docker("image", "inspect", "-f", "{{.Id}}", tag))

    def cleanup(self) -> None:
        remove = {
            "container": ("rm", "-f", "-v"),  # -v also drops anonymous volumes
            "network": ("network", "rm"),
            "image": ("rmi", "-f"),
        }
        for kind, ident in reversed(self.created):
            self.docker(*remove[kind], ident, check=False)
        self.created.clear()
