from __future__ import annotations

from fastapi.routing import APIRoute

from apps.admin_console.core.access_control import (
    public_tier,
    require_admin,
    require_lifecycle_token,
    require_websocket_admin,
    route_tier,
)
from apps.admin_console.server import app


def _dependency_calls(route) -> set[object]:
    calls: set[object] = set()
    pending = list(getattr(getattr(route, "dependant", None), "dependencies", []))
    while pending:
        dependency = pending.pop()
        calls.add(dependency.call)
        pending.extend(dependency.dependencies)
    return calls


def test_every_registered_route_has_a_declared_tier_and_guard():
    for route in app.routes:
        is_websocket = getattr(route, "websocket", False) or route.__class__.__name__.endswith(
            "WebSocketRoute"
        )
        if not isinstance(route, APIRoute) and not is_websocket and not hasattr(route, "dependant"):
            continue
        methods = set(getattr(route, "methods", ()) or ())
        tier = route_tier(route.path, methods, is_websocket=is_websocket)

        assert tier is not None, f"Missing route tier: {sorted(methods)} {route.path}"

        dependency_calls = _dependency_calls(route)
        assert public_tier in dependency_calls, f"Missing public tier dependency: {route.path}"
        if route.path == "/api/v1" or route.path.startswith("/api/v1/"):
            dependency_names = {
                getattr(call, "__name__", type(call).__name__).casefold()
                for call in dependency_calls
            }
            assert any(
                any(marker in name for marker in ("auth", "token", "tenant"))
                for name in dependency_names
            ), f"Cloud API route lacks tenant authentication: {route.path}"
        if tier == "admin":
            guard = require_websocket_admin if is_websocket else require_admin
            assert guard in dependency_calls, f"Missing admin guard: {route.path}"
        if tier == "lifecycle":
            assert require_lifecycle_token in dependency_calls, (
                f"Missing lifecycle guard: {route.path}"
            )
        if methods.intersection({"POST", "PUT", "PATCH", "DELETE"}):
            if route.path == "/api/v1" or route.path.startswith("/api/v1/"):
                assert any(call is not public_tier for call in dependency_calls), (
                    f"Cloud API mutation lacks tenant-token auth: {route.path}"
                )
            else:
                assert tier in {"admin", "lifecycle"}, f"Unprotected mutation: {route.path}"
