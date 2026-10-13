# Workbench run header (CHE-1502)

The shared run view places its short CF1 title, copyable mono run ID, status,
primary action and More disclosure in the header. Stop targets the displayed
active run and requires the same owner/admin capability as Pin and Delete.
Run again preserves the original prompt. Existing confirmation dialogs and
clipboard fallback remain in use.

The facts row shows App, Phone, Started (ICT), Elapsed or Duration, and Model.
App comes from recorded action metadata. Model comes only from a matching
session. Missing values read “Not recorded”; this slice does not invent
historical model metadata or change the catalog API. Elapsed updates each
second without announcing the clock and stops updating on teardown.

An active run uses the status strip. The finished verdict slot retains the
existing report/checker summary until CHE-1364 supplies the verdict panel.
Paused and retrying states retain their explanations; Continue task remains
restricted to the selected, resumable live session.

## Verification

From `apps/showcase_ui`:

```sh
npm ci
npm run build -- --configuration development
npx tsc --noEmit -p tsconfig.spec.json
npm run test:run-header
```

The audit serves deterministic mock data on localhost, uses X99 headless
Chrome, and writes four PNGs and `results.json` to `run-header-evidence`
(or `SHOTS`). It checks finished/running actions, ID copy, More contents,
prompt disclosure, app facts, elapsed updates, 44 × 44 px own hit boxes and
horizontal overflow at 1440 px and 390 px. It closes its browser/server and
removes its temporary browser profile. No device or production run is used.

Karma specs cover state-dependent actions, owner/read-only restrictions,
copy, menu disclosure, facts, clock teardown and header control dimensions.
Karma execution belongs to frontend CI, not the X99 local acceptance route.
