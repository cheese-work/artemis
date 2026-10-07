# Go host tunnel peer (B3a-2a)

The peer lives in `host-agent`, the `smartqa-host` main package. It ports the
B2 Python host primitive without changing the server or wire protocol. The
reference is PR #79 at `6876c2875e8d6329992b4c11d4a4650dc9ce0a8d`; the delivery
base is PR #77 at `7dd105366d934074d0820a530f592ca856355a8f`. Those revisions
have identical `host_peer.py`, `host_mux.py`, `host_protocol.py`, and protocol
documentation.

## CHE-1097 integration boundary

- `NewHostPeer(epoch, sharedSerials)` creates one authenticated generation.
  It rejects creation unless `ARTEMIS_HOST_AGENT` is enabled. The epoch comes
  from the server's authenticated `connected.generation`, not a local counter.
- `Receive(binaryMessage)` validates and queues frames without waiting for
  local adb. `NextFrame(ctx)` supplies bounded outbound binary messages.
  Only the server opens streams. The peer connects to `127.0.0.1:5037` and
  always uses the fixed gateway allowlist; there is no gateway override.
- `SetShared(serial, shared)` updates the local allowlist. Unshare resets only
  streams selected for that serial and closes their local adb sockets.
  Device-list and tracking responses are filtered against the current shares.
- `Close()` cancels pending connects, closes sockets and streams, waits for
  relay goroutines, and releases the process budget. Unenroll must cancel
  `RunHostConnections`, close the peer, then revoke/remove the identity.
  Close is idempotent.
- `Serve(ctx, HostTransport)` runs binary multiplexing and JSON ping/pong with
  the shared 20-second ping and 50-second dead-peer intervals. `HostTransport`
  operates on complete WebSocket messages; `true` means binary. Receive and
  Send must honor their contexts. Close must unblock transport operations.
  Send must serialize WebSocket writes, including concurrent scanner and
  renewal messages from the command adapter.

CHE-1097 owns WebSocket framing, challenge/hello authentication, token renewal,
device registration, local command/control wiring, and identity revocation.
It must validate the server's minimum/protocol versions before constructing
the peer, and must publish share changes through the existing `devices`
WebSocket message. HTTP token renewal remains the adapter's responsibility.
This slice does not wire the CLI or remove its SQH-E301 placeholders.

`RunHostConnections` accepts a connector returning a newly authenticated peer
and transport. The connector's context bounds authentication/dialing only;
it must not tie the established connection's lifetime to that context. Every
reconnect must return a strictly newer server generation. Old streams are
closed, never replayed. Failed connects use the shared 0.5-second initial
delay, 0.8–1.2 jitter, and active/idle caps of 5/30 seconds.

The `active` callback must describe bound runs, not merely open TCP streams.
A loss episode keeps its first 30-second grace deadline across failed
reconnects; a new authenticated generation cancels that episode. The
`onLoss` hook receives `host_disconnected` once at expiry, or `auth_expired`
immediately without retry. This is a local notification only. A1/server
admission remains authoritative for committing exactly one run interruption,
preserving completed/cancelled outcomes, and retaining immutable host/serial
bindings. The Go peer does not settle run outcomes.

## Verification

Run from `host-agent`:

```sh
go test ./...
go test -race ./...
go vet ./...
go test -run '^$' -fuzz FuzzHostFrame -fuzztime 5s
go test -run '^$' -fuzz FuzzHostMux -fuzztime 5s
sh build.sh dev ./dist
```

`tests/support/golden/host_frames.json` is consumed by both the Go tests and
the Python codec test. Go tests compare every shared constant against the
generated protocol document. Tests cover credit violations, stream-id
exhaustion, the 32-stream cap, control progress with blocked destinations,
saturation isolation, FIN ordering/half-close, untrusted text, allowlist
denials, fragmented/coalesced adb requests, binary transport ids, cancellation,
and deterministic grace/backoff flaps with one notification.

The build matrix remains linux/amd64, darwin/arm64, windows/amd64 only.
All adb peers in these tests are fake sockets; no Android device is used.
