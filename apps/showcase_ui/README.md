# ✨ SmartQA Workspace UI

The browser interface for SmartQA, built with **Angular 22** and **SCSS**. The repository, packages, CLI, backend, and API remain Artemis.

## 🎨 Visual Design Highlights

* **Visual rules**: follow [DESIGN.md](../../DESIGN.md). Surfaces are opaque; do not add glass, blur, translucency or glow.
* **One set of tokens**: `src/styles.scss` defines colours, spacing, radius, type, shadows, motion and the 44 px target as CSS custom properties (`--color-*`, `--space-*`, `--radius-*`, `--font-*`, `--target`). Every surface reads them; `npm test` runs `scripts/lint-theme.mjs`, which fails on a hex colour, an `rgba()` colour or a blur in the listed styles. Dark navy (`--color-device*`) is for a physical device only.
* **Workspace and Runs share one shell**: the run (`app-run-view`) on the left, the run list (`app-run-library`) on the right. From 1200 px they sit side by side. From 800 px they stack, with both visible. Below 800 px the page is one column and scrolls as a whole. On Runs the list sits beside an open run; below 800 px it is dropped and Back to runs reaches it.
* **New-task box**: always expanded, in normal flow under the run. Below 800 px it stays pinned to the bottom of the screen.
* **Keyboard**: Tab order is the top bar, the run, the new-task box, then the list. Skip links reach the new-task box and the run list. Task Queue / Notes & Plans and My runs / Everyone's runs are tabs with arrow keys, Home and End.

## 🚀 How to Run

### Prerequisites
- Node.js `^22.22.3`, `^24.15.0`, or `^26.0.0`
- TypeScript `>=6.0.0 <6.1.0`
- Backend API running on `http://localhost:8000` (via `apps/admin_console` or `artemis ui`)

### Development Server
```bash
cd apps/showcase_ui
npm install
npm start
```
This runs `ng serve --proxy-config proxy.conf.json` on **`http://localhost:4200/`**.

### Production Build
```bash
npm run build
```
Build artifacts will be emitted to `dist/`, which can be served statically by `apps.admin_console` or `artemis ui`.

### Keyboard Walkthrough
```bash
npm run test:keyboard-paste
npm run build
npm run test:keyboard
```

Add `-- --record=<dir>` to also write `<dir>/keyboard-walkthrough.mp4`, a screen recording with a caption of each key and the focused control (needs `ffmpeg`).

Set `CHROME_BIN` to a Chromium-based browser executable, including Brave on macOS. The scripts start Chrome with `--password-store=basic`; without it Chrome on a Linux host with no keyring can stall before its first page load.
The walkthrough seeds the real browser clipboard and sends a native CDP `paste`
editing command with Ctrl+V on Linux/Windows or Cmd+V on macOS. It verifies trusted
paste events, PNG payloads, preview removal with Enter, and native plain-text insertion.
It does not dispatch synthetic `ClipboardEvent` objects or invoke the component directly.

### Navigation

The top bar shows Workspace, Runs, What's New (when updates exist), the device chip,
and the signed-in user. Connect or switch a phone from either page by opening the
chip and choosing an action. Connection addresses appear only in the picker's detail lines.
More options opens the device settings in Setup; admins also have Setup in the user menu.
Workspace opens Setup on first use when system configuration or all provider credentials
are missing. A disconnected phone alone does not redirect to Setup, and Runs stays accessible.
Use Tab and Enter/Space to open the device and user controls. Escape closes either
control and restores focus to its trigger.
Run `npm run build && npm run test:navigation` for the navigation-only native keyboard
walkthrough. It mocks the USB chooser; it does not access a real device.

### Layout audit and evidence screenshots
```bash
npx ng build --configuration development
npm run test:layout                      # clearance, a11y, scenarios and the CHE-1278 contract phase
npm run test:layout -- contract          # one phase: panes, 44 px targets, 4.5:1 on real backgrounds, no blur, reduced motion
node scripts/layout-audit/screenshots.mjs <outDir> <label>   # Workspace, Runs, an open run and Setup at 1280 and 1024 px
```

### Direction B responsive shell

The shell uses a 224 px sidebar from 1200 px, a 72 px labelled rail from 800 to
1199 px, and a 48 px top bar plus 56 px labelled tab bar below 800 px. The fixed
version footer is removed at every width; version information stays in the account
menu. Phone and account controls remain available in all three tiers.

The next pane-layout slice can inject `ShellLayoutService`, set `activePane` to
`list` or `detail`, and mark pane roots with `data-shell-pane="list"` or
`data-shell-pane="detail"`. Below 1024 px the shell hides the inactive pane.
Both panes remain visible from 1024 px. Workspace and Runs navigation reset the
selection to `list`. This hook does not restructure the existing page panes.

```bash
npx ng build --configuration development
SHOTS=./shell-evidence npm run test:shell
```

This X99 headless-browser check uses the existing mock API and native CDP clicks.
It covers the 1200, 1024 and 800 px boundaries, checks non-overlapping 44 px
control boxes and on-screen menus, and saves PNG screenshots at 1440, 1199,
1024, 800, 799 and 390 px. `CHROME_BIN` selects the browser; `SHELL_DIST` selects
an alternative development build for the baseline control.
