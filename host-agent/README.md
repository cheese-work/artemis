# SmartQA host agent — B3a-1 independent core

This candidate branches from `main`, including B1 enrollment at
`85499732dbd0b95b1b337c437ff6a5d9ffc22dd6`. It is **not a runnable host tunnel**.
B2/CHE-1096 has no published gateway or multiplexer contract. `run`, `share`,
`unshare` and `unenroll` fail closed with `SQH-E301`; no local adb port is
exposed and no device is reported connected. Completing that integration,
live registration, local control commands, and service survival requires B2.

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
smartqa-host devices --json
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
`version` and `devices -l`. Run traffic must use B2's separate stateful
gateway; this discovery allowlist is **not** a replacement for that gateway.
The registry retains every USB, wireless and emulator transport, including
unauthorized/offline devices, and rejects blank serials. It drops sharing on
unplug, state changes and transport replacement, including `emulator-5554`
reuse. Hardware identity, USB/wireless canonicalization, opaque ids and saved
references belong to B3a-2; this candidate does not auto-share or transmit
raw serials to a server. The CLI reads a fresh local snapshot without
starting or restarting adb. The connected core scans every two seconds and
publishes bounded latest-only updates through the small tunnel interface.
Server registration still requires the B2 implementation; scans never bypass
its gateway.

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
**Service activation is refused with `SQH-E301` until a B2 tunnel is wired.**
`service install` writes no service file and invokes no service manager when
the tunnel is absent. The installer exits at that refusal and does not query
service status or enable a restarting service. The installed binary and
enrolled identity remain available for diagnostics. Service-start acceptance
remains NOT-RUN until B2 is wired.
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

## Acceptance limits

- Native Linux/macOS clean install and service survival: **NOT-RUN**; B2 is missing.
- Native macOS split-DNS VPN and Windows lifecycle: **NOT-RUN**.
- CHE-1101 deployed-hostname/Cloudflare checks: **NOT-RUN**; no binary deployed.
- Gateway allowlist/fuzz, multiplexer and reconnect/grace acceptance: B2 scope.
- Unix-socket/Windows-pipe local control and sharing integration: no insecure
  TCP fallback is introduced; these commands remain fail closed pending B2.

This partial candidate references CHE-1097 and CHE-1087. It does not close
CHE-1097, and must stay draft until the missing B2 integration and required
independent verification return.
