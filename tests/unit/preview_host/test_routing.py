"""Routing policy and atomic publication, without host or device access."""

import hashlib
import json
from pathlib import Path

import pytest

from scripts import preview_routing as routing
from scripts.preview_sandbox import Preview


def preview(pr=70, slot=0):
    return Preview(pr, "a" * 40, "sha256:" + "b" * 64, slot)


@pytest.fixture
def reconciler(tmp_path):
    base_dir = tmp_path / "protected"
    base_dir.mkdir()
    base = base_dir / "base.yaml"
    base.write_bytes(routing.encode(routing.base_config(18001, ["smart-qa.tevo.vn", "qa.tailnet"])))
    base.chmod(0o444)
    state = tmp_path / "previews"
    state.mkdir(mode=0o700)
    validations = []

    def validate(candidate):
        validations.append(candidate.read_bytes())
        routing.validate_preview_config(json.loads(candidate.read_bytes()))

    result = routing.Reconciler(
        base, state, hashlib.sha256(base.read_bytes()).hexdigest(), validate
    )
    return result, base, validations


def test_routes_have_exact_host_prefix_priority_and_registry_slot():
    config = routing.preview_config([preview(7, 2), preview(70, 0)])
    routing.validate_preview_config(config)
    route = config["http"]["routers"]["preview-pr7"]
    assert route["priority"] == 200
    assert (
        route["rule"]
        == "Host(`smart-qa.tevo.vn`) && (Path(`/preview/pr/7`) || PathPrefix(`/preview/pr/7/`))"
    )
    assert config["http"]["services"]["preview-pr7"]["loadBalancer"]["servers"] == [
        {"url": "http://127.0.0.1:18102"}
    ]
    assert config["http"]["middlewares"]["preview-pr7-strip"]["stripPrefix"]["prefixes"] == [
        "/preview/pr/7"
    ]


@pytest.mark.parametrize("previews", [[preview(), preview()], [preview(70, 0), preview(71, 0)]])
def test_duplicate_pr_or_registry_slot_rejected(previews):
    with pytest.raises(ValueError):
        routing.preview_config(previews)


@pytest.mark.parametrize(
    "field,value", [("priority", 1000), ("rule", "PathPrefix(`/`)"), ("service", "real-site")]
)
def test_configuration_cannot_expand_scope(field, value):
    config = routing.preview_config([preview()])
    config["http"]["routers"]["preview-pr70"][field] = value
    with pytest.raises(ValueError):
        routing.validate_preview_config(config)


def test_configuration_cannot_redirect_to_arbitrary_upstream():
    config = routing.preview_config([preview()])
    config["http"]["services"]["preview-pr70"]["loadBalancer"]["servers"][0]["url"] = (
        "http://169.254.169.254"
    )
    with pytest.raises(ValueError):
        routing.validate_preview_config(config)


def test_base_and_static_policy():
    base = routing.base_config(18001, ["smart-qa.tevo.vn", "qa.tailnet"])
    assert base["http"]["routers"]["real-site"]["priority"] == 1
    assert base["http"]["routers"]["preview-reserved"]["priority"] == 100
    static = routing.static_config(Path("/srv/config"), 8000)
    assert static["entryPoints"]["web"]["address"] == "127.0.0.1:8000"
    assert static["entryPoints"]["web"]["http"]["sanitizePath"] is False
    assert static["api"] == {"dashboard": False, "insecure": False}
    assert set(static["providers"]) == {"file", "providersThrottleDuration"}


def test_validate_then_swap_and_retain_last_known_good(reconciler):
    manager, base, validations = reconciler
    base_bytes = base.read_bytes()
    manager.reconcile([preview()], ready=lambda item: True)
    assert manager.active.read_bytes() == manager.last_good.read_bytes()
    assert validations[-1] == manager.active.read_bytes()
    assert base.read_bytes() == base_bytes
    assert not list(manager.state.glob("*.candidate"))


def test_not_ready_preserves_existing_routes(reconciler):
    manager, _, _ = reconciler
    manager.reconcile([preview()], ready=lambda item: True)
    previous = manager.active.read_bytes()
    with pytest.raises(ValueError, match="ready"):
        manager.reconcile([preview(71)], ready=lambda item: False)
    assert manager.active.read_bytes() == previous


def test_bad_native_validation_preserves_active_and_backup(reconciler):
    manager, base, _ = reconciler
    manager.reconcile([preview()], ready=lambda item: True)
    previous = manager.active.read_bytes()

    def reject(candidate):
        raise ValueError("native configuration rejected")

    manager.validator = reject
    with pytest.raises(ValueError, match="native configuration"):
        manager.reconcile([preview(71)], ready=lambda item: True)
    assert manager.active.read_bytes() == previous == manager.last_good.read_bytes()
    assert json.loads(base.read_bytes())["http"]["routers"]["real-site"]


def test_failed_atomic_swap_preserves_active(reconciler, monkeypatch):
    manager, _, _ = reconciler
    manager.reconcile([preview()], ready=lambda item: True)
    previous = manager.active.read_bytes()
    native_replace = routing.os.replace

    def fail_active(source, destination):
        if destination == manager.active:
            raise OSError("swap failed")
        native_replace(source, destination)

    monkeypatch.setattr(routing.os, "replace", fail_active)
    with pytest.raises(OSError, match="swap failed"):
        manager.reconcile([preview(71)], ready=lambda item: True)
    assert manager.active.read_bytes() == previous == manager.last_good.read_bytes()
    assert not list(manager.state.glob("*.candidate"))


def test_recover_bad_active_from_last_known_good(reconciler):
    manager, base, _ = reconciler
    manager.reconcile([preview()], ready=lambda item: True)
    previous = manager.active.read_bytes()
    manager.active.write_text("{broken")
    manager.recover()
    assert manager.active.read_bytes() == previous
    assert base.exists()


def test_startup_with_no_valid_preview_is_empty_and_base_untouched(reconciler):
    manager, base, _ = reconciler
    manager.last_good.write_text("broken")
    manager.last_good.chmod(0o600)
    previous = base.read_bytes()
    manager.recover()
    assert json.loads(manager.active.read_bytes()) == routing.preview_config([])
    assert base.read_bytes() == previous


def test_base_pin_changed_stops_publication(reconciler):
    manager, base, _ = reconciler
    base.chmod(0o644)
    base.write_text("{}")
    with pytest.raises(ValueError, match="base"):
        manager.reconcile([preview()], ready=lambda item: True)
    assert not manager.active.exists()


def test_recover_oversized_active_without_reading_unbounded_input(reconciler):
    manager, _, _ = reconciler
    manager.reconcile([preview()], ready=lambda item: True)
    previous = manager.last_good.read_bytes()
    manager.active.write_bytes(b"x" * (routing.MAX_CONFIG_BYTES + 1))
    manager.recover()
    assert manager.active.read_bytes() == previous


@pytest.mark.parametrize("target", ["active", "last_good"])
def test_symlinks_cannot_overwrite_other_files(reconciler, target):
    manager, base, _ = reconciler
    getattr(manager, target).symlink_to(base)
    previous = base.read_bytes()
    with pytest.raises((ValueError, OSError)):
        manager.reconcile([preview()], ready=lambda item: True)
    assert base.read_bytes() == previous


def test_import_interface_remains_inert(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("import interface executed a process")

    monkeypatch.setattr(routing.subprocess, "Popen", forbidden)
    with pytest.raises(NotImplementedError, match="disabled"):
        routing.import_preview(tmp_path / "does-not-exist.tar")


def test_bad_binary_pin_fails_before_native_execution(tmp_path, monkeypatch):
    binary = tmp_path / "traefik"
    binary.write_bytes(b"not the pinned binary")

    def forbidden(*args, **kwargs):
        pytest.fail("an unpinned binary was executed")

    monkeypatch.setattr(routing.subprocess, "Popen", forbidden)
    with pytest.raises(ValueError, match="pin mismatch"):
        routing.NativeValidator(binary, tmp_path / "base.yaml", "0" * 64)
