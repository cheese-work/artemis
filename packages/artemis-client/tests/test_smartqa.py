from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from typing import Any

from artemis_client import ArtemisClient, ConflictError
from artemis_client.smartqa import (
    CheckError,
    CheckOptions,
    load_manifest,
    load_route,
    run_check,
)
from artemis_client.smartqa_cli import main as cli_main

APK_BYTES = b"PK\x03\x04fake-apk"
APK_SHA = hashlib.sha256(APK_BYTES).hexdigest()
MANIFEST = """\
package: com.example.sample
backend: staging
test_accounts:
  - tester-one
screens:
  - id: home
    purpose: Landing screen with the demo list
  - id: pager
    purpose: Pager demo
"""
ROUTE = {
    "steps": [
        {"action": "tap", "text": "native pool"},
        {"action": "back"},
        {"action": "type", "id": "pw", "value": "${secret:TEST_LOGIN_PASSWORD}", "secret": True},
        {"action": "swipe", "direction": "left"},
    ]
}


class FakeTransport:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], list[Any]] = defaultdict(list)
        self.calls: list[tuple[str, str, Any, bytes | None]] = []

    def add(self, method: str, path: str, *responses: Any) -> None:
        self.responses[(method, path)].extend(responses)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: bytes | None = None,
        content_type: str | None = None,
        headers: Any = None,
    ) -> Any:
        self.calls.append((method, path, json_body, data))
        queued = self.responses[(method, path)]
        if not queued:
            raise AssertionError(f"Unexpected request: {method} {path}")
        response = queued.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def paths(self) -> list[str]:
        return [f"{method} {path}" for method, path, _, _ in self.calls]


class SmartQATestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.apk = self.dir / "app.apk"
        self.apk.write_bytes(APK_BYTES)
        self.manifest = self.dir / "smartqa.yml"
        self.manifest.write_text(MANIFEST)
        self.route = self.dir / "route.json"
        self.route.write_text(json.dumps(ROUTE))
        self.transport = FakeTransport()
        self.client = ArtemisClient(
            "https://artemis.example.test", poll_interval=0.001, transport=self.transport
        )

    def options(self, **overrides: Any) -> CheckOptions:
        values: dict[str, Any] = {
            "apk": self.apk,
            "manifest": self.manifest,
            "themes": ("light", "dark"),
        }
        values.update(overrides)
        return CheckOptions(**values)

    def queue_happy_path(self, themes: int = 2) -> None:
        t = self.transport
        t.add(
            "POST",
            "/api/smartqa/apks",
            {"sha256": APK_SHA, "size_bytes": len(APK_BYTES), "path": "/srv/a.apk"},
        )
        for index in range(themes):
            lease = f"lease-{index}"
            task = f"00000000-0000-4000-8000-00000000000{index}"
            t.add("POST", "/api/leases", {"lease_id": lease, "device_id": f"emulator-{index}"})
            t.add(
                "POST",
                "/api/run",
                {"status": "queued", "tasks": [{"session_id": task, "status": "queued"}]},
            )
            t.add("GET", f"/api/sessions/{task}", {"session_id": task, "status": "completed"})
            t.add("POST", f"/api/leases/{lease}/release", {"status": "released"})


class ManifestAndRouteTests(SmartQATestCase):
    def test_manifest_lists_declared_screens_and_purpose(self) -> None:
        manifest = load_manifest(self.manifest)
        self.assertEqual(manifest.package, "com.example.sample")
        self.assertEqual([s.id for s in manifest.screens], ["home", "pager"])
        self.assertEqual(manifest.screens[1].purpose, "Pager demo")
        self.assertEqual(manifest.test_accounts, ("tester-one",))

    def test_production_backend_is_refused(self) -> None:
        self.manifest.write_text(MANIFEST.replace("staging", "production"))
        with self.assertRaises(CheckError) as caught:
            load_manifest(self.manifest)
        self.assertEqual(caught.exception.code, "prod_backend_refused")

    def test_route_with_literal_secret_is_refused(self) -> None:
        bad = {"steps": [{"action": "type", "id": "pw", "value": "hunter2", "secret": True}]}
        self.route.write_text(json.dumps(bad))
        with self.assertRaises(CheckError) as caught:
            load_route(self.route)
        self.assertEqual(caught.exception.code, "route_secret_literal")

    def test_route_accepts_placeholder_secret_and_plain_steps(self) -> None:
        steps = load_route(self.route)
        self.assertEqual([s.action for s in steps], ["tap", "back", "type", "swipe"])

    def test_route_rejects_unknown_action_and_missing_target(self) -> None:
        for steps in ([{"action": "teleport"}], [{"action": "tap"}], [{"action": "swipe"}]):
            self.route.write_text(json.dumps({"steps": steps}))
            with self.assertRaises(CheckError) as caught:
                load_route(self.route)
            self.assertEqual(caught.exception.code, "route_invalid")


class RunCheckTests(SmartQATestCase):
    async def test_persona_run_uploads_then_leases_runs_and_releases_per_theme(self) -> None:
        self.queue_happy_path()
        result = await run_check(self.client, self.options(pool="x99-avd"))
        self.assertTrue(result.ok)
        self.assertEqual(result.apk_sha256, APK_SHA)
        self.assertEqual([r.theme for r in result.themes], ["light", "dark"])
        self.assertEqual(
            self.transport.paths(),
            [
                "POST /api/smartqa/apks",
                "POST /api/leases",
                "POST /api/run",
                "GET /api/sessions/00000000-0000-4000-8000-000000000000",
                "POST /api/leases/lease-0/release",
                "POST /api/leases",
                "POST /api/run",
                "GET /api/sessions/00000000-0000-4000-8000-000000000001",
                "POST /api/leases/lease-1/release",
            ],
        )
        lease_body = self.transport.calls[1][2]
        self.assertEqual(lease_body["pool"], "x99-avd")
        run_body = self.transport.calls[2][2]
        smartqa = run_body["options"]["smartqa"]
        self.assertEqual(smartqa["theme"], "light")
        self.assertTrue(smartqa["fresh_install"])
        self.assertEqual(smartqa["persona"], "first-time-user")
        self.assertEqual(smartqa["mode"], "persona")
        self.assertEqual(run_body["locked_app_package"], "com.example.sample")
        self.assertEqual(run_body["app_path"], "/srv/a.apk")
        self.assertEqual(run_body["device_serial"], "emulator-0")
        self.assertLessEqual(smartqa["max_model_calls"], 80)

    async def test_route_mode_sends_steps_and_skips_the_persona(self) -> None:
        self.queue_happy_path(themes=1)
        await run_check(self.client, self.options(themes=("light",), route=self.route))
        smartqa = self.transport.calls[2][2]["options"]["smartqa"]
        self.assertEqual(smartqa["mode"], "route")
        self.assertEqual(len(smartqa["route"]), 4)
        self.assertEqual(smartqa["max_model_calls"], 0)
        self.assertNotIn("persona", smartqa)

    async def test_lease_is_released_when_the_run_fails(self) -> None:
        t = self.transport
        t.add(
            "POST", "/api/smartqa/apks", {"sha256": APK_SHA, "size_bytes": 1, "path": "/srv/a.apk"}
        )
        t.add("POST", "/api/leases", {"lease_id": "lease-0", "device_id": "emulator-0"})
        t.add("POST", "/api/run", {"status": "rejected", "tasks": [], "error": "nope"})
        t.add("POST", "/api/leases/lease-0/release", {"status": "released"})
        with self.assertRaises(Exception):
            await run_check(self.client, self.options(themes=("light",)))
        self.assertIn("POST /api/leases/lease-0/release", self.transport.paths())

    async def test_device_busy_is_reported_without_a_run(self) -> None:
        t = self.transport
        t.add(
            "POST", "/api/smartqa/apks", {"sha256": APK_SHA, "size_bytes": 1, "path": "/srv/a.apk"}
        )
        t.add(
            "POST", "/api/leases", ConflictError(409, "device_busy", payload={"holder": "someone"})
        )
        with self.assertRaises(CheckError) as caught:
            await run_check(self.client, self.options(themes=("light",)))
        self.assertEqual(caught.exception.code, "device_busy")
        self.assertNotIn("POST /api/run", self.transport.paths())

    async def test_failed_task_makes_the_check_not_ok(self) -> None:
        t = self.transport
        t.add(
            "POST", "/api/smartqa/apks", {"sha256": APK_SHA, "size_bytes": 1, "path": "/srv/a.apk"}
        )
        t.add("POST", "/api/leases", {"lease_id": "l", "device_id": "d"})
        t.add(
            "POST",
            "/api/run",
            {"status": "queued", "tasks": [{"session_id": "s", "status": "queued"}]},
        )
        t.add("GET", "/api/sessions/s", {"session_id": "s", "status": "failed", "error": "crash"})
        t.add("POST", "/api/leases/l/release", {})
        result = await run_check(self.client, self.options(themes=("light",)))
        self.assertFalse(result.ok)
        self.assertEqual(result.themes[0].status, "failed")

    async def test_apk_over_the_size_cap_is_refused_before_any_request(self) -> None:
        with self.assertRaises(CheckError) as caught:
            await run_check(self.client, self.options(max_apk_bytes=4))
        self.assertEqual(caught.exception.code, "apk_too_large")
        self.assertEqual(self.transport.calls, [])

    async def test_server_sha_mismatch_is_refused(self) -> None:
        self.transport.add(
            "POST", "/api/smartqa/apks", {"sha256": "0" * 64, "size_bytes": 1, "path": "/x"}
        )
        with self.assertRaises(CheckError) as caught:
            await run_check(self.client, self.options())
        self.assertEqual(caught.exception.code, "apk_sha_mismatch")


class CliTests(SmartQATestCase):
    def test_missing_token_is_refused_for_a_remote_server(self) -> None:
        code = cli_main(
            [
                "check",
                "--apk",
                str(self.apk),
                "--manifest",
                str(self.manifest),
                "--server",
                "https://smart-qa.example.test",
            ],
            env={},
        )
        self.assertEqual(code, 2)

    def test_unknown_theme_is_a_usage_error(self) -> None:
        code = cli_main(
            [
                "check",
                "--apk",
                str(self.apk),
                "--manifest",
                str(self.manifest),
                "--server",
                "http://127.0.0.1:8000",
                "--themes",
                "sepia",
            ],
            env={},
        )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
