from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute, _IncludedRouter

from apps.admin_console.core.access_control import (
    public_tier,
    require_admin,
    require_effective_loopback,
    require_lifecycle_token,
    require_qa,
    require_websocket_admin,
    route_tier,
)
from apps.admin_console.server import app


def _iter_effective_routes(routes):
    for route in routes:
        if isinstance(route, _IncludedRouter):
            yield from _iter_effective_routes(route.effective_candidates())
        else:
            yield route


def _assert_routes_have_declared_tiers(routes):
    for registered_route in _iter_effective_routes(routes):
        route = getattr(registered_route, "original_route", registered_route)
        is_websocket = type(route).__name__.endswith("WebSocketRoute")
        if not isinstance(route, APIRoute) and not is_websocket:
            continue
        path = (
            getattr(registered_route, "path", "")
            or getattr(getattr(registered_route, "starlette_route", None), "path", "")
            or getattr(route, "path", "")
        )
        methods = set(getattr(registered_route, "methods", ()) or ())
        tier = route_tier(path, methods, is_websocket=is_websocket)

        assert tier is not None, f"Missing route tier: {sorted(methods)} {path}"

        dependency_calls = _dependency_calls(registered_route)
        assert public_tier in dependency_calls, f"Missing public tier dependency: {path}"
        if path == "/api/v1" or path.startswith("/api/v1/"):
            dependency_names = {
                getattr(call, "__name__", type(call).__name__).casefold()
                for call in dependency_calls
            }
            assert any(
                any(marker in name for marker in ("auth", "token", "tenant"))
                for name in dependency_names
            ), f"Cloud API route lacks tenant authentication: {path}"
        if tier == "admin":
            guard = require_websocket_admin if is_websocket else require_admin
            assert guard in dependency_calls, f"Missing admin guard: {path}"
        if tier == "qa":
            assert require_qa in dependency_calls, f"Missing QA guard: {path}"
        if tier == "lifecycle":
            assert require_lifecycle_token in dependency_calls, f"Missing lifecycle guard: {path}"
        if tier == "loopback":
            assert require_effective_loopback in dependency_calls, f"Missing loopback guard: {path}"
        # The agent bypass prefix and the agent tier are the same set, both ways.
        assert path.startswith("/api/agent/") == (tier == "agent"), f"Tier/prefix mismatch: {path}"
        if tier == "agent":
            assert any(getattr(call, "agent_auth", False) for call in dependency_calls), (
                f"Missing agent auth guard: {path}"
            )
        if methods.intersection({"POST", "PUT", "PATCH", "DELETE"}):
            if path == "/api/v1" or path.startswith("/api/v1/"):
                assert any(call is not public_tier for call in dependency_calls), (
                    f"Cloud API mutation lacks tenant-token auth: {path}"
                )
            else:
                public_task_controls = {
                    "/api/run",
                    "/api/stop",
                    "/api/tasks/{session_id}/cancel-queued",
                    "/api/resume",
                }
                assert tier in {"admin", "qa", "lifecycle", "loopback", "agent"} or (
                    tier == "public" and path in public_task_controls
                ), f"Unprotected mutation: {path}"


def _dependency_calls(route) -> set[object]:
    calls: set[object] = set()
    original_route = getattr(route, "original_route", route)
    dependant = getattr(route, "dependant", None) or getattr(original_route, "dependant", None)
    pending = list(getattr(dependant, "dependencies", []))
    while pending:
        dependency = pending.pop()
        calls.add(dependency.call)
        pending.extend(dependency.dependencies)
    calls.update(
        dependency.dependency
        for dependency in getattr(route, "dependencies", ())
        if getattr(dependency, "dependency", None) is not None
    )
    return calls


def test_every_registered_route_has_a_declared_tier_and_guard():
    _assert_routes_have_declared_tiers(app.routes)


def test_untiered_post_in_nested_included_router_fails_tier_check():
    child_router = APIRouter()

    @child_router.post("/rogue-untiered")
    async def rogue_untiered_route():
        return {"ok": True}

    parent_router = APIRouter()
    parent_router.include_router(child_router, prefix="/nested")
    test_app = FastAPI()
    test_app.include_router(parent_router, prefix="/api")

    with pytest.raises(AssertionError, match="Missing route tier"):
        _assert_routes_have_declared_tiers(test_app.routes)


def test_approved_task_controls_and_bridge_keep_public_tier():
    for path in (
        "/api/run",
        "/api/stop",
        "/api/tasks/{session_id}/cancel-queued",
        "/api/resume",
    ):
        assert route_tier(path, {"POST"}) == "public"

    assert route_tier("/api/device-bridge/session", set(), is_websocket=True) == "public"
    assert route_tier("/api/system/adb/restart", {"POST"}) == "admin"


def test_approved_device_recovery_actions_require_a_signed_qa_identity():
    for path in ("/api/system/adb/heal-keys", "/api/system/emulator/dismiss"):
        assert route_tier(path, {"POST"}) == "qa"

    assert route_tier("/api/system/adb/restart", {"POST"}) == "admin"


def test_drain_controls_are_loopback_tier_for_every_method():
    for method in ("GET", "POST", "DELETE"):
        assert route_tier("/api/system/drain", {method}) == "loopback"
    assert route_tier("/api/system/drain", {"PUT"}) is None


HOST_ROUTES = [
    ("/api/hosts", {"GET"}, "public"),
    ("/api/hosts/enrollment-codes", {"POST"}, "admin"),
    ("/api/hosts/enrollment-codes/{code_id}", {"GET"}, "public"),
    ("/api/hosts/{host_id}/revoke", {"POST"}, "admin"),
    ("/api/hosts/{host_id}/rename", {"POST"}, "admin"),
    ("/api/agent/install.sh", {"GET"}, "agent"),
    ("/api/agent/dist/{artifact}", {"GET"}, "agent"),
    ("/api/agent/enroll", {"POST"}, "agent"),
    ("/api/agent/challenge", {"POST"}, "agent"),
    ("/api/agent/renew", {"POST"}, "agent"),
    ("/api/agent/unenroll", {"POST"}, "agent"),
]


@pytest.mark.parametrize(("path", "methods", "tier"), HOST_ROUTES)
def test_host_route_tier_matrix(path, methods, tier):
    assert route_tier(path, methods) == tier


def test_agent_connect_websocket_has_the_agent_tier():
    assert route_tier("/api/agent/connect", set(), is_websocket=True) == "agent"


def test_every_host_route_in_the_matrix_is_registered_and_nothing_else_is():
    registered = {
        (
            getattr(r, "path", ""),
            frozenset(getattr(r, "methods", ()) or ()),
        )
        for r in _iter_effective_routes(app.routes)
        if getattr(r, "path", "").startswith(("/api/hosts", "/api/agent"))
        and type(getattr(r, "original_route", r)).__name__ == "APIRoute"
    }
    assert registered == {(path, frozenset(methods)) for path, methods, _ in HOST_ROUTES}


def test_untiered_route_under_the_agent_prefix_fails_tier_check():
    rogue = APIRouter()

    @rogue.post("/api/agent/rogue")
    async def rogue_route():
        return {}

    test_app = FastAPI()
    test_app.include_router(rogue)
    with pytest.raises(AssertionError, match="Missing route tier"):
        _assert_routes_have_declared_tiers(test_app.routes)


def test_agent_route_without_an_operation_level_guard_fails_tier_check():
    unguarded = APIRouter()

    @unguarded.post("/api/agent/enroll", dependencies=[Depends(public_tier)])
    async def enroll_without_guard():
        return {}

    test_app = FastAPI()
    test_app.include_router(unguarded)
    with pytest.raises(AssertionError, match="Missing agent auth guard"):
        _assert_routes_have_declared_tiers(test_app.routes)


def test_human_host_route_cannot_hide_under_the_agent_bypass():
    human = APIRouter()

    @human.post("/api/agent/revoke", dependencies=[Depends(public_tier)])
    async def revoke_under_bypass():
        return {}

    test_app = FastAPI()
    test_app.include_router(human)
    with pytest.raises(AssertionError, match="Missing route tier"):
        _assert_routes_have_declared_tiers(test_app.routes)
