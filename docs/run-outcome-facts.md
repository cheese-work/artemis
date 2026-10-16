# Run outcome facts

`run_outcome.json` contains runner and checker facts, not a QA verdict.
The SDK writes it before task finalization for successful, failed, interrupted
and cancelled runs. Each write uses a unique temporary file in the same
directory followed by an atomic rename. A write failure propagates; finalization
must not announce completion without persisted facts.

Flash stores `task_status` (the reported status, or null if absent), `cause`
and `evidence_step`. Optional `expected`, `actual` and `evidence_ref` carry
the model's comparison and the runner's validated screenshot reference.
Invalid report status or cause values are discarded with `report_cause_invalid`.
Runner-generated interruption statuses are not model report statuses.
Max-turn and no-response stops do not invent a cause.

The report tool accepts `completed` or `failed`. Failure causes are
`app_defect`, `goal_unreachable` and `blocked_by_environment`. An app defect
requires an explicit positive step number with a stored screenshot in the
current session. The runner prefers that step's post screenshot, then its pre
screenshot. It never substitutes a nearby step or trusts a model-supplied
`evidence_ref`. A completed report references its own final screenshot when
available. References contain `step_id`, `step_number`, `image_name` and `which`;
they do not expose filesystem paths.

Pro retains `task_status` and the existing `tests` summary for compatibility.
`checker_items` preserves the ledger records, including `kind` (`assert`,
`verify`, `check`) and `anchor_step_id`. Failed summary items also retain their
failure anchor. A run without checks still writes an outcome. If the graph
stops before reporting a task status, the SDK preserves the available checker
facts with a null task status.

Verdict arbitration, ledger precedence and QA-result persistence belong to
the separate result-derivation layer, not this artifact writer.
