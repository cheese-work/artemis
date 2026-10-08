# ✨ SmartQA Workspace UI

The browser interface for SmartQA, built with **Angular 22** and **SCSS**. The repository, packages, CLI, backend, and API remain Artemis.

## 🎨 Visual Design Highlights

* **Visual rules**: follow [DESIGN.md](../../DESIGN.md). Surfaces are opaque; do not add glass, blur, translucency or glow.
* **Real-time Dual-Pane Workspace**:
  - **Left Pane (`app-agent-stream`)**: Live streaming of Agent thought steps, plan breakdowns, tool actions, and status.
  - **Right Pane (`app-chat-interface`)**: Natural language chat input sidebar with interactive feedback and status pills.

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

Set `CHROME_BIN` to a Chromium-based browser executable, including Brave on macOS.
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
