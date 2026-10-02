import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from apps.admin_console.core.security import SameOriginBoundaryMiddleware
from apps.admin_console.routers import system


def test_service_readiness_does_not_run_diagnostic_or_device_probes(monkeypatch):
    async def unexpected_probe(*args, **kwargs):
        raise AssertionError("service readiness must not run diagnostic probes")

    monkeypatch.setattr(system.readiness_engine, "run_all", unexpected_probe)
    app = FastAPI()
    app.include_router(system.router)

    async def request():
        transport = ASGITransport(app=app, client=("127.0.0.1", 43100))
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.get("/api/system/service-readiness")

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json() == {"service_ready": True}


def test_service_readiness_accepts_only_loopback_effective_peers(monkeypatch):
    async def unexpected_probe(*args, **kwargs):
        raise AssertionError("service readiness must not run diagnostic probes")

    monkeypatch.setattr(system.readiness_engine, "run_all", unexpected_probe)
    app = FastAPI()
    app.add_middleware(SameOriginBoundaryMiddleware)
    app.include_router(system.router)
    proxy_app = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")

    async def request(target, peer, headers=None):
        transport = ASGITransport(app=target, client=(peer, 43100))
        async with AsyncClient(transport=transport, base_url="http://smart-qa.tevo.vn") as client:
            return await client.get("/api/system/service-readiness", headers=headers or {})

    async def verify():
        loopback = await request(app, "127.0.0.1")
        remote = await request(app, "198.51.100.23")
        spoofed_loopback = await request(app, "198.51.100.23", {"X-Forwarded-For": "127.0.0.1"})
        forwarded_remote = await request(
            proxy_app, "127.0.0.1", {"X-Forwarded-For": "198.51.100.23"}
        )
        return loopback, remote, spoofed_loopback, forwarded_remote

    loopback, remote, spoofed_loopback, forwarded_remote = asyncio.run(verify())
    assert loopback.status_code == 200
    assert loopback.json() == {"service_ready": True}
    assert remote.status_code == 403
    assert spoofed_loopback.status_code == 403
    assert forwarded_remote.status_code == 403
