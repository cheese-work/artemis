"""Static gate: adb is reached through ``EndpointTransport`` only (CHE-1094).

Every adb subprocess, adbutils client and uiautomator2 connection must come from
:mod:`artemis.runtime.endpoint_transport`, built from the run's explicit
endpoint. A bare call ignores the run's ``AdbTarget``: it talks to whatever the
process-global adbutils client, the ``adb`` default server or ``PATH`` resolve to,
which under a host agent is the wrong computer.

The rules (AST based, so comments and strings never trip them):

``adbutils-client``  ``AdbClient(...)``, ``adbutils.adb`` / ``adbutils.device``
``u2-connect``       ``uiautomator2.connect(...)`` / ``connect_usb(...)``
``adb-binary``       resolving or spawning the ``adb`` executable directly
``adb-command``      ``adb_command(...)`` / ``AdbSession(...)`` (use the transport)

``ALLOWLIST`` lists the files that still break a rule, with the reason. It may
only shrink: an entry whose file no longer breaks the rule fails the test, so
whoever migrates a call site must delete the line.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCAN_ROOTS = ("artemis", "apps", "mcp_server", "scripts")

#: The modules that own adb access.
OWNERS = frozenset(
    {
        "artemis/runtime/endpoint_transport.py",
        "artemis/runtime/adb_endpoint.py",
    }
)

#: file -> {rule: why it is still allowed}. Delete a line when its file is migrated.
ALLOWLIST: dict[str, dict[str, str]] = {
    "artemis/toolchain/descriptors.py": {
        "adb-binary": "Locates the adb executable (adbutils' bundled copy); transport.adb_binary() sits on top of it.",
    },
    "artemis/toolchain/resolver.py": {
        "adb-binary": "The resolver itself: find_adb() is how the toolchain names the adb path.",
    },
    "mcp_server/utils/device_utils.py": {
        "adb-binary": "Standalone MCP launcher: finds this computer's adb to list local devices and boot a local "
        "emulator. Host runs go through the admin queue, never this launcher.",
    },
}

_SUBPROCESS_FUNCS = {
    "run",
    "Popen",
    "check_output",
    "check_call",
    "call",
    "create_subprocess_exec",
    "create_subprocess_shell",
}


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_adb_literal(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value == "adb"


def violations(source: str) -> set[tuple[str, int]]:
    """``(rule, line)`` pairs for every bare adb access in ``source``."""
    tree = ast.parse(source)
    found: set[tuple[str, int]] = set()
    u2_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "uiautomator2"
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "adbutils":
            for alias in node.names:
                if alias.name in {"adb", "device"}:
                    found.add(("adbutils-client", node.lineno))
        if isinstance(node, ast.Attribute):
            owner = node.value
            if isinstance(owner, ast.Name) and owner.id == "adbutils":
                if node.attr in {"adb", "device"}:
                    found.add(("adbutils-client", node.lineno))
                if node.attr == "adb_path":
                    found.add(("adb-binary", node.lineno))
        if not isinstance(node, ast.Call):
            continue
        func = _name(node.func)
        first = node.args[0] if node.args else None
        if func == "AdbClient":
            found.add(("adbutils-client", node.lineno))
        elif func in {"connect", "connect_usb"} and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id in u2_names:
                found.add(("u2-connect", node.lineno))
        elif func in {"adb_command", "AdbSession"}:
            found.add(("adb-command", node.lineno))
        elif func in {"find_adb", "adb_binary_path"}:
            found.add(("adb-binary", node.lineno))
        elif func in {"resolve", "which"} and _is_adb_literal(first):
            found.add(("adb-binary", node.lineno))
        elif func in _SUBPROCESS_FUNCS:
            argv = first
            if _is_adb_literal(argv) or (
                isinstance(argv, (ast.List, ast.Tuple))
                and argv.elts
                and _is_adb_literal(argv.elts[0])
            ):
                found.add(("adb-binary", node.lineno))
            elif func in {"create_subprocess_exec", "create_subprocess_shell"} and _is_adb_literal(
                first
            ):
                found.add(("adb-binary", node.lineno))
    return found


def _scan() -> dict[str, set[tuple[str, int]]]:
    results: dict[str, set[tuple[str, int]]] = {}
    for root in SCAN_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            relative = path.relative_to(REPO).as_posix()
            if relative in OWNERS or "/." in f"/{relative}":
                continue
            found = violations(path.read_text(encoding="utf-8"))
            if found:
                results[relative] = found
    return results


SCAN = _scan()


def test_no_bare_adb_access_outside_the_allowlist():
    unexpected = {
        file: sorted(f"{rule}:{line}" for rule, line in hits if rule not in ALLOWLIST.get(file, {}))
        for file, hits in SCAN.items()
    }
    unexpected = {file: hits for file, hits in unexpected.items() if hits}

    assert not unexpected, (
        "Bare adb access ignores the run's endpoint. Route it through "
        "artemis.runtime.endpoint_transport.EndpointTransport:\n"
        + "\n".join(f"  {file}: {', '.join(hits)}" for file, hits in sorted(unexpected.items()))
    )


def test_allowlist_only_shrinks():
    stale = [
        f"{file}:{rule}"
        for file, rules in ALLOWLIST.items()
        for rule in rules
        if rule not in {r for r, _ in SCAN.get(file, set())}
    ]

    assert not stale, "Migrated or deleted; remove these ALLOWLIST entries: " + ", ".join(stale)


def test_allowlist_entries_carry_a_reason():
    assert all(reason.strip() for rules in ALLOWLIST.values() for reason in rules.values())


@pytest.mark.parametrize(
    ("source", "rules"),
    [
        ("from adbutils import AdbClient\nAdbClient()", {"adbutils-client"}),
        ("import adbutils\nadbutils.adb.device_list()", {"adbutils-client"}),
        ("from adbutils import adb", {"adbutils-client"}),
        ("import uiautomator2 as u2\nu2.connect('x')", {"u2-connect"}),
        ("import uiautomator2\nuiautomator2.connect_usb()", {"u2-connect"}),
        ("client = UIAutomatorClient('d')\nu2 = client\nu2.connect()", set()),
        ("subprocess.run(['adb', 'devices'])", {"adb-binary"}),
        ("await asyncio.create_subprocess_exec('adb', 'devices')", {"adb-binary"}),
        ("shutil.which('adb')", {"adb-binary"}),
        ("toolchain.resolve('adb')", {"adb-binary"}),
        ("find_adb()", {"adb-binary"}),
        ("adb_command(['devices'])", {"adb-command"}),
        ("AdbSession(endpoint)", {"adb-command"}),
        ("subprocess.run(['ffmpeg', '-i', 'adb'])", set()),
        ("toolchain.resolve('ffmpeg')", set()),
        ("# adb devices\nx = 'adb'", set()),
        ("transport.run(['devices'])", set()),
    ],
)
def test_the_lint_recognises_bare_access(source, rules):
    assert {rule for rule, _line in violations(source)} == rules
