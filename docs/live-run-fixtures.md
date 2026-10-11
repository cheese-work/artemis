# Live-run fixtures (CHE-1512)

The Workbench live-run fixtures use the built Angular UI and a loopback HTTP fixture server on X99. They do not connect to an Android device or a live Artemis backend.

From `apps/showcase_ui`:

```sh
npm ci
npm run build -- --configuration development
SHOTS=./live-run-evidence npm run test:live-run
```

The audit captures preparing, running, retrying, paused, phone-lost and finished states at 1440 px and 390 px. It checks the status strip, checklist, current Thought, composer, one header Stop control, 44 px controls, reduced motion and horizontal overflow. `results.json` records every fixture result. Chrome and the temporary browser profile are closed and removed in the same foreground run.

The Karma specs in `run-view.component.spec.ts`, `run-startup.util.spec.ts` and `interrupted-banner.component.spec.ts` cover selection, elapsed timing, announcement counts and state transitions. Run Karma in GitHub CI only, not on X99. The fixture audit is not a live-device acceptance run or an independent review.
