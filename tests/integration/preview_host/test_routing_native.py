"""Native Traefik on disposable loopback listeners; no live ingress or Docker."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request

import pytest

from scripts import preview_routing as routing
from scripts import preview_sandbox as sandbox


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


OPENER = build_opener(ProxyHandler({}), NoRedirect())


def request(port, path, host=routing.HOST, **headers):
    query = Request(f"http://127.0.0.1:{port}{path}", headers={"Host": host, **headers})
    try:
        return OPENER.open(query, timeout=2)
    except HTTPError as response:
        return response


class Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            self.server.release.wait(2)
            self.wfile.write(b"data: second\n\n")
            return
        payload = json.dumps(
            {
                "upstream": self.server.identity,
                "path": self.path,
                "host": self.headers.get("Host"),
                "origin": self.headers.get("Origin"),
                "access": self.headers.get("Cf-Access-Jwt-Assertion"),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def upstreams(monkeypatch):
    servers = []
    threads = []
    try:
        for attempt in range(20):
            first = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            try:
                second = ThreadingHTTPServer(("127.0.0.1", first.server_port + 1), Upstream)
                break
            except OSError:
                first.server_close()
        else:
            pytest.fail("could not allocate disposable contiguous preview ports")
        servers.extend([first, second, ThreadingHTTPServer(("127.0.0.1", 0), Upstream)])
        monkeypatch.setattr(routing, "HOST_PORT_BASE", first.server_port)
        monkeypatch.setattr(sandbox, "HOST_PORT_BASE", first.server_port)
        for server, identity in zip(servers, ("pr7", "pr70", "root"), strict=True):
            server.identity = identity
            server.release = threading.Event()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            threads.append(thread)
            thread.start()
        yield servers
    finally:
        for server in servers:
            server.release.set()
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=3)


@pytest.fixture
def native(tmp_path, upstreams):
    binary_path = os.environ.get("ARTEMIS_TEST_TRAEFIK")
    if not binary_path:
        pytest.skip("set ARTEMIS_TEST_TRAEFIK to the checksum-pinned native binary")
    binary = Path(binary_path).resolve()
    state = tmp_path / "routes"
    state.mkdir(mode=0o700)
    base = state / "base.yaml"
    base.write_bytes(
        routing.encode(routing.base_config(upstreams[2].server_port, [routing.HOST, "qa.tailnet"]))
    )
    base.chmod(0o444)
    base_sha = hashlib.sha256(base.read_bytes()).hexdigest()
    validator = routing.NativeValidator(binary, base, base_sha)
    manager = routing.Reconciler(base, state, base_sha, validator)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = tmp_path / "static.yaml"
    config.write_bytes(routing.encode(routing.static_config(tmp_path / "routes", port)))
    with (tmp_path / "native.log").open("wb+") as log:
        process = subprocess.Popen(
            [str(binary), f"--configFile={config}"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    with request(port, "/") as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    pass
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("native base routes did not start")
            assert process.poll() is None
            yield manager, port, process, upstreams
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def await_preview(port, path, expected):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with request(port, path) as response:
            if response.status == 200 and json.load(response)["upstream"] == expected:
                return
        time.sleep(0.05)
    pytest.fail("preview dynamic configuration did not reload")


def registry():
    return [
        sandbox.Preview(7, "a" * 40, "sha256:" + "b" * 64, 0),
        sandbox.Preview(70, "c" * 40, "sha256:" + "d" * 64, 1),
    ]


def assert_root(port, upstreams):
    for path in ("/", "/api/state", "/deep/link", "/api/rawdata", "/dashboard/"):
        with request(
            port,
            path,
            Origin="https://smart-qa.tevo.vn",
            **{"Cf-Access-Jwt-Assertion": "synthetic-assertion"},
        ) as response:
            assert response.status == 200
            payload = json.load(response)
            assert payload == {
                "upstream": "root",
                "path": path,
                "host": routing.HOST,
                "origin": "https://smart-qa.tevo.vn",
                "access": "synthetic-assertion",
            }
    with request(port, "/api/state", host="qa.tailnet") as response:
        assert json.load(response)["upstream"] == "root"
    started = time.monotonic()
    with request(port, "/api/events") as response:
        assert response.headers["Content-Type"] == "text/event-stream"
        assert response.readline() == b"data: first\n"
        assert time.monotonic() - started < 1
        upstreams[2].release.set()
        assert response.read() == b"\ndata: second\n\n"


def test_base_alone_serves_root_ui_api_and_sse(native):
    _, port, _, upstreams = native
    assert_root(port, upstreams)
    for path in ("/preview", "/preview/", "/preview/pr/7/", "/preview/pr/700/"):
        with request(port, path) as response:
            assert response.status == 404


@pytest.mark.parametrize("host", [routing.HOST, "qa.tailnet"])
def test_root_video_paths_keep_reserved_characters_with_previews_active(native, host):
    manager, port, _, _ = native
    manager.reconcile(registry(), ready=lambda item: True)
    await_preview(port, "/preview/pr/7/", "pr7")
    for path in (
        "/videos/Test%20%231.mp4",
        "/videos/a;b.mp4",
        "/videos/dir%2Ffile.mp4",
        "/videos/a%25b.mp4",
        "/previewish/videos/a;b.mp4",
        "/api/x?q=%2f",
    ):
        with request(port, path, host=host) as response:
            assert response.status == 200, path
            payload = json.load(response)
            assert payload["upstream"] == "root"
            assert payload["path"] == path
            assert payload["host"] == host


def test_native_prefix_boundaries_redirect_headers_and_stream(native):
    manager, port, _, upstreams = native
    manager.reconcile(registry(), ready=lambda item: True)
    await_preview(port, "/preview/pr/7/", "pr7")
    await_preview(port, "/preview/pr/70/", "pr70")
    for number, identity in ((7, "pr7"), (70, "pr70")):
        prefix = f"/preview/pr/{number}"
        with request(port, prefix + "?keep=1") as response:
            assert response.status == 301
            assert response.headers["Location"] == f"https://{routing.HOST}{prefix}/?keep=1"
        with request(port, prefix, **{"X-Forwarded-Proto": "https"}) as response:
            assert response.headers["Location"] == f"https://{routing.HOST}{prefix}/"
        with request(
            port,
            prefix + "/api/state",
            Origin="https://smart-qa.tevo.vn",
            **{"Cf-Access-Jwt-Assertion": "synthetic-assertion"},
        ) as response:
            assert json.load(response) == {
                "upstream": identity,
                "path": "/api/state",
                "host": routing.HOST,
                "origin": "https://smart-qa.tevo.vn",
                "access": "synthetic-assertion",
            }
    for path in (
        "/preview/pr/700/",
        "/preview/pr/7extra",
        "/preview/pr/070/",
        "/preview/pr/8/",
        "/preview/api/rawdata",
    ):
        with request(port, path) as response:
            assert response.status == 404
    with request(port, "/preview/pr/7/", host="qa.tailnet") as response:
        assert response.status == 404
    with request(port, "/preview/pr/7/api/events") as response:
        assert response.readline() == b"data: first\n"
        upstreams[0].release.set()
        assert response.read() == b"\ndata: second\n\n"
    assert_root(port, upstreams)


def test_withdraw_routes_leaves_reserved_404_and_root_unchanged(native):
    manager, port, _, upstreams = native
    manager.reconcile(registry(), ready=lambda item: True)
    await_preview(port, "/preview/pr/7/", "pr7")
    manager.reconcile([], ready=lambda item: True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with request(port, "/preview/pr/7/") as response:
            if response.status == 404:
                break
        time.sleep(0.05)
    else:
        pytest.fail("withdrawn preview remained routed")
    for path in ("/preview/pr/7", "/preview/pr/70/", "/preview/api/rawdata"):
        with request(port, path) as response:
            assert response.status == 404
    assert_root(port, upstreams)


@pytest.mark.parametrize(
    "path",
    [
        "/preview/pr/7/../../api/state",
        "/preview/pr/7/%2e%2e/api/state",
        "/preview/pr/7/%252e%252e/api/state",
        "/preview/pr/7%2f../api/state",
        "/preview//pr/7/",
        "/preview/pr/7/./",
        "/preview/pr/7/%5c../api/state",
        "/preview/pr/7/;../api/state",
    ],
)
def test_ambiguous_paths_never_reach_either_upstream(native, path):
    manager, port, _, _ = native
    manager.reconcile(registry(), ready=lambda item: True)
    await_preview(port, "/preview/pr/7/", "pr7")
    with request(port, path) as response:
        assert response.status in (400, 404)


def test_native_rejects_invalid_base_before_swap(native):
    manager, port, _, upstreams = native
    manager.reconcile(registry(), ready=lambda item: True)
    previous = manager.active.read_bytes()
    bad_base = manager.state.parent.parent / "bad-base.yaml"
    config = routing.base_config(upstreams[2].server_port)
    config["http"]["routers"]["real-site"]["service"] = "does-not-exist"
    bad_base.write_bytes(routing.encode(config))
    bad_base.chmod(0o444)
    bad_validator = routing.NativeValidator(
        manager.validator.binary, bad_base, hashlib.sha256(bad_base.read_bytes()).hexdigest()
    )
    manager.validator = bad_validator
    with pytest.raises(ValueError, match="native"):
        manager.reconcile([], ready=lambda item: True)
    assert manager.active.read_bytes() == previous == manager.last_good.read_bytes()
    assert_root(port, upstreams)


def test_killed_reconciler_and_corrupt_preview_leave_base_serving(native):
    manager, port, _, upstreams = native
    manager.reconcile(registry(), ready=lambda item: True)
    await_preview(port, "/preview/pr/7/", "pr7")
    previous = manager.active.read_bytes()
    program = """
import os, signal, sys
from pathlib import Path
from scripts.preview_routing import Reconciler
from scripts import preview_routing, preview_sandbox
preview_routing.HOST_PORT_BASE = int(sys.argv[4])
preview_sandbox.HOST_PORT_BASE = int(sys.argv[4])
manager = Reconciler(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], lambda candidate: os.kill(os.getpid(), signal.SIGKILL))
manager.reconcile([], ready=lambda item: True)
"""
    result = subprocess.run(
        [
            os.sys.executable,
            "-c",
            program,
            str(manager.base),
            str(manager.state),
            manager.base_sha256,
            str(routing.HOST_PORT_BASE),
        ],
        check=False,
    )
    assert result.returncode == -9
    assert manager.active.read_bytes() == previous
    assert_root(port, upstreams)
    manager.active.write_text("{broken")
    time.sleep(0.2)
    assert_root(port, upstreams)
    manager.recover()
    assert manager.active.read_bytes() == previous
    await_preview(port, "/preview/pr/7/", "pr7")


def test_prestart_recovers_corruption_before_native_gateway_restart(native):
    manager, port, process, upstreams = native
    manager.active.write_text("{broken")
    manager.active.chmod(0o640)
    manager.last_good.write_text("{also-broken")
    manager.last_good.chmod(0o640)
    process.terminate()
    process.wait(timeout=5)
    result = subprocess.run(
        [
            os.sys.executable,
            "-m",
            "scripts.preview_routing",
            "recover",
            "--base",
            str(manager.base),
            "--state",
            str(manager.state),
            "--base-sha256",
            manager.base_sha256,
            "--traefik",
            str(manager.validator.binary),
        ],
        check=False,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(manager.active.read_bytes()) == routing.preview_config([])
    config = manager.state.parent / "static.yaml"
    restarted = subprocess.Popen(
        [str(manager.validator.binary), f"--configFile={config}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "HOME": str(manager.state.parent)},
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with request(port, "/") as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                pass
            time.sleep(0.05)
        assert_root(port, upstreams)
        with request(port, "/preview/pr/7/") as response:
            assert response.status == 404
    finally:
        restarted.terminate()
        restarted.wait(timeout=5)
