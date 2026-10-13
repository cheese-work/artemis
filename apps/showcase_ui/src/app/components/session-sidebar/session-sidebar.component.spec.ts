import { TestBed, fakeAsync, tick } from '@angular/core/testing';
import { HttpClient } from '@angular/common/http';
import { of } from 'rxjs';
import { RunsService } from '../../services/runs.service';
import { SessionSidebarComponent } from './session-sidebar.component';

describe('Session sidebar', () => {
  let streamSerial = 'phone-a';
  let runStatus = 'running';
  let requests = 0;
  beforeEach(() => {
    streamSerial = 'phone-a';
    runStatus = 'running';
    requests = 0;
    TestBed.configureTestingModule({ imports: [SessionSidebarComponent], providers: [
      { provide: RunsService, useValue: {
        get: () => {
          requests++;
          return of({ session_id: 'run', status: runStatus, start_time: 1, device_ref: { serial: 'phone-a' }, agent_model: 'fixture-model' });
        },
        steps: () => of([{}, {}]),
        video: () => of({ status: 'unavailable', video_segments: [] })
      } },
      { provide: HttpClient, useValue: { get: (url: string) => of(url.includes('device-state')
        ? { serial: streamSerial, live_stream_url: '/api/stream/device-live' }
        : { total_tokens: 42, prompt_tokens: 30, completion_tokens: 12 }) } }
    ] });
  });
  function render() {
    const fixture = TestBed.createComponent(SessionSidebarComponent);
    fixture.componentRef.setInput('runId', 'run');
    fixture.componentRef.setInput('firstPrompt', 'Full first prompt');
    fixture.detectChanges();
    tick(0);
    fixture.detectChanges();
    return fixture;
  }
  it('shows only the bound phone feed, then Details and Stats', fakeAsync(() => {
    const fixture = render();
    expect(fixture.nativeElement.querySelector('.device-feed').getAttribute('src')).toContain('serial=phone-a');
    const buttons = fixture.nativeElement.querySelectorAll('[role="tab"]');
    buttons[1].click();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Full first prompt');
    expect(fixture.nativeElement.textContent).toContain('fixture-model');
    expect(fixture.nativeElement.textContent).toContain('ICT');
    buttons[2].click();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('42');
    expect(fixture.nativeElement.textContent).toContain('2');
    expect(fixture.nativeElement.querySelectorAll('button').length).toBe(3);
    fixture.destroy();
  }));
  it('never falls back to a different connected phone', fakeAsync(() => {
    streamSerial = 'phone-b';
    const fixture = render();
    expect(fixture.nativeElement.querySelector('.device-feed')).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('unavailable for this session phone');
    fixture.destroy();
  }));
  it('supports arrow-key tab selection', fakeAsync(() => {
    const fixture = render();
    fixture.nativeElement.querySelector('[role="tab"]').dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }));
    fixture.detectChanges();
    expect(fixture.componentInstance.tab()).toBe('Details');
    fixture.destroy();
  }));
  it('stops polling after the terminal response', fakeAsync(() => {
    const fixture = render();
    runStatus = 'completed';
    tick(2000);
    expect(requests).toBe(2);
    expect(fixture.componentInstance.run()?.status).toBe('completed');
    tick(10000);
    expect(requests).toBe(2);
    fixture.destroy();
  }));
});
