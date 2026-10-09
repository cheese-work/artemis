import { HttpClient } from '@angular/common/http';
import { TestBed, discardPeriodicTasks, fakeAsync, tick } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { PAGE_BUILD, VersionFooterComponent } from './version-footer.component';

describe('VersionFooterComponent', () => {
  const pageSha = 'abc1234def5678901234567890abcdef12345678';
  const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');

  afterEach(() => {
    if (originalClipboard) {
      Object.defineProperty(navigator, 'clipboard', originalClipboard);
    } else {
      delete (navigator as { clipboard?: Clipboard }).clipboard;
    }
  });

  function render(response: unknown, sha = pageSha) {
    const get = jasmine.createSpy('get').and.callFake(() =>
      response instanceof Error ? throwError(() => response) : of(response)
    );
    TestBed.configureTestingModule({
      imports: [VersionFooterComponent],
      providers: [
        { provide: HttpClient, useValue: { get } },
        { provide: PAGE_BUILD, useValue: { sha, builtAt: '2026-10-09T01:38:04Z' } }
      ]
    });
    const fixture = TestBed.createComponent(VersionFooterComponent);
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    return { fixture, get, root, text: () => (root.textContent as string).replace(/\s+/g, ' ').trim() };
  }

  const server = (sha: string) => ({ status: 'known', sha, short_sha: sha.slice(0, 7), deployed_at: '2026-10-09T01:38:04Z' });

  it('shows only the page build when the server runs the same build', () => {
    const { get, root, text } = render(server(pageSha));

    expect(get).toHaveBeenCalledOnceWith('/api/system/version');
    expect(text()).toBe('Build abc1234');
    expect(root.querySelector('button')!.title).toBe(`Build ${pageSha}, built 2026-10-09 08:38 ICT`);
  });

  it('offers a reload when the server runs a different build', () => {
    const { fixture, root, text } = render(server('ef3a3140000000000000000000000000000000ff'));

    expect(text()).toBe('Build abc1234 · New version available · Reload');
    const reload = spyOn(fixture.componentInstance, 'reload');
    root.querySelector<HTMLButtonElement>('button.reload')!.click();
    expect(reload).toHaveBeenCalled();
  });

  it('shows only the page build when the server build is unknown or the check fails', () => {
    expect(render({ status: 'unknown', sha: null, short_sha: null, deployed_at: null }).text()).toBe('Build abc1234');
    TestBed.resetTestingModule();
    expect(render(new Error('offline')).text()).toBe('Build abc1234');
  });

  it('never offers a reload when the page build is unknown', () => {
    expect(render(server(pageSha), 'unknown').text()).toBe('Build unknown');
  });

  it('checks again on window focus and every 5 minutes', fakeAsync(() => {
    const { fixture, get } = render(server(pageSha));

    window.dispatchEvent(new Event('focus'));
    expect(get).toHaveBeenCalledTimes(2);
    tick(5 * 60_000);
    expect(get).toHaveBeenCalledTimes(3);
    fixture.destroy();
    discardPeriodicTasks();
  }));

  it('copies the full sha and announces it politely', async () => {
    const writeText = jasmine.createSpy('writeText').and.resolveTo(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const { fixture, root } = render(server(pageSha));

    await fixture.componentInstance.copy();
    fixture.detectChanges();

    expect(writeText).toHaveBeenCalledOnceWith(pageSha);
    const status = root.querySelector('[role="status"]')!;
    expect(status.getAttribute('aria-live')).toBe('polite');
    expect(status.textContent).toContain('Build copied');
  });
});
