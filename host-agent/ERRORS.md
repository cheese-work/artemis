# Host agent error catalog

Error codes are stable. Error details do not print credentials or raw server responses.

| Code | Meaning and recovery |
| --- | --- |
| SQH-E001 | Feature flag disabled; opt in with `ARTEMIS_HOST_AGENT=1`. |
| SQH-E002 | Invalid configuration or state permissions; use a private state directory. |
| SQH-E003 | Invalid command; read `smartqa-host help`. |
| SQH-E004 | Enrollment rejected; create a new code in Setup → Computers. |
| SQH-E005 | Enrollment required; run `enroll`. |
| SQH-E006 | Protocol upgrade required; install a newer agent. |
| SQH-E007 | Host authentication expired or was revoked; re-enroll this computer. |
| SQH-E008 | Invalid tunnel protocol; check compatible server and agent versions. |
| SQH-E009 | Local agent unavailable or already running; inspect service status. |
| SQH-E101 | DNS failure; check split-DNS VPN routing or configure an explicit DNS server. |
| SQH-E102 | Proxy failure; check proxy address and authentication. |
| SQH-E103 | TLS failure; fix certificates or system time, never disable verification. |
| SQH-E104 | Connection failure; check `doctor`. |
| SQH-E201 | adb unavailable; install platform-tools or allow automatic download. |
| SQH-E202 | Checksum mismatch; do not execute the artifact. |
| SQH-E203 | Unsafe, malformed or oversized archive; discard the download. |
| SQH-E204 | Discovery request denied; this allowlist cannot be overridden. |
| SQH-E205 | Device absent, ambiguous or unauthorized; reconnect and inspect devices. |
| SQH-E301 | B2 transport is unpublished; run/share/unshare/unenroll cannot bypass it. |
| SQH-E302 | Artifact staged only; activation requires the B3a-3 launcher. |
| SQH-E401 | User service failure; inspect service status and user-session support. |
| SQH-E402 | Windows lifecycle/protected enrollment state is deferred to B3b. |
