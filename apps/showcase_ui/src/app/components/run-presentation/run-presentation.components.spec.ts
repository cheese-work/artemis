import { Type } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Capture, Playback, Transfer, mapRecording } from '../../utils/recording-state.util';
import { tinyVideoUrl } from '../run-view/tiny-video.testing';
import { RunStatusBadgeComponent } from './run-status-badge.component';
import { RunDeviceLabelComponent } from './run-device-label.component';
import { RunStepRowComponent } from './run-step-row.component';
import { RunEvidencePanelComponent } from './run-evidence-panel.component';
import { RunActionBarComponent } from './run-action-bar.component';

describe('shared run presentation', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [RunStatusBadgeComponent, RunDeviceLabelComponent, RunStepRowComponent,
        RunEvidencePanelComponent, RunActionBarComponent]
    }).compileComponents();
  });

  function render<ComponentType>(component: Type<ComponentType>, inputs: Record<string, unknown>, className = ''): ComponentFixture<ComponentType> {
    const fixture = TestBed.createComponent(component);
    fixture.nativeElement.className = className;
    for (const [name, value] of Object.entries(inputs)) fixture.componentRef.setInput(name, value);
    fixture.detectChanges();
    return fixture;
  }

  const statuses = [
    ['pending', 'pending', 'Queued', 'schedule', 'neutral'],
    ['running', 'running', 'Running', 'play_circle', 'neutral'],
    ['paused', 'paused', 'Paused', 'pause_circle', 'neutral'],
    ['completed', 'completed', 'Passed', 'check_circle', 'ok'],
    ['success', 'completed', 'Passed', 'check_circle', 'ok'],
    ['failed', 'failed', 'Failed', 'cancel', 'danger'],
    ['interrupted', 'interrupted', 'Interrupted', 'warning', 'warn'],
    ['cancelled', 'cancelled', 'Cancelled', 'block', 'neutral'],
    ['future_status', 'unknown', 'Unknown', 'help', 'neutral']
  ];
  for (const [status, key, label, icon, tone] of statuses) {
    it(`renders ${status} with the viewer's existing label, icon and tone`, () => {
      const fixture = render(RunStatusBadgeComponent, { status }, 'outcome-badge');
      const root = fixture.nativeElement as HTMLElement;
      expect(root.classList.contains('outcome-badge')).toBeTrue();
      expect(root.classList.contains(`tone-${tone}`)).toBeTrue();
      expect(root.textContent!.replace(/\s+/g, ' ').trim()).toBe(`${icon} ${label}`);
      expect(root.querySelector('span')!.getAttribute('aria-hidden')).toBe('true');
    });

    it(`renders ${status} with the stream's existing badge markup`, () => {
      const fixture = render(RunStatusBadgeComponent, { status, presentation: 'stream' }, 'task-badge');
      const root = fixture.nativeElement as HTMLElement;
      expect(root.classList.contains('task-badge')).toBeTrue();
      expect(root.classList.contains(key)).toBeTrue();
      expect(root.textContent!.trim()).toBe(label);
      expect(root.querySelector('.material-symbols-outlined')).toBeNull();
      expect(root.querySelector('.status-dot') !== null).toBe(status === 'running');
      if (status === 'running') expect(getComputedStyle(root.querySelector('.status-dot')!).display).toBe('none');
    });
  }

  it('updates an active badge without leaving stale classes or icons', () => {
    const fixture = render(RunStatusBadgeComponent, { status: null, liveStatus: 'running' });
    fixture.componentRef.setInput('status', 'interrupted');
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.classList.contains('tone-warn')).toBeTrue();
    expect(root.classList.contains('tone-neutral')).toBeFalse();
    expect(root.textContent).toContain('Interrupted');
  });

  for (const [serial, device, detail, expected] of [
    ['127.0.0.1:5555', { serial: '127.0.0.1:5555', model: 'Pixel 9', device_kind: 'phone' }, false, 'Pixel 9'],
    ['emulator-5554', { serial: 'emulator-5554', model: null, device_kind: 'emulator' }, false, 'Emulator'],
    ['127.0.0.1:5555', null, false, 'Phone via a browser'],
    ['usb-serial', null, false, 'usb-serial'],
    ['127.0.0.1:5555', null, true, '127.0.0.1:5555'],
    [null, null, true, 'Unknown phone']
  ] as const) {
    it(`preserves the device label ${expected}`, () => {
      const fixture = render(RunDeviceLabelComponent, { serial, device, detail });
      const root = fixture.nativeElement as HTMLElement;
      expect(detail ? root.textContent!.trim() : root.querySelector('.device-name')!.textContent).toBe(expected);
      expect(root.querySelector('.device-icon') === null).toBe(detail);
    });
  }

  it('retains browser-relative device wording', () => {
    const fixture = render(RunDeviceLabelComponent, { serial: 'localhost:5555', ownBrowser: true });
    expect(fixture.nativeElement.textContent).toContain('Phone via this browser');
  });

  it('renders a step title, outcome, duration and failure detail from plain inputs', () => {
    const fixture = render(RunStepRowComponent, { stepNumber: 4, title: 'Tap settings', failed: true,
      duration: '1.5s', failureDetail: 'Target not found' });
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('.step-number')!.textContent).toBe('Step 4');
    expect(root.querySelector('.step-title')!.textContent).toBe('Tap settings');
    expect(root.querySelector('.step-failed')!.textContent).toContain('Failed');
    expect(root.querySelector('.step-duration')!.textContent).toBe('1.5s');
    expect(root.querySelector('.step-failure-detail')!.textContent).toBe('Target not found');
  });

  it('does not invent failure or duration rows for legacy steps', () => {
    const fixture = render(RunStepRowComponent, { stepNumber: 1, title: 'Tap' });
    expect(fixture.nativeElement.querySelector('.step-failed')).toBeNull();
    expect(fixture.nativeElement.querySelector('.step-duration')).toBeNull();
    expect(fixture.nativeElement.querySelector('.step-failure-detail')).toBeNull();
  });

  for (const title of ['Checked', 'Worked']) {
    it(`preserves the ${title} phase row`, () => {
      const fixture = render(RunStepRowComponent, { presentation: 'phase', title, duration: 2.5 });
      expect(fixture.nativeElement.querySelector('.phase-worked-time').textContent).toBe(`${title} for 2.5s`);
      expect(fixture.nativeElement.querySelector('.step-number')).toBeNull();
    });
  }

  const recordings: Array<[Capture, Transfer, Playback, string, string]> = [
    ['recording', null, null, 'in_progress', 'Recording in progress'],
    ['pending', null, null, 'pending', 'The recording has not started yet.'],
    ['stopped', 'waiting_for_computer', null, 'waiting_for_computer', 'Video will appear when your computer reconnects.'],
    ['stopped', 'uploading', null, 'uploading', 'Uploading video…'],
    ['stopped', 'failed', null, 'upload_failed', 'Video upload failed. Retry upload runs from the computer.'],
    ['stopped', 'uploaded', 'processing', 'preparing', 'Preparing video…'],
    ['stopped', 'uploaded', 'failed', 'prepare_failed', 'The video could not be prepared.'],
    ['stopped', 'uploaded', null, 'uploaded_unchecked', 'Video uploaded. Checking that it can play…'],
    ['stopped', 'uploaded', 'ready', 'ready', 'Recording ready.'],
    ['partial', 'uploaded', 'ready', 'partial', 'Partial recording (stopped at 01:05)'],
    ['missing:device_offline', null, null, 'missing', 'No recording: the phone went offline.'],
    [null, null, 'unavailable', 'none', 'No recording for this run.'],
    [null, null, null, 'unknown', 'Recording status unknown.']
  ];
  for (const [capture, transfer, playback, state, copy] of recordings) {
    it(`renders the existing ${state} evidence state`, () => {
      const recording = mapRecording({ capture, transfer, playback }, { stoppedAtSeconds: 65 });
      const fixture = render(RunEvidencePanelComponent, { recording, videoUrl: tinyVideoUrl(),
        screenshotUrl: '/images/step.png', screenshotAlt: 'Screenshot for step 3', loaded: true });
      const root = fixture.nativeElement as HTMLElement;
      expect(recording.state).toBe(state);
      expect(recording.copy).toBe(copy);
      expect(root.querySelector('video') !== null).toBe(recording.playable);
      if (recording.playable) {
        expect(root.querySelector('video')!.hasAttribute('controls')).toBeTrue();
        expect(root.querySelector('video')!.getAttribute('controlsList')).toBe('nodownload');
        expect(root.querySelector('video')!.hasAttribute('playsinline')).toBeTrue();
        expect(root.querySelector('.recording-ribbon')?.textContent ?? null).toBe(recording.ribbon);
      } else {
        expect(root.querySelector('.recording-copy')!.textContent!.trim()).toBe(copy);
        expect(root.querySelector('.recording-copy')!.getAttribute('role')).toBe('status');
        expect(root.querySelector('img')!.getAttribute('alt')).toBe('Screenshot for step 3');
      }
    });
  }

  it('emits player events and lets the controller decide recovery', () => {
    const fixture = render(RunEvidencePanelComponent, { recording: mapRecording({ capture: 'stopped', transfer: 'uploaded', playback: 'ready' }), videoUrl: tinyVideoUrl() });
    const metadata = jasmine.createSpy('metadata');
    const ended = jasmine.createSpy('ended');
    const failed = jasmine.createSpy('failed');
    fixture.componentInstance.metadata.subscribe(metadata);
    fixture.componentInstance.ended.subscribe(ended);
    fixture.componentInstance.failed.subscribe(failed);
    const player = fixture.nativeElement.querySelector('video') as HTMLVideoElement;
    player.dispatchEvent(new Event('loadedmetadata'));
    player.dispatchEvent(new Event('ended'));
    player.dispatchEvent(new Event('error'));
    expect(metadata).toHaveBeenCalled();
    expect(ended).toHaveBeenCalled();
    expect(failed).toHaveBeenCalled();
    expect(fixture.componentInstance.player()!.nativeElement).toBe(player);
  });

  it('preserves playback-error copy, screenshot fallback, retry and the empty evidence message', () => {
    const fixture = render(RunEvidencePanelComponent, { recording: mapRecording({ capture: 'stopped', transfer: 'uploaded', playback: 'ready' }),
      videoUrl: tinyVideoUrl(), playerFailed: true, message: 'The video could not be played. Steps and screenshots are still here.', retryable: true, loaded: true });
    const retry = jasmine.createSpy('retry');
    fixture.componentInstance.retry.subscribe(retry);
    expect(fixture.nativeElement.querySelector('video')).toBeNull();
    expect(fixture.nativeElement.querySelector('.recording-copy').textContent).toContain('could not be played');
    expect(fixture.nativeElement.querySelector('.muted').textContent).toBe('No screenshots for this run.');
    fixture.nativeElement.querySelector('button').click();
    expect(retry).toHaveBeenCalled();
  });

  const triggers = [
    [true, 'processing', '/ready.mp4', true, 'videocam', 'Recording...'],
    [false, 'processing', '/ready.mp4', true, 'progress_activity', 'Preparing...'],
    [false, 'failed', '/ready.mp4', true, 'smart_display', 'Play Recording'],
    [false, 'failed', null, true, 'slideshow', 'Step Replay'],
    [false, 'failed', null, false, 'videocam_off', 'Recording Failed'],
    [false, null, null, false, 'videocam', 'Screen Recording']
  ] as const;
  for (const [running, playbackStatus, videoUrl, hasSteps, icon, label] of triggers) {
    it(`preserves the workspace ${label} evidence launcher`, () => {
      const fixture = render(RunEvidencePanelComponent, { presentation: 'trigger', running, playbackStatus, videoUrl, hasSteps, screenshotCount: 3 }, 'btn-screen-recording');
      expect(fixture.nativeElement.querySelector('.video-icon').textContent).toBe(icon);
      expect(fixture.nativeElement.querySelector('.btn-text').textContent).toBe(label);
      expect(fixture.nativeElement.querySelector('.video-pulse-dot') !== null).toBe(running);
      expect(fixture.nativeElement.querySelector('.steps-count-badge') !== null).toBe(!running && !videoUrl && hasSteps);
      expect(fixture.nativeElement.querySelector('video')).toBeNull();
    });
  }

  it('keeps action order and returns the original native event for controller trust dialogs', () => {
    const fixture = render(RunActionBarComponent, { actions: [
      { id: 'copy-id', label: 'ID: 12345678', ariaLabel: 'Copy full run ID 12345678' },
      { id: 'copy-summary', label: 'Copy run summary', icon: 'content_copy' },
      { id: 'share', label: 'Copy link' }, { id: 'download', label: 'Download' },
      { id: 'pin', label: 'Unpin', pressed: true }
    ] }, 'actions');
    const buttons = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('button'));
    expect(buttons.map(control => control.querySelector('span:last-child')!.textContent)).toEqual(['ID: 12345678', 'Copy run summary', 'Copy link', 'Download', 'Unpin']);
    expect(buttons[0].getAttribute('aria-label')).toBe('Copy full run ID 12345678');
    expect(buttons[4].getAttribute('aria-pressed')).toBe('true');
    const clicked: string[] = [];
    fixture.componentInstance.action.subscribe(action => {
      clicked.push(action.id);
      expect(action.event.currentTarget).toBe(buttons[clicked.length - 1]);
    });
    buttons.forEach(control => control.click());
    expect(clicked).toEqual(['copy-id', 'copy-summary', 'share', 'download', 'pin']);
  });

  it('renders feedback and retryable failures without performing any action itself', () => {
    const fixture = render(RunActionBarComponent, { actions: [], feedback: 'Copied', error: 'Download failed', retryable: true });
    expect(fixture.nativeElement.querySelector('.action-feedback').getAttribute('role')).toBe('status');
    expect(fixture.nativeElement.querySelector('.action-error').getAttribute('role')).toBe('alert');
    const action = jasmine.createSpy('action');
    fixture.componentInstance.action.subscribe(action);
    fixture.nativeElement.querySelector('button').click();
    expect(action.calls.mostRecent().args[0].id).toBe('retry');
  });
});
