import { Component, signal } from '@angular/core';
import { HttpErrorResponse, HttpEventType, HttpHeaderResponse, HttpHeaders, HttpResponse } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Observable, Subject, of, throwError } from 'rxjs';
import { AdminConfigService } from '../../services/admin-config.service';
import { RunSummary, SessionVideo } from '../../core/models/run.model';
import { StepItemData } from '../../core/models/stream.model';
import { RunsService } from '../../services/runs.service';
import { SELECTED_DEVICE_SERIAL_KEY } from '../../services/system.service';
import { RunViewerComponent } from './run-viewer.component';
import { tinyVideoUrl } from './tiny-video.testing';

@Component({ standalone: true, template: 'stub' })
class StubComponent {}

const ID = '3f2b9c1a-5d7e-4a10-9c33-0e1f2a3b4c5d';
const START = Date.UTC(2026, 9, 4, 12) / 1000;
const NOW = Math.floor(Date.now() / 1000);

const run = (over: Partial<RunSummary> = {}): RunSummary => ({
  session_id: ID,
  prompt: 'Log in and open settings',
  status: 'completed',
  interrupt_reason: null,
  start_time: START,
  end_time: START + 300,
  host_id: null,
  device_ref: { host_id: null, serial: 'emulator-5554' },
  requested_by: 'qa@example.test',
  pinned: false,
  recordings: [{ recording_id: 'r1', capture: 'stopped', transfer: 'uploaded' }],
  ...over
});

const step = (n: number, over: Partial<StepItemData> = {}): StepItemData => ({
  step_id: `st${n}`,
  step_number: n,
  session_id: ID,
  timestamp: START + n * 10,
  action_taken: { action: 'tap' },
  pre_image_name: `pre${n}.png`,
  post_image_name: `post${n}.png`,
  ...over
});

const ready = (over: Partial<SessionVideo> = {}): SessionVideo => ({
  session_id: ID,
  status: 'ready',
  has_video: true,
  video_url: VIDEO_URL,
  video_segments: [],
  ...over
});

// Real, decodable media: a 404 for a fake URL would fire the element's error event and swap in the fallback.
const VIDEO_URL = tinyVideoUrl();
const VIDEO_A = tinyVideoUrl();
const VIDEO_B = tinyVideoUrl();

const httpError = (status: number, body: unknown = {}) =>
  throwError(() => new HttpErrorResponse({ status, error: body }));

describe('RunViewerComponent', () => {
  let runs: jasmine.SpyObj<RunsService>;
  let admin: jasmine.SpyObj<AdminConfigService>;
  let fixture: ComponentFixture<RunViewerComponent>;
  let router: Router;
  let root: HTMLElement;
  let clipboard: jasmine.Spy;

  const q = <T extends Element>(selector: string) => root.querySelector<T>(selector);
  const qa = <T extends Element>(selector: string) => Array.from(root.querySelectorAll<T>(selector));
  const button = (label: string) =>
    qa<HTMLButtonElement>('button').find((b) => (b.textContent ?? '').replace(/\s+/g, ' ').trim().startsWith(label))!;

  async function settle() {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  async function open(
    {
      runResult = of(run()) as Observable<RunSummary>,
      steps = of([step(1), step(2), step(3)]) as Observable<StepItemData[]>,
      video = of(ready()) as Observable<SessionVideo>,
      isAdmin = false,
      id = ID
    } = {}
  ) {
    runs.get.and.returnValue(runResult);
    runs.steps.and.returnValue(steps);
    runs.video.and.returnValue(video);
    admin.getIdentity.and.returnValue(
      of({ email: 'a@x.test', admin: isAdmin, auth_mode: 'cloudflare', reason: null })
    );
    fixture = TestBed.createComponent(RunViewerComponent);
    fixture.componentRef.setInput('runId', id);
    root = fixture.nativeElement;
    await settle();
  }

  beforeEach(async () => {
    localStorage.removeItem(SELECTED_DEVICE_SERIAL_KEY);
    runs = jasmine.createSpyObj<RunsService>(
      'RunsService',
      ['get', 'steps', 'video', 'pin', 'unpin', 'remove', 'downloadBundle'],
      { lastLibraryQuery: signal<Record<string, string>>({ status: 'failed' }) }
    );
    admin = jasmine.createSpyObj<AdminConfigService>('AdminConfigService', ['getIdentity']);
    clipboard = spyOn(navigator.clipboard, 'writeText').and.resolveTo();
    await TestBed.configureTestingModule({
      imports: [RunViewerComponent],
      providers: [
        provideRouter([
          { path: 'runs', component: StubComponent },
          { path: 'runs/:id', component: StubComponent },
          { path: 'workspace', component: StubComponent }
        ]),
        { provide: RunsService, useValue: runs },
        { provide: AdminConfigService, useValue: admin }
      ]
    }).compileComponents();
    router = TestBed.inject(Router);
  });

  describe('reading order', () => {
    it('leads with outcome and prompt, then evidence with steps, then actions, then technical details', async () => {
      await open();
      const order = qa('[data-section], details.technical-details').map(
        (el) => el.getAttribute('data-section') ?? 'technical'
      );
      expect(order).toEqual(['outcome', 'evidence', 'steps', 'actions', 'technical']);
      const outcome = q('[data-section="outcome"]')!;
      expect(outcome.querySelector('.outcome-badge')!.textContent).toContain('Passed');
      expect(outcome.querySelector('.outcome-badge .material-symbols-outlined')).not.toBeNull();
      expect(outcome.querySelector('h1.run-prompt')!.textContent).toContain('Log in and open settings');
    });

    it('requests the run first, then its steps and playback by the resolved full id', async () => {
      await open({ id: '3f2b9c1a' });
      expect(runs.get).toHaveBeenCalledWith('3f2b9c1a');
      expect(runs.steps).toHaveBeenCalledWith(ID);
      expect(runs.video).toHaveBeenCalledWith(ID);
    });

    it('never touches the selected device or the live session', async () => {
      localStorage.setItem(SELECTED_DEVICE_SERIAL_KEY, 'R58M123');
      await open();
      expect(localStorage.getItem(SELECTED_DEVICE_SERIAL_KEY)).toBe('R58M123');
    });

    it('links back to the library with the remembered filters', async () => {
      await open();
      const back = q<HTMLAnchorElement>('a.back-to-runs')!;
      expect(back.getAttribute('href')).toBe('/runs?status=failed');
    });
  });

  describe('page states', () => {
    it('shows a skeleton while the run loads', async () => {
      await open({ runResult: new Subject<RunSummary>() });
      expect(q('.skeleton')).not.toBeNull();
      expect(q('[data-section="outcome"]')).toBeNull();
    });

    it('shows "Run not found" with a way back for an unknown id', async () => {
      await open({ runResult: httpError(404, { error: 'not_found' }) });
      expect(q('.state-page h1')!.textContent).toContain('Run not found');
      expect(q('.state-page a.back-to-runs')).not.toBeNull();
    });

    it('shows "This run was removed" and the reason for a removed run, not a blank screen', async () => {
      await open({ runResult: httpError(410, { error: 'removed', reason: 'retention', deleted_at: START }) });
      expect(q('.state-page h1')!.textContent).toContain('This run was removed');
      expect(q('.state-page')!.textContent).toContain('retention');
      expect(q('.state-page a.back-to-runs')).not.toBeNull();
    });

    for (const status of [401, 403, 0]) {
      it(`shows the access page with a sign-in link back to this run when the check answers ${status}`, async () => {
        await open({ runResult: httpError(status) });
        expect(q('.state-page h1')!.textContent).toContain('Sign in to open this run');
        expect(q<HTMLAnchorElement>('.state-page a.sign-in')!.getAttribute('href')).toBe(`/runs/${ID}`);
      });
    }

    it('lists candidates when a short id matches several runs', async () => {
      await open({
        id: '3f2b9c1a',
        runResult: httpError(409, { error: 'ambiguous_prefix', candidates: [ID, '3f2b9c1a-0000-4000-8000-000000000000'] })
      });
      expect(q('.state-page h1')!.textContent).toContain('More than one run');
      expect(qa('.state-page a.candidate').map((a) => a.getAttribute('href'))).toEqual([
        `/runs/${ID}`,
        '/runs/3f2b9c1a-0000-4000-8000-000000000000'
      ]);
    });

    it('shows "Couldn\'t load this run" with Retry that asks again', async () => {
      await open({ runResult: httpError(500) });
      expect(q('.state-page h1')!.textContent).toContain("Couldn't load this run");
      runs.get.and.returnValue(of(run()));
      button('Retry').click();
      await settle();
      expect(q('[data-section="outcome"]')).not.toBeNull();
    });
  });

  describe('evidence', () => {
    it('is never blocked on video: steps and screenshot show while playback is still loading', async () => {
      await open({ video: new Subject<SessionVideo>() });
      expect(qa('ol.step-list li').length).toBe(3);
      expect(q('img.evidence-image')).not.toBeNull();
    });

    it('plays a ready recording in an opaque video element without the native download control', async () => {
      await open();
      const video = q<HTMLVideoElement>('video')!;
      expect(video.getAttribute('src')).toBe(VIDEO_URL);
      expect(video.hasAttribute('controls')).toBe(true);
      expect(video.getAttribute('controlslist')).toContain('nodownload');
      expect(q('.recording-ribbon')).toBeNull();
    });

    it('plays a partial recording with the "stopped at mm:ss" ribbon', async () => {
      await open({
        runResult: of(run({ recordings: [{ recording_id: 'r1', capture: 'partial', transfer: 'uploaded' }] })),
        video: of(ready({ video_segments: [{ url: VIDEO_A, duration: 40 }, { url: VIDEO_B, duration: 25 }] }))
      });
      expect(q('video')).not.toBeNull();
      expect(q('.recording-ribbon')!.textContent).toContain('Partial recording (stopped at 01:05)');
    });

    it('falls back to the screenshot and says why there is no video, per the mapping function', async () => {
      await open({
        runResult: of(run({ recordings: [{ recording_id: 'r1', capture: 'stopped', transfer: 'waiting_for_computer' }] })),
        video: of({ session_id: ID, status: 'unavailable', has_video: false, video_url: null, video_segments: [] })
      });
      expect(q('video')).toBeNull();
      expect(q('.recording-copy')!.textContent).toContain('Video will appear when your computer reconnects.');
      expect(q('img.evidence-image')!.getAttribute('src')).toContain('post3.png');
    });

    it('does not treat uploaded as ready: it says preparing and offers to check again', async () => {
      await open({
        video: of({ session_id: ID, status: 'unavailable', has_video: false, video_url: null, video_segments: [] })
      });
      expect(q('video')).toBeNull();
      expect(q('.recording-copy')!.textContent).toContain('Preparing video…');
      runs.video.and.returnValue(of(ready()));
      button('Check again').click();
      await settle();
      expect(q('video')).not.toBeNull();
    });

    it('says "No recording for this run" for a legacy run that never had one', async () => {
      await open({
        runResult: of(run({ recordings: [] })),
        video: of({ session_id: ID, status: 'unavailable', has_video: false, video_url: null, video_segments: [] })
      });
      expect(q('.recording-copy')!.textContent).toContain('No recording for this run.');
    });

    it('selects a step from the timeline and shows its screenshot', async () => {
      await open({ video: of({ session_id: ID, status: 'unavailable', has_video: false, video_url: null, video_segments: [] }) });
      const stepButtons = qa<HTMLButtonElement>('ol.step-list button.step-button');
      expect(stepButtons.length).toBe(3);
      expect(stepButtons[2].getAttribute('aria-current')).toBe('step');
      stepButtons[0].click();
      await settle();
      expect(stepButtons[0].getAttribute('aria-current')).toBe('step');
      expect(stepButtons[2].getAttribute('aria-current')).toBeNull();
      expect(q('img.evidence-image')!.getAttribute('src')).toContain('post1.png');
    });

    it('moves to the segment that holds the selected step\'s time', async () => {
      await open({
        video: of(
          ready({
            video_segments: [
              { url: VIDEO_A, duration: 15, start: 0, offset_ms: 0, duration_ms: 15000 },
              { url: VIDEO_B, duration: 20, start: 15, offset_ms: 20000, duration_ms: 20000 }
            ]
          })
        )
      });
      qa<HTMLButtonElement>('ol.step-list button.step-button')[2].click(); // 30 s into the session
      await settle();
      expect(fixture.componentInstance.activeSegmentIndex()).toBe(1);
      expect(q('video')!.getAttribute('src')).toBe(VIDEO_B);
    });
  });

  describe('interrupted runs', () => {
    const interrupted = () =>
      run({ status: 'interrupted', interrupt_reason: 'host_disconnected', recordings: [{ recording_id: 'r1', capture: 'partial', transfer: 'uploaded' }] });

    it('shows the agreed sentence and the reason in QA language, in the viewer and not as a toast', async () => {
      await open({ runResult: of(interrupted()) });
      const banner = q('.interrupted-banner')!;
      expect(banner.getAttribute('role')).toBe('status');
      expect(banner.textContent).toContain(
        'Run interrupted at step 3. Recording is partial. Reconnecting will not resume this run.'
      );
      expect(banner.textContent).toContain('Your computer lost its connection to SmartQA.');
    });

    it('has a reason for every interrupt reason, and a safe sentence for an unknown one', async () => {
      for (const [reason, text] of [
        ['bridge_closed', 'The browser tab holding the phone closed.'],
        ['device_offline', 'The phone went offline.'],
        ['server_restarted', 'SmartQA restarted.'],
        ['auth_expired', 'The computer\'s session expired.'],
        ['brand_new_reason', 'The run stopped before it finished.']
      ]) {
        await open({ runResult: of(run({ status: 'interrupted', interrupt_reason: reason })) });
        expect(q('.interrupted-banner')!.textContent).toContain(text);
        fixture.destroy();
      }
    });

    it('starts a new run with the same prompt by going to Workspace with the prompt as a draft', async () => {
      await open({ runResult: of(interrupted()) });
      spyOn(router, 'navigate').and.resolveTo(true);
      button('Start new run with this prompt').click();
      expect(router.navigate).toHaveBeenCalledWith(['/workspace'], { state: { draftPrompt: 'Log in and open settings' } });
    });

    it('does not show the banner for a run that finished', async () => {
      await open();
      expect(q('.interrupted-banner')).toBeNull();
    });
  });

  describe('trust notices before share and download', () => {
    const NOTICE = 'Videos and screenshots are not redacted and may contain sensitive information.';
    const CF = "Anyone who passes this site's Cloudflare Access check can open this run.";

    it('asks before copying the link, shows both notices, and copies the full-id link only on confirm', async () => {
      await open();
      button('Copy link').click();
      await settle();
      const dialog = q<HTMLDialogElement>('dialog.trust-dialog')!;
      expect(dialog.open).toBe(true);
      expect(dialog.textContent).toContain(NOTICE);
      expect(dialog.textContent).toContain(CF);
      expect(clipboard).not.toHaveBeenCalled();
      dialog.querySelector<HTMLButtonElement>('.dialog-confirm')!.click();
      await settle();
      expect(clipboard).toHaveBeenCalledOnceWith(`${window.location.origin}/runs/${ID}`);
      expect(dialog.open).toBe(false);
      expect(q('.action-feedback')!.textContent).toContain('Link copied');
    });

    it('asks before downloading, shows only the redaction notice, and downloads on confirm', async () => {
      await open();
      button('Download').click();
      await settle();
      const dialog = q<HTMLDialogElement>('dialog.trust-dialog')!;
      expect(dialog.textContent).toContain(NOTICE);
      expect(dialog.textContent).not.toContain('Cloudflare Access');
      expect(runs.downloadBundle).not.toHaveBeenCalled();
      const events = new Subject<unknown>();
      runs.downloadBundle.and.returnValue(events as never);
      dialog.querySelector<HTMLButtonElement>('.dialog-confirm')!.click();
      await settle();
      expect(runs.downloadBundle).toHaveBeenCalledWith(ID);
      expect(q('.action-feedback')!.textContent).toContain('Preparing bundle');
    });

    it('shows the size once the server reports it, saves the file, and reports done', async () => {
      await open();
      const events = new Subject<unknown>();
      runs.downloadBundle.and.returnValue(events as never);
      const created = spyOn(URL, 'createObjectURL').and.returnValue('blob:x');
      spyOn(URL, 'revokeObjectURL');
      const click = spyOn(HTMLAnchorElement.prototype, 'click').and.stub();
      button('Download').click();
      await settle();
      q<HTMLButtonElement>('.dialog-confirm')!.click();
      await settle();
      events.next(new HttpHeaderResponse({ status: 200, headers: new HttpHeaders({ 'Content-Length': String(84 * 1024 * 1024) }) }));
      await settle();
      expect(q('.action-feedback')!.textContent).toContain('84 MB');
      events.next(new HttpResponse({ status: 200, body: new Blob(['zip']) }));
      events.complete();
      await settle();
      expect(created).toHaveBeenCalled();
      expect(click).toHaveBeenCalled();
      expect(q('.action-feedback')!.textContent).toContain('Download started');
      expect(HttpEventType.Response).toBeDefined();
    });

    it('offers Retry when a download fails, and Retry downloads again without asking twice', async () => {
      await open();
      runs.downloadBundle.and.returnValue(httpError(500) as never);
      button('Download').click();
      await settle();
      q<HTMLButtonElement>('.dialog-confirm')!.click();
      await settle();
      expect(q('.action-error')!.textContent).toContain('Download failed');
      runs.downloadBundle.and.returnValue(of(new HttpResponse({ status: 200, body: new Blob(['zip']) })) as never);
      spyOn(URL, 'createObjectURL').and.returnValue('blob:x');
      spyOn(URL, 'revokeObjectURL');
      spyOn(HTMLAnchorElement.prototype, 'click').and.stub();
      button('Retry').click();
      await settle();
      expect(runs.downloadBundle).toHaveBeenCalledTimes(2);
      expect(q('.action-error')).toBeNull();
    });

    it('closes on Escape (cancel) or Cancel without acting, and returns focus to the button that opened it', async () => {
      await open();
      document.body.appendChild(root);
      const copy = button('Copy link');
      copy.focus();
      copy.click();
      await settle();
      const dialog = q<HTMLDialogElement>('dialog.trust-dialog')!;
      dialog.dispatchEvent(new Event('cancel', { cancelable: true }));
      await settle();
      expect(dialog.open).toBe(false);
      expect(clipboard).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(copy);

      const download = button('Download');
      download.focus();
      download.click();
      await settle();
      q<HTMLButtonElement>('.dialog-cancel')!.click();
      await settle();
      expect(runs.downloadBundle).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(download);
      root.remove();
    });

    it('focuses the dialog\'s first control when it opens', async () => {
      await open();
      document.body.appendChild(root);
      button('Copy link').click();
      await settle();
      expect(q('dialog.trust-dialog')!.contains(document.activeElement)).toBe(true);
      root.remove();
    });
  });

  describe('pin and delete', () => {
    it('pins and unpins with a pressed state', async () => {
      await open();
      const pin = button('Pin');
      expect(pin.getAttribute('aria-pressed')).toBe('false');
      runs.pin.and.returnValue(of({}));
      pin.click();
      await settle();
      expect(runs.pin).toHaveBeenCalledWith(ID);
      expect(button('Unpin').getAttribute('aria-pressed')).toBe('true');
      runs.unpin.and.returnValue(of({}));
      button('Unpin').click();
      await settle();
      expect(runs.unpin).toHaveBeenCalledWith(ID);
      expect(button('Pin')).toBeTruthy();
    });

    it('says so when pinning fails and leaves the state alone', async () => {
      await open();
      runs.pin.and.returnValue(httpError(500));
      button('Pin').click();
      await settle();
      expect(q('.action-error')!.textContent).toContain("Couldn't update the pin");
      expect(button('Pin').getAttribute('aria-pressed')).toBe('false');
    });

    it('confirms before unpinning a run already past its retention date', async () => {
      await open({ runResult: of(run({ pinned: true, expires_at: NOW - 86400 })) });
      runs.unpin.and.returnValue(of({}));
      button('Unpin').click();
      await settle();
      expect(q('dialog.trust-dialog')!.textContent).toContain('past its retention date');
      expect(runs.unpin).not.toHaveBeenCalled();
      q<HTMLButtonElement>('.dialog-confirm')!.click();
      await settle();
      expect(runs.unpin).toHaveBeenCalledWith(ID);
    });

    it('hides Delete from someone who is neither an admin nor the run\'s owner', async () => {
      await open({ isAdmin: false });
      expect(qa('button').some((b) => b.textContent!.trim() === 'Delete')).toBe(false);
    });

    it('shows Delete to an admin', async () => {
      await open({ isAdmin: true });
      expect(button('Delete')).toBeTruthy();
    });

    it('confirms before deleting and returns to the library afterwards', async () => {
      await open({ isAdmin: true });
      runs.remove.and.returnValue(of({}));
      spyOn(router, 'navigate').and.resolveTo(true);
      button('Delete').click();
      await settle();
      expect(q('dialog.trust-dialog')!.textContent).toContain('This cannot be undone');
      expect(runs.remove).not.toHaveBeenCalled();
      q<HTMLButtonElement>('.dialog-confirm')!.click();
      await settle();
      expect(runs.remove).toHaveBeenCalledWith(ID);
      expect(router.navigate).toHaveBeenCalledWith(['/runs'], { queryParams: { status: 'failed' } });
    });
  });

  describe('technical details', () => {
    it('keeps raw logs behind a collapsed disclosure and renders them only once opened', async () => {
      await open();
      const details = q<HTMLDetailsElement>('details.technical-details')!;
      expect(details.open).toBe(false);
      expect(details.querySelector('summary')!.textContent).toContain('Technical details');
      expect(details.querySelector('pre.raw-logs')).toBeNull();
      details.open = true;
      details.dispatchEvent(new Event('toggle'));
      await settle();
      expect(details.querySelector('pre.raw-logs')!.textContent).toContain('"step_id": "st1"');
    });
  });

  describe('review corrections', () => {
    const OTHER = '9a8b7c6d-1111-4222-8333-444455556666';
    const noVideo = (): SessionVideo => ({ session_id: ID, status: 'unavailable', has_video: false, video_url: null, video_segments: [] });
    const whenLoaded = (video: HTMLVideoElement) =>
      new Promise<void>((resolve) =>
        video.readyState > 0 ? resolve() : video.addEventListener('loadedmetadata', () => resolve(), { once: true })
      );

    async function openWith(over: { runFor?: (id: string) => RunSummary; video?: SessionVideo; steps?: StepItemData[] } = {}) {
      runs.get.and.callFake((id: string) => of((over.runFor ?? ((rid: string) => run({ session_id: rid, prompt: `Prompt ${rid.slice(0, 4)}` })))(id)));
      runs.steps.and.returnValue(of(over.steps ?? [step(1), step(2), step(3)]));
      runs.video.and.returnValue(of(over.video ?? noVideo()));
      admin.getIdentity.and.returnValue(of({ email: 'a@x.test', admin: true, auth_mode: 'cloudflare', reason: null }));
      fixture = TestBed.createComponent(RunViewerComponent);
      fixture.componentRef.setInput('runId', ID);
      root = fixture.nativeElement;
      await settle();
    }

    async function showOther() {
      fixture.componentRef.setInput('runId', OTHER);
      await settle();
    }

    it('P1: a late Pin success for run A cannot overwrite the viewer showing run B', async () => {
      await openWith();
      const pending = new Subject<unknown>();
      runs.pin.and.returnValue(pending);
      button('Pin').click();
      await showOther();
      expect(q('h1.run-prompt')!.textContent).toContain(`Prompt ${OTHER.slice(0, 4)}`);
      pending.next({});
      pending.complete();
      await settle();
      expect(fixture.componentInstance.run()!.session_id).toBe(OTHER);
      expect(q('h1.run-prompt')!.textContent).toContain(`Prompt ${OTHER.slice(0, 4)}`);
      expect(button('Pin').getAttribute('aria-pressed')).toBe('false');
    });

    it('P1: a late Unpin success and a late Pin failure also leave run B alone', async () => {
      await openWith({ runFor: (id) => run({ session_id: id, pinned: true, prompt: `Prompt ${id.slice(0, 4)}` }) });
      const unpin = new Subject<unknown>();
      runs.unpin.and.returnValue(unpin);
      button('Unpin').click();
      await showOther();
      unpin.next({});
      await settle();
      expect(button('Unpin').getAttribute('aria-pressed')).toBe('true');

      const failing = new Subject<unknown>();
      runs.unpin.and.returnValue(failing);
      button('Unpin').click();
      await settle();
      fixture.componentRef.setInput('runId', ID);
      await settle();
      failing.error(new HttpErrorResponse({ status: 500 }));
      await settle();
      expect(q('.action-error')).toBeNull();
    });

    it('P1: a late bundle download or delete from run A does nothing once B is showing', async () => {
      await openWith();
      const bundle = new Subject<unknown>();
      runs.downloadBundle.and.returnValue(bundle as never);
      const created = spyOn(URL, 'createObjectURL').and.returnValue('blob:x');
      button('Download').click();
      await settle();
      q<HTMLButtonElement>('.dialog-confirm')!.click();
      await settle();
      await showOther();
      bundle.next(new HttpResponse({ status: 200, body: new Blob(['zip']) }));
      await settle();
      expect(created).not.toHaveBeenCalled();
      expect(q('.action-feedback')).toBeNull();
    });

    it('P2: selecting a step seeks a single-file recording (no segments) to that step\'s time', async () => {
      await openWith({ video: ready({ video_segments: [] }), steps: [step(1, { timestamp: START + 2 }), step(2, { timestamp: START + 3 })] });
      const video = q<HTMLVideoElement>('video')!;
      await whenLoaded(video);
      qa<HTMLButtonElement>('ol.step-list button.step-button')[0].click();
      await settle();
      expect(video.currentTime).toBeCloseTo(2, 1);
      qa<HTMLButtonElement>('ol.step-list button.step-button')[1].click();
      await settle();
      expect(video.currentTime).toBeCloseTo(3, 1);
    });

    it('P2: a step before the recording began, or past its end, clamps instead of failing', async () => {
      await openWith({ video: ready(), steps: [step(1, { timestamp: START - 5 }), step(2, { timestamp: START + 500 })] });
      const video = q<HTMLVideoElement>('video')!;
      await whenLoaded(video);
      qa<HTMLButtonElement>('ol.step-list button.step-button')[0].click();
      await settle();
      expect(video.currentTime).toBe(0);
      qa<HTMLButtonElement>('ol.step-list button.step-button')[1].click();
      await settle();
      expect(video.currentTime).toBeLessThanOrEqual(video.duration);
    });

    it('P2: an id the server rejects as invalid (400) shows Run not found, not a retryable error', async () => {
      await open({ id: 'bad id', runResult: httpError(400, { error: 'invalid_session_id' }) });
      expect(q('.state-page h1')!.textContent).toContain('Run not found');
      expect(qa('button').some((b) => b.textContent!.trim() === 'Retry')).toBe(false);
    });

    it('P2: a video file that fails to load falls back to the screenshot with recovery copy', async () => {
      await openWith({ video: ready() });
      expect(q('video')).not.toBeNull();
      q<HTMLVideoElement>('video')!.dispatchEvent(new Event('error'));
      await settle();
      expect(q('video')).toBeNull();
      expect(q('.recording-copy')!.textContent).toContain('could not be played');
      expect(q('img.evidence-image')).not.toBeNull();
      runs.video.and.returnValue(of(ready()));
      button('Check again').click();
      await settle();
      expect(q('video')).not.toBeNull();
    });
  });

  describe('keyboard walkthrough', () => {
    const FOCUSABLE = 'a[href], button, input, select, summary, video, [tabindex]:not([tabindex="-1"])';
    const nameOf = (el: Element) =>
      el.getAttribute('aria-label') || (el.textContent ?? '').replace(/\s+/g, ' ').trim();
    const controls = () =>
      qa<HTMLElement>(FOCUSABLE).filter((el) => !(el as HTMLButtonElement).disabled && el.checkVisibility());

    it('tabs from back link through steps and actions to technical details, all natively focusable', async () => {
      await open({ isAdmin: true });
      const names = controls().map(nameOf);
      expect(names[0]).toBe('Back to runs');
      const stepsAt = names.findIndex((n) => n.startsWith('Step 1'));
      const copyAt = names.indexOf('Copy link');
      expect(stepsAt).toBeGreaterThan(0);
      expect(copyAt).toBeGreaterThan(stepsAt);
      expect(names.slice(copyAt, copyAt + 4)).toEqual(['Copy link', 'Download', 'Pin', 'Delete']);
      expect(names[names.length - 1]).toBe('Technical details');
      for (const el of controls()) {
        expect(el.tabIndex).toBeGreaterThanOrEqual(0);
      }
    });

    it('keeps targets at least 24 px, primary actions at least 44 px', async () => {
      await open({ isAdmin: true });
      for (const el of controls()) {
        if (el.tagName === 'VIDEO') continue;
        const box = el.getBoundingClientRect();
        expect(box.width).toBeGreaterThanOrEqual(24, nameOf(el));
        expect(box.height).toBeGreaterThanOrEqual(24, nameOf(el));
      }
      for (const label of ['Copy link', 'Download', 'Pin', 'Delete']) {
        expect(button(label).getBoundingClientRect().height).toBeGreaterThanOrEqual(44, label);
      }
    });
  });

  describe('whose run it is (CHE-1152)', () => {
    const ME = 'qa1@example.test';
    const THEM = 'qa2@example.test';
    const hasDelete = () => qa('button').some((b) => b.textContent!.trim() === 'Delete');
    const notice = () => q('.read-only-notice');

    async function openAs(identity: { email: string | null; admin: boolean; auth_mode: string }, owner: string | null) {
      runs.get.and.returnValue(of(run({ requested_by: owner })));
      runs.steps.and.returnValue(of([step(1)]));
      runs.video.and.returnValue(of(ready()));
      admin.getIdentity.and.returnValue(of({ ...identity, reason: null }));
      fixture = TestBed.createComponent(RunViewerComponent);
      fixture.componentRef.setInput('runId', ID);
      root = fixture.nativeElement;
      await settle();
    }

    it('lets a QA delete their own run, with no read-only notice', async () => {
      await openAs({ email: ME, admin: false, auth_mode: 'cloudflare' }, ME);
      expect(hasDelete()).toBeTrue();
      expect(notice()).toBeNull();
    });

    it('opens a colleague\'s run read-only: no Delete, and a notice naming the owner', async () => {
      await openAs({ email: ME, admin: false, auth_mode: 'cloudflare' }, THEM);
      expect(hasDelete()).toBeFalse();
      expect(notice()!.textContent).toContain(THEM);
      expect(notice()!.textContent).toContain('read-only');
    });

    it('keeps Copy link and Download on a read-only run, so the link still works for sharing', async () => {
      await openAs({ email: ME, admin: false, auth_mode: 'cloudflare' }, THEM);
      expect(button('Copy link')).toBeTruthy();
      expect(button('Download')).toBeTruthy();
    });

    it('treats a run with no owner as read-only for a QA', async () => {
      await openAs({ email: ME, admin: false, auth_mode: 'cloudflare' }, null);
      expect(hasDelete()).toBeFalse();
      expect(notice()!.textContent).toContain('no owner');
    });

    it('lets an admin delete anyone\'s run without a read-only notice', async () => {
      await openAs({ email: 'boss@example.test', admin: true, auth_mode: 'cloudflare' }, THEM);
      expect(hasDelete()).toBeTrue();
      expect(notice()).toBeNull();
    });

    it('does not filter in open mode', async () => {
      await openAs({ email: null, admin: true, auth_mode: 'open' }, THEM);
      expect(hasDelete()).toBeTrue();
      expect(notice()).toBeNull();
    });

    it('stays read-only when the identity cannot be read', async () => {
      runs.get.and.returnValue(of(run({ requested_by: ME })));
      runs.steps.and.returnValue(of([step(1)]));
      runs.video.and.returnValue(of(ready()));
      admin.getIdentity.and.returnValue(httpError(0));
      fixture = TestBed.createComponent(RunViewerComponent);
      fixture.componentRef.setInput('runId', ID);
      root = fixture.nativeElement;
      await settle();
      expect(hasDelete()).toBeFalse();
    });
  });
});
