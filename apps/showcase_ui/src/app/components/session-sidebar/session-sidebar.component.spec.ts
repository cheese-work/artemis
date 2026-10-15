import { TestBed, fakeAsync, tick } from '@angular/core/testing';
import { HttpClient } from '@angular/common/http';
import { of } from 'rxjs';
import { RunsService } from '../../services/runs.service';
import { SessionSidebarComponent } from './session-sidebar.component';

describe('Session sidebar', () => {
  let streamSerial = 'phone-a';
  let runStatus = 'running';
  let requests = 0;
  let usageByTurn: Record<string, unknown> = {};
  beforeEach(() => {
    usageByTurn = {};
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
        : usageByTurn[url.split('/')[3]] ?? { total_tokens: 42, prompt_tokens: 30, completion_tokens: 12 }) } }
    ] });
  });
  function render(turns?: { id: string; number: number }[]) {
    const fixture = TestBed.createComponent(SessionSidebarComponent);
    fixture.componentRef.setInput('runId', 'run');
    if (turns) fixture.componentRef.setInput('turns', turns);
    fixture.componentRef.setInput('firstPrompt', 'Full first prompt');
    fixture.componentRef.setInput('technicalDetails', true);
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
  function statsText(turns?: { id: string; number: number }[]) {
    const fixture = render(turns);
    fixture.nativeElement.querySelectorAll('[role="tab"]')[2].click();
    fixture.detectChanges();
    const text = fixture.nativeElement.textContent as string;
    fixture.destroy();
    return text;
  }
  it('Stats sums tokens and estimated cost across every turn', fakeAsync(() => {
    usageByTurn = {
      t1: { total_tokens: 48212, prompt_tokens: 45930, completion_tokens: 2282, model: 'gpt-6-sol', estimated_cost_usd: 0.14 },
      t2: { total_tokens: 1000, prompt_tokens: 900, completion_tokens: 100, model: 'gpt-6-sol', estimated_cost_usd: 0.02 }
    };
    const text = statsText([{ id: 't1', number: 1 }, { id: 't2', number: 2 }]);
    expect(text).toContain('49,212');
    expect(text).toContain('≈ $0.16');
    expect(text).toContain('est.');
    expect(text).toContain('Turn 1 · gpt-6-sol');
    expect(text).toContain('Turn 2 · gpt-6-sol');
    expect(text).toContain('Estimate at list price; your provider bill may differ.');
  }));
  it('Stats shows a dash and the reason when a model has no list price', fakeAsync(() => {
    usageByTurn = { run: { total_tokens: 48212, prompt_tokens: 45930, completion_tokens: 2282, model: 'llama3.2-vision', estimated_cost_usd: null } };
    const text = statsText();
    expect(text).toContain('48,212');
    expect(text).toContain('No list price for this model.');
    expect(text).not.toContain('$');
  }));
  it('Stats shows no total when one turn is unpriced', fakeAsync(() => {
    usageByTurn = {
      t1: { total_tokens: 100, prompt_tokens: 90, completion_tokens: 10, model: 'gpt-6-sol', estimated_cost_usd: 0.14 },
      t2: { total_tokens: 100, prompt_tokens: 90, completion_tokens: 10, model: 'llama3.2-vision', estimated_cost_usd: null }
    };
    const text = statsText([{ id: 't1', number: 1 }, { id: 't2', number: 2 }]);
    expect(text).toContain('No list price for this model.');
    expect(text).not.toContain('≈ $0.14 est.');
  }));
  it('Stats rounds a tiny cost up to less than one cent', fakeAsync(() => {
    usageByTurn = { run: { total_tokens: 100, prompt_tokens: 90, completion_tokens: 10, model: 'gpt-6-sol', estimated_cost_usd: 0.004 } };
    expect(statsText()).toContain('< $0.01');
  }));
});
