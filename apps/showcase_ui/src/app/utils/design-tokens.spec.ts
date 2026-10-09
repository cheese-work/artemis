function toLinear(value: number): number {
  const v = value / 255;
  return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}
function relativeLuminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => toLinear(parseInt(hex.slice(i, i + 2), 16)));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrastRatio(a: string, b: string): number {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

// CHE-1278: one set of tokens for every surface (DESIGN.md). Text colours hold 4.5:1 on the page and on white.
describe('design tokens', () => {
  const style = getComputedStyle(document.documentElement);
  const token = (name: string) => style.getPropertyValue(name).trim();
  const bg = () => token('--color-bg');
  const surface = () => token('--color-surface');

  it('defines every colour as a six-digit hex', () => {
    for (const name of ['bg', 'surface', 'surface-subtle', 'ink', 'text', 'text-muted', 'text-faint', 'rule', 'rule-strong', 'border',
      'primary', 'primary-hover', 'primary-tint', 'primary-edge', 'on-primary', 'focus', 'success', 'success-bg', 'warning', 'warning-bg',
      'error', 'error-bg', 'error-solid', 'neutral', 'neutral-bg', 'device', 'device-edge', 'device-text', 'device-muted']) {
      expect(token(`--color-${name}`)).toMatch(/^#[0-9a-f]{6}$/i, name);
    }
  });

  it('keeps text at 4.5:1 on the page and on the surface', () => {
    for (const name of ['ink', 'text', 'text-muted', 'text-faint', 'primary', 'primary-hover', 'success', 'warning', 'error']) {
      expect(contrastRatio(token(`--color-${name}`), bg())).toBeGreaterThanOrEqual(4.5, `${name} on bg`);
      expect(contrastRatio(token(`--color-${name}`), surface())).toBeGreaterThanOrEqual(4.5, `${name} on surface`);
    }
    for (const name of ['surface-subtle', 'primary-tint']) {
      expect(contrastRatio(token('--color-text-faint'), token(`--color-${name}`))).toBeGreaterThanOrEqual(4.5, `text-faint on ${name}`);
    }
  });

  it('keeps white text on the primary and the solid red at 4.5:1, and the tint readable under ink', () => {
    expect(contrastRatio(token('--color-on-primary'), token('--color-primary'))).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(token('--color-on-primary'), token('--color-primary-hover'))).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(token('--color-on-primary'), token('--color-error-solid'))).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(token('--color-ink'), token('--color-primary-tint'))).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(token('--color-ink'), token('--color-surface-subtle'))).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(token('--color-text-muted'), token('--color-surface-subtle'))).toBeGreaterThanOrEqual(4.5);
  });

  it('keeps control borders and the focus ring at 3:1 on the surface', () => {
    expect(contrastRatio(token('--color-border'), surface())).toBeGreaterThanOrEqual(3);
    expect(contrastRatio(token('--color-focus'), surface())).toBeGreaterThanOrEqual(3);
    expect(contrastRatio(token('--color-focus'), bg())).toBeGreaterThanOrEqual(3);
  });

  it('keeps each status pair at 4.5:1 and reads the same through the review-mode names', () => {
    for (const [tone, name] of [['ok', 'success'], ['warn', 'warning'], ['danger', 'error'], ['neutral', 'neutral']]) {
      expect(contrastRatio(token(`--color-${name}`), token(`--color-${name}-bg`))).toBeGreaterThanOrEqual(4.5, name);
      expect(token(`--status-${tone}-fg`).toLowerCase()).toBe(token(`--color-${name}`).toLowerCase());
      expect(token(`--status-${tone}-bg`).toLowerCase()).toBe(token(`--color-${name}-bg`).toLowerCase());
    }
    expect(token('--evidence-surface').toLowerCase()).toBe(surface().toLowerCase());
    expect(token('--focus-ring').toLowerCase()).toBe(token('--color-focus').toLowerCase());
  });

  it('uses Inter for UI and display text at the Workbench text sizes', () => {
    expect(token('--font-ui')).toMatch(/^['"]Inter['"],/);
    expect(token('--font-display')).toBe(token('--font-ui'));
    expect(['label', 'ui', 'body', 'section', 'title'].map((name) => token(`--text-${name}`))).toEqual(['0.75rem', '0.8125rem', '0.875rem', '1rem', '1.125rem']);
  });

  it('sets the spacing, radius, layout and 44px target scale', () => {
    expect(token('--target')).toBe('44px');
    expect(['xs', 'sm', 'smd', 'md', 'lg', 'xl'].map((n) => token(`--space-${n}`))).toEqual(['4px', '8px', '12px', '16px', '24px', '32px']);
    expect(['sm', 'md', 'lg'].map((name) => token(`--radius-${name}`))).toEqual(['4px', '6px', '8px']);
    expect(['sidebar-w', 'list-w', 'row-h', 'appbar-h', 'tabbar-h'].map((name) => token(`--${name}`))).toEqual(['224px', '360px', '56px', '48px', '56px']);
  });
});
