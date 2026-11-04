import { Component } from '@angular/core';
import { By } from '@angular/platform-browser';
import { TestBed } from '@angular/core/testing';
import { Session } from '../../core/models/session.model';
import { RunSummaryCopyComponent } from './run-summary-copy.component';

@Component({
  standalone: true,
  imports: [RunSummaryCopyComponent],
  template: `
    <app-run-summary-copy
      [session]="session"
      [currentStatus]="'failed'"
      [logs]="logs"
      [recordingUrl]="'/recordings/run.mp4'"
    />
  `
})
class RunSummaryCopyHostComponent {
  public session: Session = {
    session_id: 'run-full-id-12345678',
    initial_goal: 'Do not copy this unredacted goal',
    start_time: 1_791_000_000,
    status: 'failed',
    device_serial: 'pixel-qa-01'
  };
  public logs = [{
    type: 'step_updated',
    data: { step_number: 3, status: 'failed', action_taken: { action: 'tap', status: 'failed' } }
  }];
}

describe('RunSummaryCopyComponent', () => {
  const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');

  afterEach(() => {
    if (originalClipboard) {
      Object.defineProperty(navigator, 'clipboard', originalClipboard);
    } else {
      delete (navigator as { clipboard?: Clipboard }).clipboard;
    }
  });

  it('copies the summary with run details and never includes the unredacted goal', async () => {
    const writeText = jasmine.createSpy('writeText').and.resolveTo(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText }
    });

    await TestBed.configureTestingModule({ imports: [RunSummaryCopyHostComponent] }).compileComponents();
    const fixture = TestBed.createComponent(RunSummaryCopyHostComponent);
    fixture.detectChanges();
    const component = fixture.debugElement.query(By.directive(RunSummaryCopyComponent)).componentInstance as RunSummaryCopyComponent;
    const copySummary = spyOn(component, 'copySummary').and.callThrough();
    (fixture.nativeElement.querySelector('button') as HTMLButtonElement).click();
    await copySummary.calls.mostRecent().returnValue;
    fixture.detectChanges();

    const copiedSummary = writeText.calls.mostRecent().args[0] as string;
    expect(copiedSummary).toContain('- Run ID: run-full-id-12345678');
    expect(copiedSummary).toContain('- Device: pixel-qa-01');
    expect(copiedSummary).toContain('- Outcome: Failed');
    expect(copiedSummary).toContain('- Failing step: Step 3: tap');
    expect(copiedSummary).toContain(`[Open recording](${window.location.origin}/recordings/run.mp4)`);
    expect(copiedSummary).not.toContain('unredacted goal');
    expect(fixture.nativeElement.textContent).toContain('Copied');
  });
});
