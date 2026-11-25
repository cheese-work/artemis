# Preview acquisition egress

CHE-9872 (L5b2b) closes the gap in `docs/preview-build.md`: the acquisition
namespace now has a proxy-only network rule. Nothing here installs a rule, starts
a service or touches a host. Stage 1 (`scripts/preview_egress.py`,
`config/preview/egress-squid.conf`) is the Squid policy; this stage is the
network policy that makes it the only way out.

| File | Role |
| --- | --- |
| `scripts/preview_egress.py` `render_acquisition_ruleset(uid)` | Renders the nftables table |
| `config/preview/egress-acquisition.nft` | Checked-in render with stand-in UID `65533` (a test fails on drift) |
| `config/preview/artemis-preview-egress.service` | Squid as `artemis-preview-egress`, in the acquisition namespace |
| `config/preview/sysusers.conf` | Declares the no-login `artemis-preview-egress` account |

## Rules

One `inet` table, `artemis_preview_egress`, loaded inside the acquisition
namespace only. It is separate from the bridge table in
`scripts/preview_sandbox.py` and does not change it.

- Input and output policy are `drop`.
- Established and related replies are allowed.
- Loopback is allowed to TCP 3128 (the proxy) and nothing else.
- Only the proxy UID (`meta skuid`) may open new TCP 443, to IPv4 outside the
  denied list and to IPv6 inside `2000::/3`.
- The proxy UID's denied IPv4 and IPv6 ranges (the stage 1 lists, plus
  `::ffff:0:0/96` IPv4-mapped and `::/128`; NAT64 `64:ff9b::/96` and
  `64:ff9b:1::/48` are already listed) are dropped before the accept rules.
- DNS (UDP/TCP 53), all UDP and every other destination have no rule, so they hit
  the policy drop. The proxy resolves names from the pinned hosts file, not DNS.

## Acquisition flow

All steps run as the trusted host, outside the build sandbox.

1. Pin hosts: resolve the three registry hosts on the trusted host and write
   `render_hosts_pin(answers)` to `/run/artemis-preview/egress.hosts`.
2. Start the proxy: install `config/preview/egress-squid.conf` as
   `/etc/artemis-preview/egress-squid.conf`, then start
   `artemis-preview-egress.service` (its `NetworkNamespacePath` is
   `/run/netns/artemis-preview-acquire`).
3. Apply the ruleset inside that namespace, with the real UID:
   `sed "s/skuid 65533 /skuid $(id -u artemis-preview-egress) /" config/preview/egress-acquisition.nft | ip netns exec artemis-preview-acquire nft -f -`
4. Run `npm ci --ignore-scripts --no-audit --no-fund` in the namespace with the
   environment from `docs/preview-build.md` (`HTTPS_PROXY=http://127.0.0.1:3128`).

Order matters: the ruleset drops everything the proxy does not own, so applying it
before the proxy runs only makes the first `npm ci` fail closed.

## Bypass tests

Run in the namespace after step 3, as a non-proxy user (for example the build
user) unless stated. Each must fail; the last must succeed.

1. `curl --noproxy '*' https://registry.npmjs.org/` fails (direct TCP 443, wrong UID).
2. `curl --noproxy '*' https://1.1.1.1/` and any IPv6 literal fail.
3. `getent hosts registry.npmjs.org` and `dig @1.1.1.1 .` fail (DNS and UDP dropped).
4. `curl -x http://127.0.0.1:3128 https://169.254.169.254/` returns a Squid 403
   (not in the allowlist), and as the proxy UID `curl --noproxy '*' https://169.254.169.254/` fails.
5. Connecting to any other loopback port (for example `127.0.0.1:22`) fails.
6. `curl -x http://127.0.0.1:3128 https://registry.npmjs.org/` succeeds.

## Rollback

1. `ip netns exec artemis-preview-acquire nft delete table inet artemis_preview_egress`
2. `systemctl stop artemis-preview-egress.service`

Removing the table leaves the namespace with no network policy, so also stop the
acquisition or delete the namespace (`ip netns delete artemis-preview-acquire`)
rather than running `npm ci` without it.

## Remaining risk

- Never run against a real host in this stage. The nftables text and the unit are
  checked by rendered-text tests only; `nft -c -f` and `systemd-analyze verify`
  are the first steps of the install stage.
- Squid's default pid file (`/run/squid.pid`) is outside the unit's runtime
  directory. If the host's Squid treats the failed write as fatal, add a
  `pid_filename` line to the config in the install stage. The unit's only
  writable path is its runtime directory (no `LogsDirectory`), so Squid's default
  log files under `/var/log/squid` are not writable either; point `cache_log` and
  `access_log` at the runtime directory or set `access_log none` in the same stage.
- The proxy reads the pin through `SupplementaryGroups=artemis-preview-runner`,
  which also gives it read access to the shared runner directory.
- The proxy account can reach any public address on 443 if the Squid ACLs fail;
  the pinned hosts file and the allowlist are the control for that.
