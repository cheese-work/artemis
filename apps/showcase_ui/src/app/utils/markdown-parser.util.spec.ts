import { parseNoteLines, parseNote, renderMarkdownToHtml } from './markdown-parser.util';

describe('markdown-parser.util verification & check lines', () => {
  it('should parse - verify: line as type verify with checkKind verify', () => {
    const markdown = `- [x] Parent task
  - [x] Subtask 1
  - verify: Commute duration is recorded in note \`eta_details\``;

    const lines = parseNoteLines(markdown);
    expect(lines.length).toBe(3);
    expect(lines[0].type).toBe('checked');
    expect(lines[1].type).toBe('checked');

    const verifyLine = lines[2];
    expect(verifyLine.type).toBe('verify');
    expect(verifyLine.checkKind).toBe('verify');
    expect(verifyLine.atEnd).toBe(false);
    expect(verifyLine.segments.map(s => s.text).join('')).toBe('Commute duration is recorded in note eta_details');
    expect(verifyLine.segments.find(s => s.code)?.text).toBe('eta_details');
  });

  it('should parse - assert: and - assert@end: lines', () => {
    const markdown = `- [ ] Open app
  - assert: the welcome screen shows up
- assert@end: final status is completed`;

    const lines = parseNoteLines(markdown);
    expect(lines.length).toBe(3);

    expect(lines[1].type).toBe('verify');
    expect(lines[1].checkKind).toBe('assert');
    expect(lines[1].atEnd).toBe(false);

    expect(lines[2].type).toBe('verify');
    expect(lines[2].checkKind).toBe('assert');
    expect(lines[2].atEnd).toBe(true);
  });

  it('should parse - finding: lines', () => {
    const markdown = `- [x] Step 1
  - finding: Unresolved verify failure`;

    const lines = parseNoteLines(markdown);
    expect(lines[1].type).toBe('finding');
    expect(lines[1].checkKind).toBe('finding');
    expect(lines[1].segments[0].text).toBe('Unresolved verify failure');
  });

  it('should group verify lines under parent milestone checks in parseNote', () => {
    const note = `- [x] Open Maps
  - [x] Search destination
  - verify: Arrival time is visible in note \`eta\``;

    const parsed = parseNote(note);
    expect(parsed.milestones.length).toBe(1);
    expect(parsed.milestones[0].subSteps.length).toBe(1);
    expect(parsed.milestones[0].subSteps[0].type).toBe('checked');
    expect(parsed.milestones[0].checks.length).toBe(1);
    expect(parsed.milestones[0].checks[0].type).toBe('verify');
    expect(parsed.milestones[0].checks[0].checkKind).toBe('verify');
  });

  it('should render verify badges in renderMarkdownToHtml', () => {
    const text = `- verify: status is ok`;
    const html = renderMarkdownToHtml(text);
    expect(html).toContain('class="md-verify-item"');
    expect(html).toContain('<span class="verify-badge">verify</span>');
    expect(html).toContain('status is ok');
  });
  it('should render pipe tables as escaped table markup', () => {
    const html = renderMarkdownToHtml('| Bước | **Kết quả** |\n| --- | --- |\n| 1 | <img src=x onerror=alert(1)> |\n\nsau bảng');
    expect(html).toContain('<table><thead><tr><th>Bước</th><th><strong>Kết quả</strong></th></tr></thead>');
    expect(html).toContain('<td>&lt;img src=x onerror=alert(1)&gt;</td>');
    expect(html).not.toContain('<img');
    expect(html).toContain('<div>sau bảng</div>');
  });

  it('should not treat a lone pipe line as a table', () => {
    expect(renderMarkdownToHtml('a | b')).not.toContain('<table');
  });

  it('should keep escaped pipes inside a table cell and normalise row width', () => {
    const html = renderMarkdownToHtml('| Lệnh | Ghi chú |\n| --- | --- |\n| `a\\|b` | ok |\n| chỉ một |\n| 1 | 2 | thừa |');
    expect(html).toContain('<tr><td><code class="inline-code">a|b</code></td><td>ok</td></tr>');
    expect(html).toContain('<tr><td>chỉ một</td><td></td></tr>');
    expect(html).toContain('<tr><td>1</td><td>2</td></tr>');
    expect(html).not.toContain('thừa');
  });

  it('should treat an escaped backslash before a pipe as a column separator', () => {
    const html = renderMarkdownToHtml('| Path | Status |\n| --- | --- |\n| C:\\\\| failed |\n| a\\\\\\|b | ok |');
    expect(html).toContain('<tr><td>C:\\\\</td><td>failed</td></tr>');
    expect(html).toContain('<tr><td>a\\\\|b</td><td>ok</td></tr>');
  });

  it('should keep body rows without a pipe as single-cell rows until a block boundary', () => {
    const html = renderMarkdownToHtml('A | B\n--- | ---\none\ntwo | three\n# Next');
    expect(html).toContain('<tr><td>one</td><td></td></tr>');
    expect(html).toContain('<tr><td>two</td><td>three</td></tr>');
    expect(html).toContain('<h1>Next</h1>');
    const ended = renderMarkdownToHtml('A | B\n--- | ---\none\n- item\n\nafter');
    expect(ended).toContain('<tr><td>one</td><td></td></tr>');
    expect(ended).toContain('<li>item</li>');
    expect(ended).toContain('<div>after</div>');
  });

  it('should not render a table when header and separator widths differ', () => {
    const html = renderMarkdownToHtml('A | B\n--- | --- | ---\n1 | 2 | IMPORTANT');
    expect(html).not.toContain('<table>');
    expect(html).toContain('IMPORTANT');
  });

  it('should only keep a safe language token on code fences', () => {
    const html = renderMarkdownToHtml('```"><img src=x onerror=alert(1)>\ncode\n```\n\n```c++\nx\n```');
    expect(html).not.toContain('<img');
    expect(html).toMatch(/<code class="lang-[A-Za-z0-9_+-]*">code<\/code>/);
    expect(html).toContain('<code class="lang-c++">x</code>');
  });
});
