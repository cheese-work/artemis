"""Let bridge handlers finish before Starlette cancels their test connection."""

from contextlib import contextmanager
import threading

from fastapi.testclient import TestClient


class BridgeTestClient(TestClient):
    def __init__(self, app, **kwargs):
        async def observed_app(scope, receive, send):
            try:
                await app(scope, receive, send)
            finally:
                if scope["type"] == "websocket":
                    scope["bridge_test_finished"].set()

        super().__init__(observed_app, **kwargs)

    @contextmanager
    def websocket_connect(self, *args, **kwargs):
        finished = threading.Event()
        connection = super().websocket_connect(*args, **kwargs)
        connection.scope["bridge_test_finished"] = finished
        with connection as websocket:
            try:
                yield websocket
            finally:
                # Starlette's __exit__ sends disconnect and immediately cancels
                # the ASGI task. Join normal cleanup first, on this portal loop.
                websocket.close()
                finished.wait()
