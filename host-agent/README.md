# SmartQA host agent — integrated Go core

This candidate branches from `main`, including B1 enrollment at
`85499732dbd0b95b1b337c437ff6a5d9ffc22dd6`. It consumes CHE-1328's Go peer
and B2's authenticated server protocol. `run` signs a fresh challenge,
validates protocol versions, registers local devices, renews its session,
and reconnects through `RunHostConnections`. No local adb port is exposed.
Independent final review and native service survival remain NOT-RUN.

## Build and verification

Run these on X99:

```sh
cd host-agent
go test -race ./...
go vet ./...
sh build.sh 0.1.0
cd dist && sha256sum -c SHA256SUMS
```

The build script produces exactly linux/amd64, darwin/arm64 and
windows/amd64, plus `SHA256SUMS` and `install.sh`. CGO is disabled. Windows
protected identity storage, installer, services and native acceptance remain
B3b work; its executable can report version/help, but enrollment refuses to
persist an unprotected Windows key. No tray, signing or notarization is added.

## Configuration and enrollment

The feature is default off. Opt in with `ARTEMIS_HOST_AGENT=1`.
Configuration precedence is flags, `SMARTQA_HOST_*` environment, JSON file.
Supported settings are `SERVER`, `ADB`, `PROXY`, `DNS_SERVER`,
`NO_ADB_DOWNLOAD`; `CONFIG` selects the file. Enrollment uses `CODE` and
`NAME` but never persists the one-time code. The default configuration is
`~/.smartqa/config.json`; private identity is `~/.smartqa/identity.json`.
The directory must be mode 0700; identity and config writes are atomic 0600.
Enrollment saves the Ed25519 key **before** sending the signed B1 request,
so a retry after a lost response uses the same key. An enrolled identity
cannot silently move to another server.

```sh
ARTEMIS_HOST_AGENT=1 smartqa-host enroll --server https://smartqa.example
smartqa-host status --json
ARTEMIS_HOST_AGENT=1 smartqa-host devices --json
smartqa-host config show
smartqa-host doctor --no-adb-download
smartqa-host support-info
```

Set `SMARTQA_HOST_CODE` in the environment before enrollment. Do not pass the
one-time code as a process argument. The installer passes enrollment headers
to curl through stdin and passes the code to `enroll` through the environment.

HTTPS is required except for loopback fixture servers. Redirects are not
followed. Explicit `--proxy` and `--dns-server IP:port` support environments
where CGO-off networking needs a VPN-aware resolver. TLS verification is
never disabled. `doctor` distinguishes DNS, proxy and TLS failures; a proxy
resolves the target hostname remotely, so doctor does not incorrectly require
local target DNS to succeed. The split-DNS regression is a failure-classifier
fixture, **not native macOS VPN acceptance**.

## adb and discovery

Existing adb is preferred and never killed. Missing adb downloads official
Google platform-tools **37.0.1** unless `--no-adb-download` is set or a custom
adb path was supplied. All three official archive SHA-256 pins are in
`tools.go`; the archives were downloaded from `dl.google.com` and hashed on
X99. The downloader verifies the pinned checksum before extracting into a
private staging directory. Traversal, symlinks and oversized archives fail.

The local discovery executor has a non-overridable allowlist containing only
`version`, `devices -l` and one fixed identity request per new transport
(`host:transport-id:N`, then `getprop` of `ro.serialno` and the emulator AVD
name). Run traffic must use B2's separate stateful gateway; this discovery
allowlist is **not** a replacement for that gateway. The CLI reads a fresh
local snapshot without starting or restarting adb. The connected core scans
every two seconds and publishes bounded latest-only updates through the small
tunnel interface. Device registration uses B2's `devices` WebSocket message.

## Device identity (B3a-2)

- **hw id:** `physical:<ro.serialno>` for phones, `avd:<AVD name>` for
  running emulators. A blank, `unknown` or `0123456789ABCDEF` serial number,
  an emulator without an AVD name, and unauthorized or offline devices are
  untrusted. Two live emulators with one AVD name, or two USB phones with one
  serial number, are ambiguous. Both fall back to the adb serial
  (`adb:<serial>`), show Needs attention and are never auto-shared.
- **Opaque id:** the server sees only `sd-` + the first 16 hex digits of
  HMAC-SHA256(org pepper, `"dev:" + hw id`). The server generates the org
  pepper once and returns it at enrollment; the agent keeps it in
  `identity.json`. An identity enrolled before B3a-2 has no pepper: `run`
  fails with `SQH-E010` until it is re-enrolled. Raw serials, hw ids and AVD
  names stay in the agent, its private files and the local CLI. Stated limit:
  this does not protect from the server operator or from a run that itself
  reads `ro.serialno`.
- **One device per phone:** USB and wireless transports of one phone are one
  device with one id. Blank adb serials are addressed by transport id.
  `emulator-5554` reused by another AVD is a different device. A changed
  wireless endpoint keeps the id.
- **Pinned transport:** the gateway maps an id to one pinned adb transport
  (`host:transport-id:N`). The server leases a device to a run when it binds
  the run and releases the lease when it frees the run slot. It sends the
  leased ids in the `connected` message and as a `lease` message on every
  change. While a device is leased, a USB arrival never redirects it, and
  losing the pinned transport holds the device offline, however long the run
  is quiet. The run then ends `interrupted(device_offline)` with no silent
  failover. Without a lease the device moves at once, preferring USB.
  Device lists, `get-serialno` and `tport` replies carry only shared opaque ids.
- v1 registers only running emulators; it never boots, snapshots or kills an AVD.

**Known discovery gap:** `doctor` only checks the resolved adb executable's
version, including a downloaded platform-tools binary. A PASS does not make
`devices` or `run` discovery ready. Both use the existing adb server's smart
socket at `127.0.0.1:5037`, not the downloaded executable. If that server is
absent, discovery returns `SQH-E201`. Downloading platform-tools does not
start an adb server, and this agent does not start an adb server or restart
an existing one. An already-running adb server is required for discovery;
automatic downloaded-adb startup remains unimplemented before ready.

## Install and user services

Permanent tunnel failures (`SQH-E006`, `SQH-E007`, `SQH-E008`, `SQH-E009`)
exit with status 78. The systemd unit uses `RestartPreventExitStatus=78`.
The LaunchAgent uses `service run`, which logs those errors and exits 0,
with `KeepAlive.SuccessfulExit=false`. Other failures still exit 1 and
remain eligible for restart. Correct the identity, protocol, or duplicate
instance before manually starting the service again. Native systemd and
launchd lifecycle acceptance is NOT-RUN.

An operator must deploy the built distribution directory and set
`ARTEMIS_AGENT_DIST_DIR` on the server. B1's existing authenticated install
and artifact routes then serve only the three supported targets, installer
and checksum manifest. Without a valid enrollment header or with the feature
off, the routes remain unavailable. Symlinks and unsupported assets are denied.

After that deployment, export `SMARTQA_HOST_CODE` and set `SERVER` to the
HTTPS server URL. The one-line Linux/macOS installer is:

```sh
( installer=$(mktemp) && trap 'rm -f "$installer"' EXIT && printf 'X-Artemis-Enrollment-Code: %s\n' "$SMARTQA_HOST_CODE" | curl --proto '=https' -fsS -H @- "$SERVER/api/agent/install.sh" -o "$installer" && sh "$installer" "$SERVER" )
```

The line verifies the binary checksum, installs into `~/.local/bin`, enrolls,
runs doctor, then requests user-service installation. Add `--no-adb-download`
as the second installer argument to opt out.
`service install` requires an enrolled, private identity and a matching valid
server configuration before writing a service file or invoking a manager.
The integrated binary supplies the Go tunnel adapter. An unwired core still
refuses service activation before creating files. The installer stops on any
failed enrollment, doctor check, or service installation. A successful
`unenroll` stops and joins the peer before revocation, removes the identity
only after confirmation, and exits the service successfully rather than
triggering an on-failure restart.
No install on a deployed hostname is claimed. Linux uses systemd `--user`
and attempts linger; macOS uses a LaunchAgent. No root or SYSTEM service is
created. `service uninstall` removes only this agent's service; identity is
not silently erased or remotely revoked. `logs` reads the user's journal or
the configured LaunchAgent log.

## Updates and support

Basic `update --url HTTPS_URL --sha256 HASH --size BYTES` downloads and stages
an artifact through a pluggable `manifestVerifier`. It never overwrites the
installed binary or activates an update. Launcher-owned activation, manifest
sequence/channel/halt and rollback belong to B3a-3. Staging refuses an existing
file rather than following a symlink or overwriting another attempt.

`support-info` emits only version/platform, enrollment boolean, configuration
booleans and device-state counts. It omits hostnames, host ids, paths, keys,
codes, proxy credentials, models and serials. Errors print stable catalog
messages rather than raw server bodies or credential-bearing error strings.
See `ERRORS.md` for every `SQH-Exxx` code.

## Local control and sharing

While `run` is active, Unix commands use a mode-0600 `control.sock` in the
private configuration directory. No TCP fallback is provided. `status` and
`devices` report the live registry; `share DEVICE` and `unshare DEVICE`
update both the gateway allowlist and the server registration. `DEVICE` is
the `id` (opaque id) or `hw_id` from `devices --json`, any adb serial of the
device, or a unique label; a selector that matches two devices fails with
`SQH-E207` (exit 3).

Every scanned device is registered; the share mode decides which are runnable.
The state is kept in a private `sharing.json` (0600) next to the configuration.

- `mode select` (default on a fresh install, nothing selected): `share`
  saves a reference `{kind, hw_id, label, first_seen, last_serial}` that
  re-applies whenever the device reappears, never by serial or list position. A `share` in this mode also clears an exclusion left from auto mode.
- `mode auto`: every trusted device is shared, including devices plugged in
  later. `unshare` records an exclusion that survives rescan, replug and
  restart until the device is shared again. `status` always says
  "all devices, new devices auto-shared (N excluded)". The first time a phone
  is auto-shared, the agent logs how to unshare it and sends
  `device_auto_shared`; the desktop notification with an Unshare action is
  tray work (B4a).
- `mode auto` and `mode select` print their effect and need `--yes`
  (`SQH-E206`, exit 2, otherwise). Auto to select saves exactly the current
  shared set; select to auto keeps exclusions.
- An untrusted or ambiguous device is shared only by an explicit `share`
  ("Choose it once to share"). That consent is kept in memory and is cleared
  on unplug, replug or agent restart.
- `enroll --share-mode select|auto` (or `SMARTQA_HOST_SHARE_MODE`) picks the
  mode at enrollment.

The agent sends `share_mode_changed`, `device_share_changed{by}`,
`device_auto_shared` and `identity_ambiguous` audit events. The server logs
an event only when every field is allowlisted, and stores only opaque ids.
A physical id registered by two computers is flagged `also_visible` on both
rows, and B5a-1 admission dispatches to neither. B5a-1 holds the admin
choice ("Use this computer", "These are different phones"); this slice adds
no admin route for it.
Emulators are never flagged: an AVD name is per computer.

`unenroll` cancels and joins the connection loop before using its token on
`POST /api/agent/unenroll`. The server revokes only the authenticated identity.
If no agent is running, the command authenticates a temporary connection,
closes the peer, and revokes that identity. Failure preserves the local key.
The server remains authoritative for run interruption. The adapter has no
bound-run activity feed and uses the peer's idle reconnect cap. The
unshare-versus-live-stream design is tracked separately.

## Acceptance limits

- Native Linux/macOS clean install and service survival: **NOT-RUN**.
- Native macOS split-DNS VPN and Windows lifecycle: **NOT-RUN**.
- CHE-1101 deployed-hostname/Cloudflare checks: **NOT-RUN**; no binary deployed.
- Gateway, multiplexer and adapter verification uses fake sockets only.
- Windows-pipe local control remains B3b scope; no insecure TCP fallback is introduced.

This partial candidate references CHE-1097 and CHE-1087. It does not close
CHE-1097, and stays draft for the required independent final verification.
