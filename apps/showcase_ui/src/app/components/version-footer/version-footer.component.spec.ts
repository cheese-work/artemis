import { HttpClient } from '@angular/common/http';
import { DATE_PIPE_DEFAULT_OPTIONS } from '@angular/common';
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
        { provide: DATE_PIPE_DEFAULT_OPTIONS, useValue: { timezone: '-0800' } },
        { provide: PAGE_BUILD, useValue: { sha, builtAt: '2026-10-09T01:38:04Z' } }
      ]
    });
    const fixture = TestBed.createComponent(VersionFooterComponent);
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    const clean = (value: string | null | undefined) => (value ?? '').replace(/\s+/g, ' ').trim();
    return {
      fixture,
      get,
      root,
      text: () => clean(root.textContent),
      stamp: () => clean(root.querySelector('.deployed')?.textContent)
    };
  }

  const server = (sha: string) => ({ status: 'known', sha, short_sha: sha.slice(0, 7), deployed_at: '2026-10-09T01:38:04Z' });

  it('shows only the ICT deploy stamp even when the default timezone differs', () => {
    const { fixture, get, stamp } = render({
      status: 'known',
      sha: '52b9ed0a1b2c3d4e5f60718293a4b5c6d7e8f901',
      short_sha: '52b9ed0',
      deployed_at: '2026-10-05T03:10:00Z'
    });

    expect(get).toHaveBeenCalledOnceWith('/api/system/version');
    expect(stamp()).toBe('SmartQA 20261005-1010');
    expect(fixture.nativeElement.querySelector('time').getAttribute('datetime')).toBe('2026-10-05T03:10:00Z');
  });

  [
    { deployedAt: '2026-10-05T18:42:00Z', build: '20261006-0142' },
    { deployedAt: '2026-10-05T23:42:00+05:00', build: '20261006-0142' },
    { deployedAt: '2026-12-31T20:59:00Z', build: '20270101-0359' }
  ].forEach(({ deployedAt, build }) => {
    it(`formats ${deployedAt} as ${build} in ICT`, () => {
      const { stamp } = render({
        status: 'known', short_sha: '52b9ed0', deployed_at: deployedAt, build
      });

      expect(stamp()).toBe(`SmartQA ${build}`);
    });
  });

  it('shows the sha alone when the deploy time is missing', () => {
    const { stamp } = render({ status: 'known', sha: 'abc1234', short_sha: 'abc1234', deployed_at: null });

    expect(stamp()).toBe('SmartQA abc1234');
  });

  it('shows the sha alone when the deploy time is malformed', () => {
    const { stamp } = render({ status: 'known', short_sha: 'abc1234', deployed_at: 'not-a-time' });

    expect(stamp()).toBe('SmartQA abc1234');
  });

  it('says the version is unknown when the server does not know it', () => {
    const { stamp } = render({ status: 'unknown', sha: null, short_sha: null, deployed_at: null });

    expect(stamp()).toBe('SmartQA version unknown');
  });

  it('says the version is unknown on an HTTP error or malformed payload', () => {
    expect(render(new Error('offline')).stamp()).toBe('SmartQA version unknown');
    TestBed.resetTestingModule();
    expect(render([]).stamp()).toBe('SmartQA version unknown');
  });

  it('shows the page build with no notice when the server runs the same build', () => {
    const { root, text } = render(server(pageSha));

    expect(text()).toBe('SmartQA 20261009-0838 · Build abc1234');
    expect(root.querySelector<HTMLButtonElement>('button.build')!.title).toBe(`Build ${pageSha}, built 2026-10-09 08:38 ICT`);
  });

  it('offers a reload when the server runs a different build', () => {
    const { fixture, root, text } = render(server('ef3a3140000000000000000000000000000000ff'));

    expect(text()).toBe('SmartQA 20261009-0838 · Build abc1234 · New version available · Reload');
    const reload = spyOn(fixture.componentInstance, 'reload');
    root.querySelector<HTMLButtonElement>('button.reload')!.click();
    expect(reload).toHaveBeenCalled();
  });

  it('offers a reload when the server sha shares only the first seven characters', () => {
    const { text } = render(server('abc1234111111111111111111111111111111111'), 'abc1234000000000000000000000000000000000');

    expect(text()).toBe('SmartQA 20261009-0838 · Build abc1234 · New version available · Reload');
  });

  it('shows no notice when the server sends no full sha', () => {
    const { text } = render({ status: 'known', sha: 'ef3a314', short_sha: 'ef3a314', deployed_at: '2026-10-09T01:38:04Z' });

    expect(text()).toBe('SmartQA 20261009-0838 · Build abc1234');
  });

  it('shows no notice when the server build is unknown or the check fails', () => {
    expect(render({ status: 'unknown', sha: null, short_sha: null, deployed_at: null }).text())
      .toBe('SmartQA version unknown · Build abc1234');
    TestBed.resetTestingModule();
    expect(render(new Error('offline')).text()).toBe('SmartQA version unknown · Build abc1234');
  });

  it('never offers a reload when the page build is unknown', () => {
    expect(render(server(pageSha), 'unknown').text()).toBe('SmartQA 20261009-0838 · Build unknown');
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
