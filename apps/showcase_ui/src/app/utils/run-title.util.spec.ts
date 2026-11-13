import { runTitle } from './run-title.util';

describe('runTitle', () => {
  const cases: [string, string | null | undefined, string][] = [
    ['headings', '## Open Settings\nCheck the toggles.', 'Open Settings'],
    ['closing heading markers', '# Open Settings #', 'Open Settings'],
    ['bold', '**Open Settings**', 'Open Settings'],
    ['emphasis', '_Open_ __Settings__', 'Open Settings'],
    ['unordered lists', '- **Open** `Settings`', 'Open Settings'],
    ['asterisk lists', '* Open Settings', 'Open Settings'],
    ['plus lists', '+ Open Settings', 'Open Settings'],
    ['ordered lists', '1. Open Settings', 'Open Settings'],
    ['parenthesized lists', '2) Open Settings', 'Open Settings'],
    ['code spans', '`Open Settings`', 'Open Settings'],
    ['combined markdown', '### **Open** _Settings_ with `Android`', 'Open Settings with Android'],
    ['first non-empty line', '\n \t\n  Open Settings  \nIgnore this line', 'Open Settings'],
    ['Windows line endings', '\r\n# Open Settings\r\nSecond line', 'Open Settings'],
    ['empty prompts', '', 'Untitled run'],
    ['whitespace-only prompts', ' \t\r\n  ', 'Untitled run'],
    ['null prompts', null, 'Untitled run'],
    ['missing prompts', undefined, 'Untitled run'],
    ['markdown-only prompts', '# **_`_**', 'Untitled run']
  ];

  for (const [name, prompt, expected] of cases) {
    it(`handles ${name}`, () => expect(runTitle(prompt)).toBe(expected));
  }

  it('leaves a very long first line intact for CSS to clamp', () => {
    const line = 'Open Settings '.repeat(200).trim();
    expect(runTitle(`${line}\nSecond line`)).toBe(line);
  });
});
