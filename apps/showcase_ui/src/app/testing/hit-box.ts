export function expectHitBox(element: HTMLElement): void {
  const box = element.getBoundingClientRect();
  const label = element.getAttribute('aria-label') || element.textContent?.trim() || element.tagName;
  expect(box.width).withContext(`${label}: width`).toBeGreaterThanOrEqual(44);
  expect(box.height).withContext(`${label}: height`).toBeGreaterThanOrEqual(44);
}
