# SmartQA deploy drain

`scripts/deploy_qa.sh` is the repository source for the QA deployment script.
The live script's CHE-1146 `DEPLOYED_SHA` badge is included. This change does
not install the script, restart a live service, or authorize a deployment.

## Order and recovery

The script builds Python dependencies and the UI in a fresh, detached worktree
under `$STATE/releases` before closing admission. It never builds in the live
checkout. A release has its own virtual environment and `DEPLOYED_SHA`.

The loopback-only `/api/system/drain` endpoint writes an atomic, fsynced,
private JSON file containing the deploy owner, deadline, and held queue items.
The deadline determines `Retry-After`; expiration does **not** reopen admission.
Restart does not remove the drain. Queued items remain pending and are restored
on startup; cancelled items are not revived. Only active/executing runs count
towards the wait. Queue reservations are renewed on restoration.

After active runs finish, the script activates the built release, restarts,
checks service readiness, records the confirmed revision, and reopens admission.
On the first activation, the original clone is preserved at `$STATE/source`;
`$REPO` becomes a symlink to the selected release. Worktree metadata is repaired
after the move. Release directories stay at their original paths, so virtual
environment entry points and editable imports do not need relocation.

If health fails, recovery builds the last confirmed revision separately and
restores it under the same drain. Admission stays closed between restart and
the failed health check. EXIT, TERM, INT and HUP cleanup requests owner-scoped
reopening. A failed reopen retains `$STATE/drain_owner` for the next tick.
SIGKILL cannot run a shell trap: the next tick reconciles the durable marker,
recovers the confirmed revision when required, checks health, and reopens.

On a default drain timeout, the script aborts without switching releases or
restarting, reopens admission, and retries on the next timer tick. Timeout is
not a bad build. Only `--skip-drain` or a configured max-defer threshold permits
a cut. `/api/system/drain/cut` durably commits `InterruptReason.DEPLOY_CUT` for
the active session IDs before restart; both server and script log the IDs.

The script refuses a legacy drain response without the matching owner and
deadline. Installing this script against a memory-only server is not a safe
bootstrap: the deployment owner must first arrange the durable API under the
normal deployment approval gate. This PR does not perform that deployment.

## Settings

| Environment setting | Default | Meaning |
| --- | --- | --- |
| `ARTEMIS_DRAIN_TIMEOUT_SECONDS` | `600` | N: deploy wait, in seconds. `DRAIN_TIMEOUT_SECONDS` overrides it in the script. |
| `ARTEMIS_DEPLOY_MAX_DEFER_COUNT` | `0` | Disabled by default. A positive value allows cutting on that consecutive deferred attempt for the same candidate. `MAX_DEFER_COUNT` overrides it. |
| `DRAIN_INTERVAL` | `2` | Seconds between drain status requests. |
| `ARTEMIS_DRAIN_FILE` | Next to `PAUSE_FILE`, named `deploy-drain.json` | Durable state outside the release checkout. Keep this path stable across releases. |
| `ARTEMIS_PAUSE_WINDOW_SECONDS` | `300` | X: shared SmartQA setting for the later pause/resume layer; this layer does not implement part B. |
| `ARTEMIS_VERDICT_PENDING_SECONDS` | `30` | Shared setting for the later verdict pending-cap layer; this layer does not implement verdict classification. |
| `HEALTH_ATTEMPTS` / `HEALTH_INTERVAL` | `60` / `2` | Bounded service-readiness check. |

POST accepts `owner` and `deadline` (Unix seconds). GET reports the owner,
deadline, active count and held queue count. DELETE requires the same owner;
the legacy no-body POST and no-owner DELETE use `loopback`. A different owner
cannot steal or clear a drain. Remote peers, forwarded requests, invalid Host
and cross-origin requests are rejected for every method, including cut.

New run submissions and chat follow-ups return 503 `server_draining` before
persisting. The UI shows “SmartQA đang cập nhật” and the remaining seconds from
the server deadline. Draft text and images remain available for retry. A chat
admission race reports the drain, not a phone reacquisition error.

## Checks and disk ownership

Run `bash scripts/test_deploy_qa.sh` and the CHE-1643 pytest baseline. The shell
harness uses temporary repositories and stubbed uv, npm, curl and systemctl;
it never controls a live service. Temporary harness directories are removed
on exit. Release worktrees, the source clone and rollback points are retained
by the live script. The deployment owner must inventory references before
removing an exact obsolete release; the script does not broadly prune them.
