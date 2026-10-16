# CLAUDE.md

## Design System
Always read DESIGN.md before making any visual or UI decisions.
All font choices, colors, spacing, and aesthetic direction are defined there.
Do not deviate without explicit user approval.
In QA mode, flag any code that doesn't match DESIGN.md.

## What's New entries
A PR that changes what QAs see in SmartQA adds one entry file in the same PR:
`apps/showcase_ui/whats-new/entries/<YYYY-MM-DD>-<slug>.json`. Format, writing
rules and the `no-whats-new` label: CONTRIBUTING.md, "What's New entries".
CI fails a PR that changes `apps/showcase_ui/src/` (except `*.spec.ts`) without
a new entry or the label. A backend change QAs notice also gets an entry.
