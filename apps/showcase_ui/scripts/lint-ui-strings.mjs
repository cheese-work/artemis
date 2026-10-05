// "daemon" names only the local Artemis server. QA-facing computer/phone UI
// says "computer" and "phone"; fail the build if these files drift.
import { readFile } from 'node:fs/promises';

const files = [
  'src/app/utils/computer-strings.ts',
  'src/app/utils/device-chip.util.ts',
  'src/app/services/hosts.service.ts',
  'src/app/components/computers/computers.component.ts',
  'src/app/components/computers/computers.component.html',
  'src/app/components/device-chip/device-chip.component.ts',
  'src/app/components/registry-phones/registry-phones.component.ts',
  'src/app/pages/setup/setup.component.html',
  'src/app/utils/run-library-strings.ts',
  'src/app/utils/recording-state.util.ts',
  'src/app/components/run-library/run-library.component.html',
  'src/app/components/run-viewer/run-viewer.component.html',
];

const failures = [];
for (const file of files) {
  const text = await readFile(new URL(`../${file}`, import.meta.url), 'utf8');
  if (/daemon/i.test(text)) failures.push(file);
}
if (failures.length) {
  throw new Error(`UI strings must say "computer"/"phone", never "daemon": ${failures.join(', ')}`);
}
