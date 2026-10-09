# Board operations: migrations, config and contributor map

Contract for CHE-1332. This file is the single definition of the board config
keys, the migration and rollback rules, and the code map for contributors.
Routes and errors: [board API](board-api.md). Scopes and permissions:
[board permissions](board-permissions.md). Matching rules:
[device identity](device-identity.md).

## Config keys

All keys are environment variables read at startup. A missing, non-numeric or
out-of-range value falls back to the default and logs a warning, the same as
`ARTEMIS_BRIDGE_SESSION_TTL_SECONDS` today.

| Key | Default | Range | Meaning |
| --- | --- | --- | --- |
| `ARTEMIS_BOARD_ENABLED` | `1` | `0`, `1` | `0` turns off the board routes, board SSE events and board UI. Data and notes stay. This is the rollback switch. |
| `ARTEMIS_DEVICE_TTL_S` | `60` | 10 to 3600 | Seconds without a heartbeat before a connection is `unknown`. Above the host agent's 50 s dead interval (`host-agent/host_protocol.go`). |
| `ARTEMIS_STEP_STALL_S` | `300` | 30 to 86400 | Seconds without a new step before a running run raises `step_stalled`. |
| `ARTEMIS_LANE_HIDE_AFTER_H` | `24` | 1 to 720 | Hours a disconnected lane stays on the board before it is hidden. |
| `ARTEMIS_TIMELINE_WINDOW_H` | `24` | `24`, `168` | Default availability timeline window. The UI offers 24 h and 7 d. |
| `ARTEMIS_RUN_SEARCH_DEFAULT_DAYS` | `7` | 1 to 365 | Default date range of the run list and search in the UI. |
| `ARTEMIS_BOARD_PREVIEW_LIMIT` | `3` | 1 to 20 | Default `limit` for `queue.items` and `recent.items` per lane. |

`GET /api/run/defaults` gains an additive `board` object with the effective
values the UI needs (`preview_limit`, `search_default_days`,
`timeline_window_h`, `step_stall_s`, `lane_hide_after_h`), so the UI never
hard-codes them.

## Migration contract

The board adds its own tables (devices, connections, aliases, device events,
queue, submission ledger, annotations, comments, link-share records, lane
preferences, measurement events) and nullable columns on run metadata (connection
snapshot, app build, agent model, suite version, device model).

1. **Per-module revision table.** `schema_revisions(module TEXT PRIMARY KEY,
   revision INTEGER NOT NULL, updated_at REAL NOT NULL)`. Each board module
   (`devices`, `queue`, `notes`, `run_meta_ext`, `measurement`) owns one row and
   applies numbered revisions in order. The existing presence checks (for
   example `run_catalog.ensure_schema`) stay as they are.
2. **Additive and nullable.** New columns are nullable with no default
   rewrite. No existing column is renamed, retyped or dropped. Unknown
   historical identity, model, build or suite stays `NULL`, shown as unknown.
3. **WAL-aware backup first.** Before a module's first pending revision, the
   migration takes an online backup through SQLite's backup API, as
   `run_catalog._online_backup` does. The backup includes committed WAL
   frames; copying the `.db` file alone is not a backup.
4. **Resumable backfill with progress.** Backfill runs in batches and stores
   its cursor in `backfill_progress(module, cursor, done, total)`. An
   interrupted backfill resumes from its cursor. Progress is reported through
   the catalog CLI pattern (`artemis catalog migrate`, `backfill`, `rebuild`).
5. **FTS rebuild with fallback.** Annotation and comment text joins the
   existing FTS5 catalog. A rebuild uses `artemis catalog rebuild`. Without
   FTS5, search falls back to substring matching and returns the
   `search_fallback_substring` warning. Rebuild and backfill time are measured
   on a production-size catalog before release.
6. **Retention.** Media expiry is separate from run deletion. A retained note
   keeps its step or recording identity tombstone and the run's authorization
   context, so the note stays readable after its media expires. Deleting a
   whole run deletes its notes and their search entries.

### Run snapshot (`run_meta_ext`)

Revision 1 adds `run_meta.app_build`, `suite_version`, `device_model` and
`agent_model` (nullable `TEXT`; `artemis/data_engine/run_snapshot.py`).
`schema_revisions` and `backfill_progress` live in
`artemis/data_engine/schema_revisions.py`.

- **At execution.** The run writes its snapshot when its session is created.
  Today that is the device model (`ro.product.model`) and the planner model.
  A recorded value is never rewritten; a worker restart only fills unknowns.
- **Backfill.** Runs that exist at the upgrade are filled from data that
  already exists: the run's `device_info` and the host device record
  (`host_devices.model`). Anything else stays `NULL`. `artemis catalog migrate`
  and `artemis catalog backfill` report the count and resume an interrupted
  backfill.
- **Hook for later layers.** App build, suite version (CHE-1339) and the picked
  model (CHE-1331) go into the run's `device_info` under the column name before
  the session is created. The snapshot and the backfill read them from there.
- **API.** `GET /api/runs` and `GET /api/runs/{session_id}` return the four
  fields; `null` is unknown. Search filters on them are a later layer.

### Rollback

- Default rollback: set `ARTEMIS_BOARD_ENABLED=0`. New features stop; tables,
  notes and revisions stay. Restoring an old backup is not the default
  rollback.
- The previous binary runs on the upgraded database: it ignores unknown tables
  and nullable columns. Its retention sweep deletes whole runs as before; on
  the next upgrade, notes whose run no longer exists are removed with their
  search entries, the same as a full-run deletion.
- Re-enabling resumes from the recorded revisions and backfill cursors.

### Required migration tests

Upgrade from the current schema; a migration interrupted mid-backfill resumes;
queued work across a restart follows the
[restart and Retry contract](board-api.md#restart-and-retry); the previous
binary works on the upgraded database, including its retention sweep; search
without FTS5.

## Contributor map

Data flows one way. Each layer has one owner module and one focused test
command.

```text
adb pool ─┐
bridge  ──┼─▶ reconciler ─▶ repository ─▶ API + SSE ─▶ Angular service ─▶ board UI
host reg ─┘   (state, events)  (SQLite)    (routers)    (HTTP + stream)
```

| Layer | Owns | Location | Focused tests |
| --- | --- | --- | --- |
| Reconciler | Heartbeats, TTL, hysteresis, identity matching, `device_events`, the board read model | `apps/admin_console/services/` (new `device_reconciler.py`, `board_read_model.py`) | `uv run pytest tests/unit/admin_console -k "reconciler or device_identity"` |
| Repository | Tables, revisions, backfill, queue and ledger, notes, visibility joins | `apps/admin_console/database/repositories/` | `uv run pytest tests/unit/admin_console -k "repository or migration"` |
| API and SSE | Routes, error envelope, event registry, access and preview registries | `apps/admin_console/routers/` (new `board.py`, `inventory.py`, `notes.py`), `core/access_control.py`, `core/preview_routes.py` | `uv run pytest tests/unit/admin_console -k "board or queue or notes or route_tiers or preview_routes"` |
| Angular service | HTTP calls, SSE subscription, watermark and resync, legacy error adapter | `apps/showcase_ui/src/app/services/` (new `board.service.ts`) | GitHub CI runs `npm test`; Karma does not run on X99 |
| Board UI | Lanes, composer, run list, notes | `apps/showcase_ui/src/app/pages/`, `components/` | GitHub CI, rendered-DOM specs next to the components |

Rules for contributors:

- The board API and SSE read only from the board read model, never from
  `state.queue_items` directly.
- A new SSE event type needs a visibility check in the event registry, or the
  server refuses to send it.
- A new route needs entries in the access-control and preview route registries;
  `test_route_tiers.py` and `test_preview_routes.py` fail otherwise.
- The device-free preview profile ([preview fixtures](preview-fixtures.md)) is
  the shared fixture for backend tests, frontend specs and manual checks.
- UI follows [DESIGN.md](../DESIGN.md).
