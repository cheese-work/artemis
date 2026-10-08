import { HttpClient } from '@angular/common/http';
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
      providers: [{ provide: HttpClient, useValue: { get } }]
    });
    const fixture = TestBed.createComponent(VersionFooterComponent);
    fixture.detectChanges();
    return { fixture, get, text: () => (fixture.nativeElement.textContent as string).replace(/\s+/g, ' ').trim() };
  }

  it('shows the short sha and deploy time from the version endpoint', () => {
    const { fixture, get, text } = render({
      status: 'known',
      sha: '52b9ed0a1b2c3d4e5f60718293a4b5c6d7e8f901',
      short_sha: '52b9ed0',
      deployed_at: '2026-10-05T03:10:00Z'
    });

    expect(get).toHaveBeenCalledOnceWith('/api/system/version');
    expect(text()).toContain('SmartQA 52b9ed0, deployed');
    expect(fixture.nativeElement.querySelector('time').getAttribute('datetime')).toBe('2026-10-05T03:10:00Z');
  });

  it('shows the sha alone when the deploy time is missing', () => {
    const { text } = render({ status: 'known', sha: 'abc1234', short_sha: 'abc1234', deployed_at: null });

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
