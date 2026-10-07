"""Preview route allowlist and guard (CHE-1289).

A per-PR preview serves the real FastAPI app over synthetic fixtures, so every
registered route is classified, and a route nobody reviewed stays off:

* ``REAL``: real read-only behaviour over the preview's own data.
* ``SYNTHETIC``: served by ``routers.preview_synthetic``, never the real handler.
* ``DISABLED``: answered with ``preview_disabled`` before any handler runs.

``PreviewRouteGuard`` is pure ASGI, so a disabled route fails before routing,
dependencies, body parsing and the handler. Tests and server startup call
``unclassified_routes`` so a new route cannot ship without a decision here.

This module stays stdlib-only so the preview profile can import it first.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json
import re
from typing import Any

PREVIEW_DISABLED_CODE = "preview_disabled"
_DISABLED_BODY = json.dumps(
    {
        "detail": "This endpoint is disabled in a per-PR preview.",
        "code": PREVIEW_DISABLED_CODE,
        "fix": "Use the live service; a preview serves synthetic data only.",
    }
).encode()

WEBSOCKET = "WS"

REAL = frozenset(
    {
        "GET /",
        "GET /{full_path:path}",
        "GET /api/images/{image_name}",
        "GET /images/{image_name}",
        "GET /videos/{video_path:path}",
        "GET /api/sessions",
        "GET /api/sessions/{session_id}",
        "GET /api/sessions/{session_id}/video",
        "GET /api/sessions/{session_id}/plan",
        "GET /api/sessions/{session_id}/notes",
        "GET /api/sessions/{session_id}/checks",
        "GET /api/sessions/{session_id}/goal-images/{index}",
        "GET /api/sessions/{session_id}/events",
        "GET /api/sessions/{session_id}/usage",
        "GET /api/sessions/{session_id}/tree",
        "GET /api/sessions/{session_id}/background_tasks",
        "GET /api/sessions/{session_id}/startup_progress",
        "GET /api/sessions/{session_id}/steps",
        "GET /api/steps/{step_id}/traces",
        "GET /api/traces/{trace_id}",
        "GET /api/traces/{trace_id}/download",
        "GET /api/runs",
        "GET /api/runs/{session_id}",
        "GET /api/runs/{session_id}/bundle.zip",
        "GET /api/system/retention",
        "GET /api/system/storage",
        "GET /api/system/whoami",
        "GET /api/system/version",
        "GET /api/tasks/presets",
        "GET /api/tasks/catalog",
        "GET /api/run/defaults",
        "GET /api/stream",
        "GET /api/stream/{session_id}",
    }
)

# Control and device queries answered from preview-only state (no process,
# device, provider or host work). Each is shadowed by a handler in
# ``routers.preview_synthetic``; the dispatch tests prove the real one never runs.
SYNTHETIC = frozenset(
    {
        "GET /api/devices",
        "GET /api/status",
        "GET /api/stream/device-state",
        "GET /api/system/readiness",
        "POST /api/stop",
        "POST /api/resume",
        "POST /api/tasks/{session_id}/cancel-queued",
    }
)

DISABLED = frozenset(
    {
        # Framework docs and the legacy console UI (root-absolute links).
        "GET /openapi.json",
        "GET /docs",
        "GET /docs/oauth2-redirect",
        "GET /redoc",
        "GET /admin",
        "GET /debug",
        # Host files and live device streaming.
        "GET /local_file",
        "GET /api/stream/device-live",
        # Run launch and device replay execute on a device.
        "POST /api/run",
        "GET /api/replay/tools",
        "GET /api/replay/config",
        "GET /api/sessions/{session_id}/replay_steps",
        "POST /api/sessions/{session_id}/steps/{step_number}/replay",
        "GET /api/sessions/{session_id}/steps/{step_number}/replay_traces",
        # Run library writes wait for the L3 fixture store and its ownership tests.
        "POST /api/cleanup",
        "POST /api/sessions/{session_id}/delete",
        "POST /api/runs/{session_id}/pin",
        "POST /api/runs/{session_id}/unpin",
        "POST /api/runs/{session_id}/delete",
        "POST /api/runs/clear",
        "PUT /api/system/retention",
        "POST /api/system/retention/dry-run",
        "POST /api/system/retention/run",
        # Live configuration, credentials, ADB, emulator and server control.
        "GET /api/system/config",
        "PUT /api/system/config",
        "POST /api/system/devices/select",
        "POST /api/system/adb/restart",
        "POST /api/system/adb/heal-keys",
        "POST /api/system/adb/connect",
        "GET /api/system/adb/server",
        "POST /api/system/adb/server/connect",
        "POST /api/system/adb/server/probe",
        "POST /api/system/adb/server/local",
        "POST /api/system/emulator/launch",
        "GET /api/system/emulator/status",
        "POST /api/system/emulator/stop",
        "POST /api/system/emulator/dismiss",
        "GET /api/system/credentials",
        "POST /api/system/credentials/test",
        "POST /api/system/credentials",
        "GET /api/system/model-config-env",
        "GET /api/system/server-status",
        "POST /api/system/restart",
        "POST /api/system/shutdown",
        "GET /api/system/drain",
        "POST /api/system/drain",
        "DELETE /api/system/drain",
        # Host agents, enrollment and the device bridge.
        f"{WEBSOCKET} /api/device-bridge/session",
        "GET /api/hosts",
        "POST /api/hosts/enrollment-codes",
        "GET /api/hosts/enrollment-codes/{code_id}",
        "POST /api/hosts/{host_id}/revoke",
        "POST /api/hosts/{host_id}/rename",
        "GET /api/agent/install.sh",
        "GET /api/agent/dist/{artifact}",
        "POST /api/agent/enroll",
        "POST /api/agent/challenge",
        "POST /api/agent/renew",
        f"{WEBSOCKET} /api/agent/connect",
    }
)

ALLOWED = REAL | SYNTHETIC

# The catch-all SPA route also serves these legacy consoles from sub-paths.
_LEGACY_CONSOLE = re.compile(r"^/(?:admin|debug)(?:/|$)")


class UnsupportedRouteTree(RuntimeError):
    """The router tree has a shape this module cannot classify; refuse to guess."""


@dataclass(frozen=True)
class RegisteredRoute:
    path: str
    methods: frozenset[str]  # {WEBSOCKET} for a websocket route; HEAD is folded into GET
    regex: Any

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(f"{method} {self.path}" for method in self.methods)

    def matches(self, method: str, path: str) -> bool:
        return method in self.methods and self.regex.match(path) is not None


def _registered(node: Any) -> RegisteredRoute:
    # An effective context of a non-API route wraps the Starlette route that carries the path.
    route = getattr(node, "starlette_route", None) or node
    if type(route).__name__ in ("Mount", "Host"):
        raise UnsupportedRouteTree(f"{type(route).__name__} routes are not classified.")
    path, regex = getattr(route, "path", None), getattr(route, "path_regex", None)
    if not path or regex is None:
        raise UnsupportedRouteTree(f"Cannot read the path of {type(node).__name__}.")
    methods = getattr(route, "methods", None)
    return RegisteredRoute(
        path=path,
        methods=frozenset(methods) - {"HEAD"} if methods else frozenset({WEBSOCKET}),
        regex=regex,
    )


def _flatten(nodes: Iterable[Any]) -> Iterable[RegisteredRoute]:
    for node in nodes:
        if hasattr(node, "effective_candidates"):  # FastAPI's lazily included router
            yield from _flatten(node.effective_candidates())
        else:
            yield _registered(node)


def registered_routes(app: Any) -> list[RegisteredRoute]:
    """Every route in the app's router tree, in dispatch order."""
    router = app.router
    if any(True for _ in getattr(router, "_iter_low_priority_routes", lambda: ())()):
        raise UnsupportedRouteTree("Low-priority frontend routes are not classified.")
    return list(_flatten(router.routes))


def unclassified_routes(app: Any) -> list[str]:
    """Route keys with no disposition. Empty means every route was reviewed."""
    known = ALLOWED | DISABLED
    return sorted({k for route in registered_routes(app) for k in route.keys} - known)


def require_classified(app: Any) -> None:
    missing = unclassified_routes(app)
    if missing:
        raise UnsupportedRouteTree(f"Unclassified preview routes: {', '.join(missing)}")


class PreviewRouteGuard:
    """Answer ``preview_disabled`` unless every route that could serve the request is allowed.

    Allowed means classified ``REAL`` or ``SYNTHETIC``: an unclassified route is refused too.

    Checking every matching route (not only the first) makes the decision independent
    of dispatch order: the catch-all SPA route matches each GET, yet a disabled route
    beneath it still wins. A request no route serves is refused the same way.
    """

    def __init__(self, app: Any, *, route_source: Any) -> None:
        self.app = app
        self._route_source = route_source
        self._routes: list[RegisteredRoute] | None = None

    def _allowed(self, scope: dict[str, Any]) -> bool:
        if self._routes is None:
            self._routes = registered_routes(self._route_source)
        if scope["type"] == "websocket":
            method = WEBSOCKET
        else:
            method = "GET" if scope["method"] == "HEAD" else scope["method"]
        path = scope["path"]
        if method == "GET" and _LEGACY_CONSOLE.match(path):
            return False
        keys = [route.path for route in self._routes if route.matches(method, path)]
        return bool(keys) and all(f"{method} {key}" in ALLOWED for key in keys)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket") or self._allowed(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": PREVIEW_DISABLED_CODE})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(_DISABLED_BODY)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _DISABLED_BODY})
