import asyncio

from apps.admin_console.routers import system


def test_service_readiness_does_not_run_diagnostic_or_device_probes(monkeypatch):
    async def unexpected_probe(*args, **kwargs):
        raise AssertionError("service readiness must not run diagnostic probes")

    monkeypatch.setattr(system.readiness_engine, "run_all", unexpected_probe)

    assert asyncio.run(system.get_service_readiness()) == {"service_ready": True}
