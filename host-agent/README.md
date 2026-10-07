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
ARTEMIS_HOST_AGENT=1 smartqa-host enroll --server https://smartqa.example --code CODE
smartqa-host status --json
smartqa-host devices --json
smartqa-host config show
smartqa-host doctor --no-adb-download
smartqa-host support-info
```

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

## Install and user services

An operator must deploy the built distribution directory and set
`ARTEMIS_AGENT_DIST_DIR` on the server. B1's existing authenticated install
and artifact routes then serve only the three supported targets, installer
and checksum manifest. Without a valid enrollment header or with the feature
off, the routes remain unavailable. Symlinks and unsupported assets are denied.

After that deployment, the one-line Linux/macOS installer is:

```sh
( installer=$(mktemp) && trap 'rm -f "$installer"' EXIT && curl --proto '=https' -fsS -H "X-Artemis-Enrollment-Code: $CODE" "$SERVER/api/agent/install.sh" -o "$installer" && sh "$installer" "$SERVER" "$CODE" )
```

The line verifies the binary checksum, installs into `~/.local/bin`, enrolls,
runs doctor, installs/starts a user service and checks its status. Add
`--no-adb-download` as the third installer argument to opt out.
**This candidate cannot pass the service-start acceptance until B2 is wired.**
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
