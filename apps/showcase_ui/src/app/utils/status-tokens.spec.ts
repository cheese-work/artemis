// Contrast is measured on the real stylesheet: the tokens in styles.scss are the
// colours the library and viewer use, on the opaque surface they sit on.
function channel(value: number): number {
  const c = value / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const n = parseInt(hex.replace('#', ''), 16);
  return 0.2126 * channel((n >> 16) & 255) + 0.7152 * channel((n >> 8) & 255) + 0.0722 * channel(n & 255);
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

describe('status colour tokens', () => {
  const style = getComputedStyle(document.documentElement);
  const token = (name: string) => style.getPropertyValue(name).trim();
  const surface = () => token('--evidence-surface');

  it('defines one foreground and one tint per status, plus an opaque evidence surface', () => {
    expect(surface()).toMatch(/^#[0-9a-f]{6}$/i);
    for (const tone of ['ok', 'warn', 'danger', 'neutral']) {
      expect(token(`--status-${tone}-fg`)).toMatch(/^#[0-9a-f]{6}$/i, tone);
      expect(token(`--status-${tone}-bg`)).toMatch(/^#[0-9a-f]{6}$/i, tone);
    }
  });

  it('keeps normal text at 4.5:1 or better on its own tint and on the evidence surface', () => {
    for (const tone of ['ok', 'warn', 'danger', 'neutral']) {
      expect(contrast(token(`--status-${tone}-fg`), token(`--status-${tone}-bg`))).toBeGreaterThanOrEqual(4.5, tone);
      expect(contrast(token(`--status-${tone}-fg`), surface())).toBeGreaterThanOrEqual(4.5, tone);
    }
  });

  it('keeps body text and UI boundaries on the evidence surface readable', () => {
    expect(contrast(token('--evidence-text'), surface())).toBeGreaterThanOrEqual(4.5);
    expect(contrast(token('--evidence-muted'), surface())).toBeGreaterThanOrEqual(4.5);
    expect(contrast(token('--evidence-border'), surface())).toBeGreaterThanOrEqual(3);
  });
});
