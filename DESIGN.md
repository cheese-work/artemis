---
# gstack: design-md-format=spec
name: SmartQA
description: A calm evidence console. Light, opaque, exact surfaces where a run's verdict and its proof are read in seconds.
colors:
  background: "#f8f9fa"
  surface: "#ffffff"
  surface-subtle: "#f1f5f9"
  ink: "#0f172a"
  text: "#1e293b"
  text-muted: "#475569"
  text-faint: "#64748b"
  rule: "#e2e8f0"
  rule-strong: "#cbd5e1"
  border: "#64748b"
  primary: "#2563eb"
  primary-hover: "#1d4ed8"
  primary-tint: "#eff6ff"
  primary-edge: "#bfdbfe"
  on-primary: "#ffffff"
  focus: "#1d4ed8"
  success: "#166534"
  success-bg: "#dcfce7"
  warning: "#92400e"
  warning-bg: "#fef3c7"
  error: "#991b1b"
  error-bg: "#fee2e2"
  error-solid: "#dc2626"
  neutral: "#334155"
  neutral-bg: "#e2e8f0"
  device: "#0f172a"
  device-edge: "#1e293b"
  device-text: "#e2e8f0"
  device-muted: "#94a3b8"
  model-1: "#0072b2"
  model-2: "#a96800"
  model-3: "#008565"
  model-4: "#d55e00"
  model-5: "#aa4499"
  model-6: "#4b5563"
typography:
  display:
    fontFamily: General Sans
    fontWeight: 600
    fontSize: 2rem
    lineHeight: 2.5rem
    letterSpacing: -0.01em
  title:
    fontFamily: General Sans
    fontWeight: 600
    fontSize: 1.5rem
    lineHeight: 2rem
  section:
    fontFamily: General Sans
    fontWeight: 600
    fontSize: 1.25rem
    lineHeight: 1.75rem
  body:
    fontFamily: Instrument Sans
    fontWeight: 400
    fontSize: 1rem
    lineHeight: 1.5
  ui:
    fontFamily: Instrument Sans
    fontWeight: 400
    fontSize: 0.875rem
    lineHeight: 1.25rem
  label:
    fontFamily: Instrument Sans
    fontWeight: 600
    fontSize: 0.75rem
    lineHeight: 1rem
  mono:
    fontFamily: JetBrains Mono
    fontWeight: 400
    fontSize: 0.75rem
    lineHeight: 1.25rem
    fontFeature: tnum
rounded:
  sm: 4px
  md: 8px
  lg: 12px
  full: 9999px
spacing:
  xs: 4px
  sm: 8px
  smd: 12px
  md: 16px
  lg: 24px
  xl: 32px
  2xl: 48px
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    height: 44px
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    borderColor: "{colors.border}"
    rounded: "{rounded.md}"
    height: 44px
  input:
    backgroundColor: "{colors.surface}"
    borderColor: "{colors.border}"
    rounded: "{rounded.md}"
    height: 44px
  status-chip:
    rounded: "{rounded.full}"
    height: 24px
    typography: "{typography.label}"
  panel:
    backgroundColor: "{colors.surface}"
    borderColor: "{colors.rule}"
    rounded: "{rounded.md}"
  app-bar:
    backgroundColor: "{colors.surface}"
    borderColor: "{colors.rule}"
    height: 56px
  nav-link:
    textColor: "{colors.text-muted}"
    activeTextColor: "{colors.ink}"
    activeIndicator: "{colors.primary}"
  device-lane-header:
    backgroundColor: "{colors.device}"
    textColor: "{colors.device-text}"
  table-row:
    height: 48px
    borderColor: "{colors.rule}"
  table-header:
    backgroundColor: "{colors.surface-subtle}"
    textColor: "{colors.text-muted}"
---

# SmartQA

## Overview

**Creative North Star:** A calm evidence console. A finished run reads like a signed test report: the verdict first, the proof one glance below, nothing decorative in between.

**Product context:** SmartQA is the QA front end of Artemis, an Android test-automation agent. QA testers type a goal in plain language, pick a phone, watch the agent work step by step, then review the verdict and evidence. Admins configure model providers and devices. It is an internal tool used on laptops and external monitors through long daytime sessions, often with several phones running at once.

**What a QA should remember:** the truth about my run, fast; evidence I can trust; calm control over many phones.

**Mode per surface:** Operate everywhere (Workspace, Runs, run detail, Stats, Setup). There is no Persuade or marketing surface.

**Key characteristics:**
- Opaque white surfaces on a pale page. No glass, no blur.
- Blue appears only where you can act or where something is selected.
- Status is always an icon plus a word. Colour never carries meaning alone.
- Dark navy means a physical device, and nothing else.
- Numbers, IDs, serials and durations are set in a monospace with tabular figures.

**Origin:** formalises the identity that shipped through round 1 and round 2 (light slate neutrals, blue accent, Material Symbols) and consolidates it. Decided in CHE-1331 office hours and design consultation, 2026-10-08.

## Colors

**Strategy:** Restrained. One slate neutral family, one blue, the tested status pairs, and a categorical set for model identity. Colour is rare, so when it appears it means something.

**Light or dark:** light only. The use scene is an office in daytime with long sessions next to physical phones. There is no dark theme.

- **Neutrals:** a single slate ramp (`ink`, `text`, `text-muted`, `text-faint`, `rule`, `rule-strong`, `border`). The Tailwind gray and zinc greys that exist in the code today are retired.
- **Primary:** `#2563eb`, matching the favicon. It replaces `#1a73e8` and `#3b82f6` because `#1a73e8` measures 4.27:1 on the page background and fails the 4.5:1 text minimum; `#2563eb` measures 4.9:1. Use it for primary buttons, links, selected rows (`primary-tint`) and the active nav indicator. Never as decoration.
- **Status pairs:** foreground on its own background, each pair at least 4.5:1 (already tested in `status-tokens.spec.ts`). `error-solid` `#dc2626` is the only solid red, for small marks such as a failure tick; `#ef4444` is retired.
- **Device colours:** `device`, `device-edge`, `device-text`, `device-muted` are used only for things that are a physical or virtual phone: device chips, device lane headers and the screenshot frame. A Setup panel or a card is never dark.
- **Model colours:** `model-1` to `model-6`, assigned to models in a stable order and kept through sorting and filtering. They spread hue for colour-blind readers, but colour is never the only signal: every model also has a marker shape (circle, square, triangle, diamond, cross, plus) and a 2-letter mono tag (for example `SO`, `LU`, `GE`). Model colours never express quality: no green-for-best.

## Typography

- **General Sans** (Fontshare, 500/600) is the display voice: page titles, section titles and the verdict word. Use 600 only.
- **Instrument Sans** (Google Fonts, 400/500/600) is body and UI. It replaces Inter, which rendered only because the requested Google Sans was never loaded.
- **JetBrains Mono** (Google Fonts, 400/500) is for serials, run IDs, build SHAs, timestamps, durations, token counts and every numeric table cell, always with tabular figures.
- **Material Symbols Outlined** stays the icon set: 20px optical size, weight 300, unfilled.

Scale (size / line height): 12/16 label and metadata, 14/20 UI, 16/24 body, 20/28 section, 24/32 page title, 32/40 verdict. Body text in reading areas is never below 14px; metadata never below 12px.

Weights: 400 for text, 500 for emphasis inside UI, 600 for titles, column headers and the verdict. Hierarchy comes from size and face, not from making everything 600.

Loading: one `<link>` per provider in `index.html` with `display=swap`; remove the duplicate `@import` in `styles.scss`, the unloaded Google Sans references, and the Outfit and Roboto imports.

## Layout

- **Breakpoints:** 768px and 1200px, plus 1600px for a wider device board. Below 768px: one column, the nav collapses to icons, model and profile selectors stack under the goal, the re-run sheet is full screen. From 1200px: run detail splits into verdict + evidence; the Workspace shows device lanes side by side.
- **App bar:** 56px, opaque, one bottom rule. Not floating.
- **Page padding:** 24px. Section padding 16px. Control gaps 8px.
- **Workspace:** the composer sits in normal flow above the device board (goal, Run profile, Execution model, Start run). Device lanes are about 360px wide and keep their order; the board never reorders phones by itself. Exceptions surface as "Needs attention" on the lane header instead of moving anything.
- **Run detail:** verdict panel first (about 420px), evidence second (step list + screenshot). The failing step is highlighted with a tint and a label, not a coloured edge.
- **Tables:** one flat table per job. 48px rows (40px compact option), sticky header, frozen first column, numbers right-aligned in mono. Every column is sortable, with the direction shown and exposed to assistive tech. No KPI-card prelude.

## Elevation & Depth

Flat by default. Depth always has an offset; there is no zero-offset glow.

| Level | Shadow | Use |
|---|---|---|
| 0 | none | panels, tables, lanes (a 1px `rule` border separates them) |
| 1 | `0 2px 6px rgb(15 23 42 / 8%)` | sticky bars once content scrolls under them |
| 2 | `0 4px 12px rgb(15 23 42 / 12%)` | menus, popovers, the model picker list |
| 3 | `0 12px 32px rgb(15 23 42 / 16%)` | dialogs and the re-run sheet |

Translucency and `backdrop-filter` are not used. The 28 existing blur surfaces (nav, composer dock, chips) become opaque `surface` with a rule.

## Shapes

`sm` 4px for tags and small labels; `md` 8px for controls, panels, lanes and table containers; `lg` 12px for dialogs and sheets; `full` only for status chips and circular icon buttons. A nested element uses the outer radius minus the gap. The old 6/10/15/18px radii are retired.

## Components

- **Buttons:** 44px high. Primary is solid `primary` with `on-primary` text; one primary per view. Secondary is white with a `border` outline. Ghost is text in `primary`. Hover darkens to `primary-hover`; focus-visible shows a 3px `focus` ring with 2px offset; disabled drops to `text-faint` with no pointer and keeps its label.
- **Inputs and selects:** 44px, `border` outline, visible label above (never placeholder-as-label), error text below in `error` with an icon.
- **Status chips:** 24px pill, icon + word, colour from the status pair. Outcome vocabulary is fixed:
  - Application verdict: Pass (success), Fail (error), Inconclusive (warning), Not evaluated (neutral).
  - Execution: Completed (neutral; completing is not passing), Failed (error), Interrupted (warning), Cancelled (neutral).
  - QA review: Not reviewed, Accepted, Rejected (always spelled out).
  - The bare word "Success" and the label "Passed" for a completed execution are not used.
- **Verdict panel (run detail):** the verdict word in display type ("Fail."), the three outcome chips, a one-sentence reason in body type, a link to the decisive step, then a receipt: a mono ledger with dashed hairlines listing goal, device label, model tag, steps and duration, checks, tokens and list-price cost, SmartQA build and app build. Review actions follow (Accept result, Reject…, Re-run with…).
- **Device label:** every place that names a device uses `<device name>, <connection>, <location>` and appends ` (<team> team)` when the device belongs to a team pool. Connection is one of `USB`, `Wi-Fi`, `AVD`. Location is `your PC`, `your PC browser`, `<Name>'s PC` or `Cloud`. When the model is unknown the name is `Android device`. Examples:
  - Pixel 6 Pro, USB, your PC
  - Pixel 3a, USB, your PC browser
  - Pixel 3a emulator, AVD, Cloud
  - Xiaomi 10, USB, Thean's PC (qa1 team)
  - Android device, USB, Cloud (qa1 team)

  The serial goes on a second line in mono `device-muted` or in a tooltip, never inside the label.
- **Device lane header:** `device` background, device label, live state on the right (Running, Idle, Needs attention) as icon + word.
- **Model picker:** labelled "Execution model", default option shows the resolved name ("Default — Sol"); incompatible models stay listed but disabled with a reason; marker + tag next to each name.
- **Tables:** see Layout. Rows below the data threshold read "Limited data · n runs" at normal contrast. Missing values show "—", never 0 or $0. Intervals are shown as text plus a thin whisker.
- **States:** every list, table and panel defines loading (skeleton rows, previous data kept on refresh), empty (what to do next, in one sentence), error (what failed and how to retry), partial (what is missing and why).

## Do's and Don'ts

- Do put the verdict and its reason before any timeline or log.
- Do pair every status colour with an icon and a word.
- Do set every number, ID and duration in JetBrains Mono with tabular figures.
- Do keep device lanes, table rows and selected evidence in place while runs update.
- Do use the device label format everywhere a device is named.
- Don't use glass, blur or translucent surfaces.
- Don't use dark navy for anything that is not a device.
- Don't add KPI cards, podiums, winner badges or green-for-best to stats.
- Don't nest cards or put a coloured edge on a panel to signal state; use a tint, an icon and a label.
- Don't animate rows into new positions or add ambient motion.

## Motion

- **Approach:** minimal-functional.
- **Easing:** enter `cubic-bezier(.16,1,.3,1)`, exit and move `cubic-bezier(.4,0,.2,1)`.
- **Duration:** control feedback 120ms, panels and sheets 180ms. Nothing above 250ms.
- **The one authored moment:** the verdict word settles when a run finishes (opacity plus a 4px rise, 180ms).
- `prefers-reduced-motion: reduce` turns all of it off.

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-10-08 | Initial design system created: formalise and consolidate the existing identity | /design-consultation for CHE-1331; Cheese chose formalise over rebrand (D1 A); outside voices: Codex and Claude subagent |
| 2026-10-08 | Accent `#1a73e8` → `#2563eb` | `#1a73e8` is 4.27:1 on the page background, below 4.5:1 |
| 2026-10-08 | Fonts: General Sans / Instrument Sans / JetBrains Mono | Google Sans was never loaded; Inter, Outfit, Roboto are overused or unused |
| 2026-10-08 | Remove all glass/blur; dark navy only for devices (R2); verdict receipt (R1) | Evidence must read as solid; device identity at a glance |
| 2026-10-08 | Device label `<name>, <connection>, <location> (<team> team)` | Cheese review of the preview |
| 2026-10-08 | Every stats column sortable; QA table default alphabetical | Cheese T2 override at the CHE-1331 autoplan gate |
