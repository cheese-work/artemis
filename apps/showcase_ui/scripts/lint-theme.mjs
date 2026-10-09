// One theme: the surfaces below take every colour from the design tokens in src/styles.scss.
// Fail the build on a hex colour, an rgba() colour, or a blur/glass effect (DESIGN.md: opaque surfaces, no blur).
// rgb(15 23 42 / n%) is the shadow ink DESIGN.md specifies, so it is the one literal allowed.
import { readFile } from 'node:fs/promises';

const files = [
  'src/app/app.component.scss',
  'src/app/components/chat-interface/chat-interface.component.scss',
  'src/app/components/computers/computers.component.scss',
  'src/app/components/failures/failures.component.scss',
  'src/app/components/storage/storage.component.scss',
  'src/app/components/nav-switcher/nav-switcher.component.scss',
  'src/app/components/run-library/run-library.component.scss',
  'src/app/components/run-presentation/run-action-bar.component.scss',
  'src/app/components/run-presentation/run-evidence-panel.component.scss',
  'src/app/components/run-view/run-view.component.scss',
  'src/app/pages/home/home.component.scss',
  'src/app/pages/setup/setup.component.scss',
  'src/app/pages/workspace/workspace.component.scss',
  // Components whose styles live inline in the .ts file (only the styles: [`...`] block is checked).
  'src/app/components/admin-identity-indicator/admin-identity-indicator.component.ts',
  'src/app/components/device-chip/device-chip.component.ts',
  'src/app/components/interrupted-banner/interrupted-banner.component.ts',
  'src/app/components/run-id-copy/run-id-copy.component.ts',
  'src/app/components/run-presentation/run-device-label.component.ts',
  'src/app/components/run-presentation/run-step-row.component.ts',
  'src/app/components/scope-switch/scope-switch.component.ts',
  'src/app/components/usb-phone-connection/usb-phone-connection.component.ts',
  'src/app/components/version-footer/version-footer.component.ts',
  'src/app/components/whats-new/whats-new.component.ts',
  'src/app/components/workspace-device-chip/workspace-device-chip.component.ts',
];
const rules = [
  [/#[0-9a-f]{3,8}\b/i, 'hex colour'],
  [/\brgba?\((?!\s*15[ ,]+23[ ,]+42\b)/i, 'rgb()/rgba() colour'],
  [/backdrop-filter|-webkit-backdrop-filter|\bblur\(/i, 'blur or glass effect'],
];

const failures = [];
for (const file of files) {
  let text = await readFile(new URL(`../${file}`, import.meta.url), 'utf8');
  if (file.endsWith('.ts')) text = [...text.matchAll(/styles:\s*\[\s*`([\s\S]*?)`\s*\]/g)].map((m) => m[1]).join('\n');
  const lines = text.split('\n');
  lines.forEach((line, index) => {
    if (/^\s*(\/\/|\/\*|\*)/.test(line)) return;
    for (const [pattern, what] of rules) if (pattern.test(line)) failures.push(`${file}:${index + 1} ${what}: ${line.trim()}`);
  });
}
if (failures.length) {
  console.error(failures.join('\n'));
  throw new Error(`${failures.length} hard-coded colour or glass effect(s); use the tokens in src/styles.scss`);
}
