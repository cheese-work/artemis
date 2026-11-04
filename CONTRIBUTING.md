# Contributing to ARTEMIS

Thank you for your interest in contributing to **ARTEMIS**! We welcome and appreciate all forms of contributions—whether it's reporting bugs, proposing new features, improving documentation, or submitting code.

---

## 🤝 Ways to Contribute

- **🐛 Report Bugs & Suggest Features**: If you find an issue or have an idea to improve ARTEMIS, please open an [Issue](https://github.com/google/artemis/issues).
- **💡 Submit Pull Requests**:
  1. Fork the repository and create your feature/fix branch (`git checkout -b feat/my-feature`).
  2. Make your changes and verify with `make test`, `make lint`, and `make typecheck`.
  3. Open a Pull Request with a brief summary of your work.
- **💬 Community & Discussion**: Join our [Discord Community](https://discord.gg/wF2FN4WHGY) to chat with maintainers, ask questions, or share feedback.

---

Every contribution—big or small—helps make ARTEMIS better. Thank you for building with us!

## Test layers

`make test` is the required, deterministic suite. It does not need an Android
device, model credentials, or the optional private cloud service. This is the
same suite used by pull-request CI.

- `make test-integration` runs cross-component tests that may need configured
  model credentials.
- `make test-device` runs Android and end-to-end tests and therefore requires
  an attached, authorized device or emulator.
- `make test-all` collects every test tree and is intended for a fully
  provisioned maintainer environment.

Tests that require external state must carry the appropriate `android`,
`cloud`, `manual`, or `e2e` marker. They must remain safely collectable when
that dependency is unavailable.

## Writing adb code

Reach adb only through `artemis.runtime.endpoint_transport.EndpointTransport`,
built from an explicit `AdbEndpoint` (or `EndpointTransport.shared(None)` for the
process's own server). It owns every adb subprocess, adbutils client and
uiautomator2 connection, so a run bound to one adb server cannot silently reach
another one. `tests/unit/runtime/test_endpoint_transport_lint.py` fails on a bare
`AdbClient(...)`, `u2.connect(...)`, `adb_command(...)` or direct `adb` spawn; its
`ALLOWLIST` may only shrink.

Test adb code against the fake adb server, never a real device:

```python
def test_something(fake_adb_server_factory):
    server = fake_adb_server_factory("alpha")
    server.add_device("emulator-5554")
    transport = EndpointTransport(server.endpoint)
```

`tests/support/fake_adb.py` speaks the adb smart-socket protocol well enough for
the pinned `adb`, adbutils and uiautomator2, and records every request.
`tests/support/golden/adb_protocol.json` holds the requests those clients send;
after a deliberate client upgrade regenerate it with
`ARTEMIS_UPDATE_ADB_FIXTURES=1 uv run pytest tests/unit/runtime/test_adb_protocol_golden.py`
and review the diff.

## What's New entries

SmartQA's What's New page is built from one small file per change, so parallel
and stacked PRs never edit the same file. `npm run build` (its `prebuild` step)
checks every entry and combines them, newest first, into
`apps/showcase_ui/public/whats-new.json`. That file is generated and not
committed. A malformed entry fails the build.

**When.** Add an entry in the same PR as any change QAs can see or feel: a new
screen or control, changed wording or layout, a fixed bug they could hit. This
includes backend changes with a visible effect (for example a recorder fix);
CI cannot detect those, so the author decides.

**Where and format.** One file,
`apps/showcase_ui/whats-new/entries/<YYYY-MM-DD>-<slug>.json`:

```json
{
  "id": "2026-10-08-long-agent-reports",
  "date": "2026-10-08",
  "title": "Long agent reports are easier to read",
  "body": "A failed run now shows a short summary. Open Agent report to read the full report.",
  "issues": ["CHE-1351"]
}
```

- `id` equals the file name without `.json`; the file name starts with `date`.
- `title`: one plain sentence. `body` (optional): at most 3 short lines.
- `issues` (optional): related issue numbers like `CHE-1351`. The page shows
  them as plain text, never as links.
- No other keys.

**Writing rule.** Write for QAs, not engineers: say what they can now do or
what looks different. No internal detail: no PR numbers, branch names, hosts,
agent names, file paths or security internals.

**The `no-whats-new` label.** CI fails a PR that changes
`apps/showcase_ui/src/` (except `*.spec.ts`) and adds no entry file. Add the
`no-whats-new` label instead of an entry only when QAs will not notice the
change: a refactor, tests only, or internal tooling.

Tests: `npm run test:whats-new` in `apps/showcase_ui`.
