import { HttpClient } from '@angular/common/http';
import { DATE_PIPE_DEFAULT_OPTIONS } from '@angular/common';
import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { VersionFooterComponent } from './version-footer.component';

describe('VersionFooterComponent', () => {
  function render(response: unknown) {
    const get = jasmine.createSpy('get').and.returnValue(
      response instanceof Error ? throwError(() => response) : of(response)
    );
    TestBed.configureTestingModule({
      imports: [VersionFooterComponent],
      providers: [
        { provide: HttpClient, useValue: { get } },
        { provide: DATE_PIPE_DEFAULT_OPTIONS, useValue: { timezone: '-0800' } }
      ]
    });
    const fixture = TestBed.createComponent(VersionFooterComponent);
    fixture.detectChanges();
    return { fixture, get, text: () => (fixture.nativeElement.textContent as string).replace(/\s+/g, ' ').trim() };
  }

  it('shows only the ICT deploy stamp even when the default timezone differs', () => {
    const { fixture, get, text } = render({
      status: 'known',
      sha: '52b9ed0a1b2c3d4e5f60718293a4b5c6d7e8f901',
      short_sha: '52b9ed0',
      deployed_at: '2026-10-05T03:10:00Z'
    });

    expect(get).toHaveBeenCalledOnceWith('/api/system/version');
    expect(text()).toBe('SmartQA 20261005-1010');
    expect(fixture.nativeElement.querySelector('time').getAttribute('datetime')).toBe('2026-10-05T03:10:00Z');
  });

  [
    { deployedAt: '2026-10-05T18:42:00Z', build: '20261006-0142' },
    { deployedAt: '2026-10-05T23:42:00+05:00', build: '20261006-0142' },
    { deployedAt: '2026-12-31T20:59:00Z', build: '20270101-0359' }
  ].forEach(({ deployedAt, build }) => {
    it(`formats ${deployedAt} as ${build} in ICT`, () => {
      const { text } = render({
        status: 'known', short_sha: '52b9ed0', deployed_at: deployedAt, build
      });

      expect(text()).toBe(`SmartQA ${build}`);
    });
  });

  it('shows the sha alone when the deploy time is missing', () => {
    const { text } = render({ status: 'known', sha: 'abc1234', short_sha: 'abc1234', deployed_at: null });

    expect(text()).toBe('SmartQA abc1234');
  });

  it('shows the sha alone when the deploy time is malformed', () => {
    const { text } = render({ status: 'known', short_sha: 'abc1234', deployed_at: 'not-a-time' });

    expect(text()).toBe('SmartQA abc1234');
  });

  it('says the version is unknown when the server does not know it', () => {
    const { text } = render({ status: 'unknown', sha: null, short_sha: null, deployed_at: null });

    expect(text()).toBe('SmartQA version unknown');
  });

  it('says the version is unknown on an HTTP error or malformed payload', () => {
    expect(render(new Error('offline')).text()).toBe('SmartQA version unknown');
    TestBed.resetTestingModule();
    expect(render([]).text()).toBe('SmartQA version unknown');
  });
});
