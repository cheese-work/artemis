"""Bounded Unix broker transport shared by the CLI and SDK."""

import json
import socket


MAX_FRAME = 65536


def rpc(socket_path, request):
    encoded = json.dumps(request).encode() + b"\n"
    if len(encoded) > MAX_FRAME:
        raise ValueError("request is too large")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(15)
        client.connect(str(socket_path))
        client.sendall(encoded)
        with client.makefile("rb") as stream:
            response = stream.readline(MAX_FRAME + 1)
    if len(response) > MAX_FRAME or not response.endswith(b"\n"):
        raise ValueError("invalid broker response")
    return json.loads(response)
