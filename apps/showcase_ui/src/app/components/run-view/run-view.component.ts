import { LoggerService } from '../../services/logger.service';
import { appUrl, mediaUrl } from '../../utils/app-url.util';
import {
  ChangeDetectionStrategy,
  afterEveryRender,
  Component,
  DestroyRef,
  ElementRef,
  NgZone,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
  viewChild
} from '@angular/core';
import { HttpErrorResponse, HttpEvent, HttpEventType } from '@angular/common/http';
import { DatePipe, JsonPipe, NgTemplateOutlet } from '@angular/common';
import { Router, RouterLink } from '@angular/router';
import { Title } from '@angular/platform-browser';
import { Subscription, catchError, of, switchMap, timer } from 'rxjs';
import { RunSummary, SessionVideo, VideoSegment } from '../../core/models/run.model';
import { ActionParam, CheckerBlockData, SessionChecks, StepEvent, StepItemData } from '../../core/models/stream.model';
import { Session, SessionUsage } from '../../core/models/session.model';
import { OwnerScopeService } from '../../services/owner-scope.service';
import { RunsService } from '../../services/runs.service';
import {
  getActionObject,
  getActionErrorMessage,
  getActionIcon,
  getActionTitle,
  extractActionExtraParams,
  getActionCoords,
  getActionTargetText,
  getActionInputLabel,
  getActionInputText,
  getActionBounds,
  getActionClass,
  getActionResourceId,
  getReportStatusExplanation,
  getStepPostImageUrl,
  getStepPreImageUrl,
  isActionFailed,
  isReportStatusAction
} from '../../utils/action-formatter.util';
import { extractToolExtraParams, getToolCoords, getToolInputLabel, getToolInputText, getToolTargetText, getToolTitle } from '../../utils/tool-formatter.util';
import { renderMarkdownToHtml } from '../../utils/markdown-parser.util';
import { REPORT_TITLE_MAX, isLongReport, summarizeReport } from '../../utils/report-summary.util';
import { Playback, mapRecording, prepareFailedCopy, technicalDetail } from '../../utils/recording-state.util';
import { locateSessionTime } from '../../utils/recording-timeline.util';
import { runStatusView } from '../../utils/run-status.util';
import { runTitle } from '../../utils/run-title.util';
import { buildStartupWorkItems } from '../../utils/run-startup.util';
import { buildCheckerSnapshotLogs, consolidateLogsToBlocks, getSortedStepEvents } from '../../utils/stream-aggregator.util';
import { AgentService, type StartupProgressEvent } from '../../services/agent.service';
import {
  DELETE_NOTICE,
  MEDIA_NOTICE,
  RUN_STRINGS,
  SHARE_NOTICE,
  UNPIN_EXPIRED_NOTICE,
  interruptReason,
  interruptedSentence,
  readOnlyText,
  removedReason
} from '../../utils/run-library-strings';
import { RunIdCopyComponent } from '../run-id-copy/run-id-copy.component';
import { RunStatusBadgeComponent } from '../run-presentation/run-status-badge.component';
import { RunDeviceLabelComponent } from '../run-presentation/run-device-label.component';
import { RunStepRowComponent } from '../run-presentation/run-step-row.component';
import { RunEvidencePanelComponent } from '../run-presentation/run-evidence-panel.component';
import { RunAction, RunActionBarComponent, RunActionEvent } from '../run-presentation/run-action-bar.component';

type PageState = 'empty' | 'loading' | 'ready' | 'not_found' | 'removed' | 'access' | 'ambiguous' | 'error';
type DialogKind = 'share' | 'download' | 'unpin_expired' | 'delete';
type Retryable = 'download' | null;

const DIALOGS: Record<DialogKind, { title: string; notices: string[]; confirm: string }> = {
  share: { title: 'Copy link', notices: [MEDIA_NOTICE, SHARE_NOTICE], confirm: RUN_STRINGS.copyLink },
  download: { title: 'Download this run', notices: [MEDIA_NOTICE], confirm: RUN_STRINGS.download },
  unpin_expired: { title: 'Unpin this run?', notices: [UNPIN_EXPIRED_NOTICE], confirm: RUN_STRINGS.unpin },
  delete: { title: 'Delete this run?', notices: [DELETE_NOTICE], confirm: RUN_STRINGS.delete }
};

@Component({
  selector: 'app-run-view',
  standalone: true,
  imports: [RouterLink, DatePipe, JsonPipe, NgTemplateOutlet, RunIdCopyComponent, RunStatusBadgeComponent, RunDeviceLabelComponent,
    RunStepRowComponent, RunEvidencePanelComponent, RunActionBarComponent],
  templateUrl: './run-view.component.html',
  styleUrl: './run-view.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunViewComponent {
  public readonly appUrl = appUrl;
  private readonly logger = inject(LoggerService);
  private readonly runsApi = inject(RunsService);
  private readonly agentService = inject(AgentService);
  private readonly ownerScope = inject(OwnerScopeService);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private readonly zone = inject(NgZone);
  private readonly pageTitle = inject(Title);
  private readonly promptEl = viewChild<ElementRef<HTMLDetailsElement>>('promptEl');
  public readonly title = computed(() => runTitle(this.run()?.prompt));

  /** Full run id or its 8-character prefix, from the URL. */
  public readonly runId = input<string>('');
  public readonly readOnly = input(false);
  public readonly readOnlyRun = computed(() => this.readOnly() || !!this.run()?.read_only);
  public readonly mode = input<'live' | 'review'>('review');
  public readonly liveSession = input<Session | null>(null);
  public readonly liveSteps = input<StepItemData[]>([]);
  public readonly startupProgress = input<StartupProgressEvent[]>([]);
  public readonly startupItems = computed(() => buildStartupWorkItems(
    this.startupProgress(), this.clock(), this.steps().length > 0, this.active()));
  public readonly newRunPrompt = output<string>();

  public readonly strings = RUN_STRINGS;
  public readonly statusView = runStatusView;
  public readonly interruptReason = interruptReason;
  public readonly removedReason = removedReason;

  public readonly state = signal<PageState>('loading');
  private readonly catalogRun = signal<RunSummary | null>(null);
  public readonly run = computed<RunSummary | null>(() => {
    const catalog = this.catalogRun();
    const session = this.mode() === 'live' ? this.liveSession() : null;
    if (!session || session.session_id !== this.runId()) return catalog;
    if (catalog) {
      return runStatusView(session.status).active || runStatusView(catalog.status).active
        ? { ...catalog, status: session.status ?? catalog.status }
        : catalog;
    }
    const serial = session.device_serial ?? session.device_id ?? null;
    return {
      session_id: session.session_id, prompt: session.initial_goal, status: session.status ?? 'pending',
      interrupt_reason: null, start_time: session.start_time, end_time: session.end_time ?? null,
      host_id: null, device_ref: serial ? { host_id: null, serial } : null,
      requested_by: null, pinned: false, recordings: []
    };
  });
  public readonly removed = signal<{ reason?: string } | null>(null);
  public readonly candidates = signal<string[]>([]);
  private readonly storedSteps = signal<StepItemData[]>([]);
  public readonly steps = computed(() => {
    if (this.mode() !== 'live') return this.storedSteps();
    const merged = new Map(this.storedSteps().map((step) => [step.step_id, step]));
    for (const step of this.liveSteps()) {
      if (step.session_id === this.run()?.session_id) merged.set(step.step_id, { ...merged.get(step.step_id), ...step });
    }
    return [...merged.values()].sort((first, second) => first.step_number - second.step_number);
  });
  public readonly stepsLoaded = signal(false);
  public readonly stepsFailed = signal(false);
  public readonly video = signal<SessionVideo | null>(null);
  public readonly videoFailed = signal(false);
  /** The media element itself could not load or decode the file. */
  public readonly playerFailed = signal(false);
  public readonly selectedStepId = signal<string | null>(null);
  public readonly followLatest = signal(true);
  public readonly liveLogOpen = signal(false);
  public readonly activeSegmentIndex = signal(0);
  public readonly compact = signal(false);
  public readonly techOpen = signal(false);
  public readonly expandedSteps = signal<ReadonlySet<string>>(new Set());
  private readonly checks = signal<SessionChecks | null>(null);
  public readonly checksFailed = signal(false);
  private readonly notes = signal<Record<string, string> | null>(null);
  public readonly notesFailed = signal(false);
  public readonly reportPending = computed(() => this.notes() === null && !this.notesFailed());

  public readonly dialogKind = signal<DialogKind | null>(null);
  public readonly feedback = signal('');
  public readonly actionError = signal<{ text: string; retry: Retryable } | null>(null);
  public readonly viewingLiveSession = computed(() => this.mode() === 'live'
    && !!this.run()?.session_id && this.run()?.session_id === this.agentService.currentSessionId());
  private readonly activeLiveSession = computed(() => this.viewingLiveSession()
    && !!this.agentService.runningSessionId() && this.run()?.session_id === this.agentService.runningSessionId());
  public readonly canResume = computed(() => {
    if (this.readOnlyRun() || this.mode() !== 'live' || !this.agentService.isPaused() || this.agentService.agentStatus() !== 'paused') return false;
    const pausedSessionId = this.agentService.runningSessionId();
    return !!pausedSessionId && this.run()?.session_id === pausedSessionId
      && this.agentService.currentSessionId() === pausedSessionId;
  });
  public readonly liveState = computed(() => {
    if (!this.activeLiveSession()) return null;
    if (this.canResume()) return { title: 'Task paused', reason: this.agentService.pausedError() || 'AI call failed.' };
    if (this.agentService.isRetrying() && this.agentService.agentStatus() === 'running') {
      return { title: 'Retrying', reason: this.agentService.retryMessage() || 'AI service is temporarily busy. Retrying…' };
    }
    return null;
  });
  public readonly infoOpen = signal(false);
  public readonly usage = signal<SessionUsage | null>(null);
  public readonly usageFailed = signal(false);
  public readonly viewedModel = computed(() => {
    const live = this.liveSession();
    const session = live?.session_id === this.run()?.session_id ? live
      : this.agentService.sessions().find(session => session.session_id === this.run()?.session_id);
    return session?.session_id === this.run()?.session_id && session?.model_info
      ? session.model_info : this.viewingLiveSession() ? this.agentService.viewedModel() : null;
  });
  public readonly active = computed(() => runStatusView(this.run()?.status).active);
  private readonly clock = signal(Date.now() / 1000);
  public readonly duration = computed(() => {
    const run = this.run();
    const end = this.active() ? this.clock() : run?.end_time;
    if (run?.start_time == null || end == null) return 'Not recorded';
    const seconds = Math.max(0, Math.floor(end - run.start_time));
    return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  });
  public readonly appName = computed(() => {
    for (const step of this.steps()) {
      const action = getActionObject(step.action_taken);
      const app = action?.package_name ?? action?.args?.package_name ?? action?.app_name ?? action?.args?.app_name;
      if (typeof app === 'string' && app.trim()) return app;
    }
    return 'Not recorded';
  });
  public readonly statusMessage = computed(() => {
    const live = this.liveState();
    if (live) return `${live.title}. ${live.reason}`;
    if (this.run()?.status === 'pending') return 'Waiting for the phone.';
    if (this.run()?.status === 'paused') return 'Task paused. Waiting to continue.';
    if (!this.steps().length) return 'Preparing the phone. Steps will appear here.';
    return `Step ${this.steps().at(-1)!.step_number} in progress. The verdict appears here when the run finishes.`;
  });
  public readonly announcement = computed(() => {
    const run = this.run();
    if (!run) return '';
    if (!this.active()) {
      const outcome = this.resultOutcome();
      let verdict: string | null = null;
      if (outcome?.tests?.failed) verdict = 'Fail';
      else if (outcome?.tests?.inconclusive || outcome?.tests?.unchecked
        || outcome?.task_status === 'partial' || outcome?.task_status === 'blocked') verdict = 'Inconclusive';
      else if (outcome?.task_status === 'completed' && outcome.tests?.passed) verdict = 'Pass';
      const execution = this.statusView(run.status);
      return `Run finished: ${verdict ?? (execution.key === 'completed' ? 'Completed' : execution.label)}.`;
    }
    const step = this.currentStep();
    return step ? `Step ${step.step_number} in progress.` : '';
  });
  public readonly currentStep = computed(() => this.active() && this.run()?.status !== 'pending' ? this.steps().at(-1) ?? null : null);
  public readonly currentStepElapsed = computed(() => {
    const step = this.currentStep();
    return step ? `${Math.max(0, Math.floor(this.clock() - step.timestamp))}s` : null;
  });
  public readonly headerActions = computed<RunAction[]>(() => this.active()
    ? this.canChange() ? [{ id: 'stop', label: 'Stop run', icon: 'stop', className: 'action-button danger', ariaLabel: 'Stop run', title: `Stop ${this.title()}` }] : []
    : this.readOnlyRun() ? [] : [{ id: 'again', label: 'Run again', icon: 'replay', ariaLabel: 'Run again' }]);
  private readonly resultBlocks = computed(() => {
    const id = this.run()?.session_id;
    if (!id) return [];
    const checks = this.checks();
    const logs = checks ? buildCheckerSnapshotLogs(id, checks.records ?? [], checks.run_outcome, checks.streams) : [];
    if (this.viewingLiveSession()) logs.push(...this.agentService.sessionLogs().filter(log =>
      (!log.session_id || log.session_id === id) && (!log.data?.session_id || log.data.session_id === id)));
    return consolidateLogsToBlocks(logs).filter(block => block.type === 'checker');
  });
  public readonly resultOutcome = computed<CheckerBlockData | null>(() =>
    this.resultBlocks().find(block => block.data.phase === 'outcome')?.data ?? null);
  public readonly reportHtml = computed(() => {
    const report = this.notes()?.['output.md'];
    if (report) return renderMarkdownToHtml(report);
    const explanation = this.steps().flatMap(step => getSortedStepEvents(step))
      .filter(event => isReportStatusAction(event.data)).map(event => getReportStatusExplanation(event.data)).filter(Boolean).at(-1);
    return explanation ? renderMarkdownToHtml(explanation) : null;
  });
  public readonly unanchoredChecks = computed(() => this.resultBlocks().filter(block =>
    block.data.phase !== 'outcome' && !this.steps().some(step => step.step_id === block.data.anchor_step_id)));
  public readonly streamBlocks = computed(() => this.viewingLiveSession()
    ? consolidateLogsToBlocks(this.agentService.sessionLogs()).map((block) => ({
      id: block.id,
      stepId: block.data.step_id as string | undefined,
      resets: (block.data.stream_resets ?? []) as Array<{ id: string; message: string }>,
      events: getSortedStepEvents(block.data).filter((event) => event.type === 'thinking' || event.type === 'text')
    })).filter((block) => block.events.length || block.resets.length)
    : []);
  public readonly liveThought = computed(() => {
    const step = this.currentStep();
    if (!step) return '';
    const block = this.streamBlocks().filter(block => block.stepId === step.step_id).at(-1);
    if (block) return block.events.filter(event => event.type === 'thinking').at(-1)?.data.text ?? '';
    return step.operator_native_thinking || step.operator_raw_thinking || '';
  });
  public readonly actions = computed<RunAction[]>(() => [
    { id: 'share', label: this.strings.copyLink },
    { id: 'download', label: this.strings.download },
    ...(this.canChange() ? [
      { id: 'pin', label: this.run()?.pinned ? this.strings.unpin : this.strings.pin, pressed: this.run()?.pinned ?? false },
      { id: 'delete', label: this.strings.delete, className: 'action-button danger' }
    ] : [])
  ]);

  /** Pin and Delete change the run, so only its owner or an admin gets them; the server checks too. */
  public readonly canChange = computed(() => !this.readOnlyRun() && this.ownerScope.canAct(this.run()?.requested_by));
  /** Set once we know who is looking and they may not change this run. */
  public readonly readOnlyNote = computed(() =>
    this.ownerScope.identity() && this.run() && !this.canChange() ? readOnlyText(this.run()!.requested_by) : null
  );

  public readonly lastQuery = this.runsApi.lastLibraryQuery;
  public readonly dialog = computed(() => (this.dialogKind() ? DIALOGS[this.dialogKind()!] : null));

  public readonly segments = computed(() => {
    let start = 0;
    return (this.video()?.video_segments ?? []).map((segment: VideoSegment) => {
      const withStart = { ...segment, start: segment.start ?? start };
      start = withStart.start + segment.duration;
      return withStart as VideoSegment & { start: number };
    });
  });

  public readonly selectedStep = computed(() => {
    const steps = this.steps();
    if (this.followLatest()) return steps.at(-1) ?? null;
    return steps.find((s) => s.step_id === this.selectedStepId()) ?? steps[steps.length - 1] ?? null;
  });

  public readonly recording = computed(() => {
    const first = this.run()?.recordings[0];
    const segments = this.segments();
    const stoppedAt = segments.length ? segments.reduce((sum, s) => sum + s.duration, 0) : null;
    return mapRecording(
      {
        capture: (first?.capture ?? null) as never,
        transfer: (first?.transfer ?? null) as never,
        playback: (this.video()?.status ?? null) as Playback
      },
      { stoppedAtSeconds: stoppedAt }
    );
  });

  /** Player-area copy; a failed check or a file the browser cannot play wins over the mapped copy. */
  public readonly copy = computed(() => {
    if (this.videoFailed()) return "Couldn't check the video.";
    if (this.playerFailed()) return 'The video could not be played. Steps and screenshots are still here.';
    return this.recording().state === 'prepare_failed'
      ? prepareFailedCopy(this.recording().copy, this.video()?.message)
      : this.recording().copy;
  });

  /** Raw recorder output behind a failed video, shown only inside "Technical details". */
  public readonly videoDetail = computed(() =>
    this.recording().state === 'prepare_failed' ? technicalDetail(this.video()?.detail) : null
  );

  public readonly videoUrl = computed(
    () => mediaUrl(this.segments()[this.activeSegmentIndex()]?.url ?? this.video()?.video_url)
  );

  public readonly screenshot = computed(() => {
    const step = this.selectedStep();
    return step ? (getStepPostImageUrl(step) ?? getStepPreImageUrl(step)) : null;
  });

  public readonly interruptedAtStep = computed(() => this.steps()[this.steps().length - 1]?.step_number ?? null);
  public readonly interruptedText = computed(() => interruptedSentence(this.interruptedAtStep()));
  public readonly failureReason = computed(() => {
    const failed = [...this.steps()].reverse().find((step) => this.stepFailed(step));
    return failed ? this.stepFailureDetail(failed) : this.run()?.interrupt_reason || 'No failure reason was recorded.';
  });
  /** Inline text for the failure banner: the reason itself, or a short summary of a long report. */
  public readonly failureSummary = computed(() => {
    const reason = this.failureReason();
    return isLongReport(reason) ? summarizeReport(reason) : reason;
  });
  /** Full long report as escaped markdown HTML, or null when the reason is short. */
  public readonly failureReportHtml = computed(() => {
    const reason = this.failureReason();
    return isLongReport(reason) ? renderMarkdownToHtml(reason) : null;
  });
  public readonly canCheckAgain = computed(
    () =>
      this.videoFailed() ||
      this.playerFailed() ||
      ['preparing', 'uploaded_unchecked', 'prepare_failed'].includes(this.recording().state)
  );
  public readonly rawLogs = computed(() => JSON.stringify({ run: this.run(), steps: this.steps() }, null, 2));

  private readonly dialogEl = viewChild<ElementRef<HTMLDialogElement>>('dialogEl');
  private readonly evidencePanel = viewChild(RunEvidencePanelComponent);
  private readonly scrollEl = viewChild<ElementRef<HTMLElement>>('scrollEl');
  private readonly timelineEl = viewChild<ElementRef<HTMLElement>>('timelineEl');
  private readonly liveLogEl = viewChild<ElementRef<HTMLDetailsElement>>('liveLogEl');
  private followedStepId: string | null = null;
  private focusLiveLog = false;
  private pendingPosition: { scrollTop: number; timelineScrollTop: number } | null = null;
  private loadedSessionId: string | null = null;
  private renderedStepId: string | null = null;
  private liveStateKey = '';
  private opener: HTMLElement | null = null;
  private loadRequest: Subscription | null = null;
  private evidenceRequests = new Subscription();
  /** Pin, download and delete: cancelled on navigation so run A's answer never lands on run B. */
  private actionRequests = new Subscription();
  private pendingSeek: number | null = null;
  private continuePlaying = false;

  constructor() {
    this.ownerScope.load();
    const previousTitle = this.pageTitle.getTitle();
    effect(() => this.pageTitle.setTitle(this.state() === 'ready'
      ? `${this.title()} · SmartQA` : previousTitle));
    this.destroyRef.onDestroy(() => this.pageTitle.setTitle(previousTitle));
    effect((onCleanup) => {
      const run = this.run();
      if (!run || !this.active()) return;
      this.clock.set(Date.now() / 1000);
      const clock = this.zone.runOutsideAngular(() => setInterval(() => this.clock.set(Date.now() / 1000), 1000));
      onCleanup(() => clearInterval(clock));
    });

    const query = typeof window !== 'undefined' ? window.matchMedia('(max-width: 1279px)') : null;
    if (query) {
      this.compact.set(query.matches);
      const onChange = (event: MediaQueryListEvent) => this.compact.set(event.matches);
      query.addEventListener('change', onChange);
      this.destroyRef.onDestroy(() => query.removeEventListener('change', onChange));
    }

    effect(() => {
      const id = this.runId();
      untracked(() => this.load(id));
    });
    effect((onCleanup) => {
      const id = this.viewingLiveSession() && this.infoOpen() ? this.run()?.session_id : null;
      const active = runStatusView(this.run()?.status).active;
      this.usage.set(null);
      this.usageFailed.set(false);
      if (!id) return;
      const requests = (active ? timer(0, 3000) : of(0)).pipe(
        switchMap(() => this.agentService.getSessionUsage(id).pipe(catchError(() => of(null))))
      ).subscribe((usage) => {
        this.usage.set(usage?.session_id === id ? usage : null);
        this.usageFailed.set(!usage || usage.session_id !== id);
      });
      onCleanup(() => requests.unsubscribe());
    });
    effect(() => {
      const session = this.mode() === 'live' ? this.liveSession() : null;
      const id = this.runId();
      const key = session ? `${session.session_id}:${session.status}:${session.recording_status}` : '';
      untracked(() => {
        if (session && session.session_id === id && key !== this.liveStateKey) {
          const previous = this.liveStateKey;
          this.liveStateKey = key;
          if (previous.startsWith(`${id}:`)) this.refresh(id);
          else if (this.state() === 'not_found') {
            this.state.set('ready');
            this.loadEvidence(id);
          }
        }
      });
    });
    afterEveryRender(() => {
      if (this.state() === 'ready') this.renderedStepId = this.selectedStep()?.step_id ?? null;
      if (this.state() === 'ready' && this.stepsLoaded() && this.pendingPosition && this.scrollEl()) {
        this.scrollEl()!.nativeElement.scrollTop = this.pendingPosition.scrollTop;
        if (this.timelineEl()) this.timelineEl()!.nativeElement.scrollTop = this.pendingPosition.timelineScrollTop;
        this.pendingPosition = null;
      }
      const latest = this.currentStep();
      if (this.followLatest() && latest && latest.step_id !== this.followedStepId) {
        const row = this.timelineEl()?.nativeElement.querySelector<HTMLElement>('.current-step .step-button');
        if (row) {
          row.scrollIntoView({ block: 'nearest', behavior: 'instant' });
          this.followedStepId = latest.step_id;
        }
      }
      if (this.focusLiveLog && this.liveLogEl()) {
        this.liveLogEl()!.nativeElement.querySelector<HTMLElement>('summary')?.focus();
        this.focusLiveLog = false;
      }
    });
    this.destroyRef.onDestroy(() => {
      this.loadRequest?.unsubscribe();
      this.evidenceRequests.unsubscribe();
      this.actionRequests.unsubscribe();
    });
  }

  // -- loading ----------------------------------------------------------------

  public reload(): void {
    this.load(this.runId());
  }

  private load(id: string): void {
    if (this.promptEl()) this.promptEl()!.nativeElement.open = false;
    this.rememberPosition();
    this.loadedSessionId = id || null;
    this.renderedStepId = null;
    this.closeDialog();
    this.loadRequest?.unsubscribe();
    this.evidenceRequests.unsubscribe(); // a slow answer for the previous run must not land on this one
    this.evidenceRequests = new Subscription();
    this.actionRequests.unsubscribe();
    this.actionRequests = new Subscription();
    this.playerFailed.set(false);
    this.state.set('loading');
    this.catalogRun.set(null);
    this.storedSteps.set([]);
    this.expandedSteps.set(new Set());
    this.checks.set(null);
    this.checksFailed.set(false);
    this.notes.set(null);
    this.notesFailed.set(false);
    this.stepsLoaded.set(false);
    this.video.set(null);
    this.selectedStepId.set(null);
    this.followLatest.set(true);
    this.followedStepId = null;
    this.liveLogOpen.set(false);
    this.focusLiveLog = false;
    this.activeSegmentIndex.set(0);
    this.feedback.set('');
    this.actionError.set(null);
    const position = this.runsApi.viewPosition();
    this.selectedStepId.set(position?.sessionId === id ? position.selectedStepId : null);
    if (position?.sessionId === id && position.selectedStepId) this.followLatest.set(false);
    this.pendingPosition = position?.sessionId === id ? position : { scrollTop: 0, timelineScrollTop: 0 };
    if (!id) {
      this.state.set('empty');
      return;
    }
    this.loadRequest = this.runsApi.get(id).subscribe({
      next: (run) => {
        this.loadedSessionId = run.session_id;
        this.catalogRun.set(run);
        if (position?.sessionId === run.session_id) {
          this.selectedStepId.set(position.selectedStepId);
          this.followLatest.set(!position.selectedStepId);
          this.pendingPosition = position;
        }
        this.state.set('ready');
        this.loadEvidence(run.session_id);
      },
      error: (error: HttpErrorResponse) => {
        if (error.status === 404 && this.mode() === 'live' && this.run()) {
          this.state.set('ready');
          this.loadEvidence(id);
        } else this.fail(error);
      }
    });
  }

  private refresh(id: string): void {
    this.loadRequest?.unsubscribe();
    this.loadRequest = this.runsApi.get(id).subscribe({
      next: (run) => {
        this.catalogRun.set(run);
        this.state.set('ready');
        this.evidenceRequests.unsubscribe();
        this.evidenceRequests = new Subscription();
        this.loadEvidence(run.session_id);
      },
      error: () => undefined
    });
  }

  public rememberPosition(): void {
    if (this.state() !== 'ready' || !this.loadedSessionId || !this.scrollEl()) return;
    this.runsApi.viewPosition.set({
      sessionId: this.loadedSessionId, selectedStepId: this.selectedStepId() ?? this.renderedStepId,
      scrollTop: this.scrollEl()!.nativeElement.scrollTop,
      timelineScrollTop: this.timelineEl()?.nativeElement.scrollTop ?? 0
    });
  }

  private fail(error: HttpErrorResponse): void {
    const body = (error.error ?? {}) as { reason?: string; candidates?: string[] };
    switch (error.status) {
      case 400: // invalid_session_id: this id can never resolve, so Retry would be a lie
      case 404:
        return this.state.set('not_found');
      case 410:
        this.removed.set({ reason: body.reason });
        return this.state.set('removed');
      case 409:
        this.candidates.set(body.candidates ?? []);
        return this.state.set('ambiguous');
      case 401:
      case 403:
      case 0: // Cloudflare Access answers an expired session with a cross-origin redirect
        return this.state.set('access');
      default:
        return this.state.set('error');
    }
  }

  private loadEvidence(sessionId: string): void {
    this.stepsFailed.set(false);
    this.evidenceRequests.add(
      this.runsApi.steps(sessionId).subscribe({
        next: (steps) => {
          this.storedSteps.set(steps);
          this.stepsLoaded.set(true);
        },
        error: () => {
          this.stepsFailed.set(true);
          this.stepsLoaded.set(true);
        }
      })
    );
    this.evidenceRequests.add(this.runsApi.checks(sessionId).subscribe({
      next: checks => { this.checks.set(checks); this.checksFailed.set(false); },
      error: () => this.checksFailed.set(true)
    }));
    this.evidenceRequests.add(this.runsApi.notes(sessionId).subscribe({
      next: result => { this.notes.set(result.notes ?? {}); this.notesFailed.set(false); },
      error: () => this.notesFailed.set(true)
    }));
    this.loadVideo(sessionId);
  }

  private loadVideo(sessionId: string): void {
    this.videoFailed.set(false);
    this.evidenceRequests.add(
      this.runsApi.video(sessionId).subscribe({
        next: (video) => this.video.set(video),
        error: () => this.videoFailed.set(true)
      })
    );
  }

  public checkAgain(): void {
    const id = this.run()?.session_id;
    if (!id) return;
    this.playerFailed.set(false);
    this.loadVideo(id);
  }

  // -- steps and playback -----------------------------------------------------

  public stepTitle(step: StepItemData): string {
    return getActionTitle(step.action_taken);
  }

  public readonly stepIcon = getActionIcon;

  public stepKind(step: StepItemData): string {
    const action = getActionObject(step.action_taken);
    const kind = action?.name || action?.action;
    return typeof kind === 'string' && kind ? kind : 'Action';
  }

  public stepTime(step: StepItemData): string {
    const start = this.run()?.start_time;
    if (start == null || !Number.isFinite(start) || !Number.isFinite(step.timestamp)) return '—';
    const seconds = Math.max(0, Math.floor(step.timestamp - start));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  }

  public goToStep(stepNumber: number): boolean {
    const index = this.steps().findIndex(step => step.step_number === stepNumber);
    if (index === -1) return false;
    this.selectStep(this.steps()[index]);
    const button = this.timelineEl()?.nativeElement.querySelectorAll<HTMLButtonElement>('.step-button')[index];
    button?.scrollIntoView({ block: 'nearest' });
    button?.focus({ preventScroll: true });
    return true;
  }

  public toggleStep(step: StepItemData): void {
    const expanded = new Set(this.expandedSteps());
    if (expanded.has(step.step_id)) expanded.delete(step.step_id);
    else expanded.add(step.step_id);
    this.expandedSteps.set(expanded);
  }

  public readonly stepEvents = getSortedStepEvents;
  public readonly markdown = renderMarkdownToHtml;

  public eventTitle(event: StepEvent): string {
    return event.type === 'action' ? getActionTitle(event.data) : getToolTitle(event.data);
  }

  public eventParameters(event: StepEvent): ActionParam[] {
    const action = event.type === 'action';
    const data = event.data;
    return [
      { key: 'Target', value: action ? getActionTargetText(data) : getToolTargetText(data) },
      { key: action ? getActionInputLabel(data) : getToolInputLabel(data), value: action ? getActionInputText(data) : getToolInputText(data) },
      { key: 'Coordinates', value: action ? getActionCoords(data) : getToolCoords(data) },
      ...(action ? [
        { key: 'Target bounds', value: getActionBounds(data) },
        { key: 'Target class', value: getActionClass(data) },
        { key: 'Resource ID', value: getActionResourceId(data) }
      ] : []),
      ...(action ? extractActionExtraParams(data) : extractToolExtraParams(data))
    ].filter(param => !!param.value);
  }

  public stepChecks(step: StepItemData) {
    return this.resultBlocks().filter(block => block.data.phase !== 'outcome' && block.data.anchor_step_id === step.step_id);
  }

  public stepFailed(step: StepItemData): boolean {
    return isActionFailed(getActionObject(step.action_taken), step);
  }

  public stepFailureDetail(step: StepItemData): string {
    return getActionErrorMessage(step.action_taken, step);
  }

  /** One-line failure text for a step row: long reports are summarised. */
  public stepFailureSummary(step: StepItemData): string {
    const detail = this.stepFailureDetail(step);
    return isLongReport(detail) ? summarizeReport(detail) : detail;
  }

  /** Full text for the row tooltip, only when a summary was shown and the text is short enough to hover. */
  public stepFailureTitle(step: StepItemData): string | null {
    const detail = this.stepFailureDetail(step);
    return isLongReport(detail) && detail.length < REPORT_TITLE_MAX ? detail : null;
  }

  public readonly preImage = getStepPreImageUrl;
  public readonly postImage = getStepPostImageUrl;

  public onStepKey(event: KeyboardEvent, index: number): void {
    const buttons = this.timelineEl()?.nativeElement.querySelectorAll<HTMLButtonElement>('.step-button');
    if (!buttons?.length) return;
    let target: number;
    if (event.key === 'ArrowDown') target = Math.min(index + 1, buttons.length - 1);
    else if (event.key === 'ArrowUp') target = Math.max(index - 1, 0);
    else if (event.key === 'Home') target = 0;
    else if (event.key === 'End') target = buttons.length - 1;
    else return;
    event.preventDefault();
    buttons[target].focus();
    this.selectStep(this.steps()[target]);
  }

  public selectStep(step: StepItemData): void {
    this.followLatest.set(false);
    this.selectedStepId.set(step.step_id);
    this.rememberPosition();
    const run = this.run();
    if (!this.recording().playable) return;
    const sessionSeconds = step.timestamp - (run?.start_time ?? step.timestamp);
    if (!this.segments().length) {
      // One file, no manifest: the session axis is the file's own axis.
      this.pendingSeek = Math.max(0, sessionSeconds);
      return this.applySeek();
    }
    const located = locateSessionTime(this.segments(), sessionSeconds);
    if (!located) return;
    this.pendingSeek = located.localTime;
    if (located.index === this.activeSegmentIndex()) this.applySeek();
    else this.activeSegmentIndex.set(located.index);
  }

  public toggleFollowLatest(): void {
    const selected = this.selectedStep();
    this.followLatest.update(value => !value);
    this.followedStepId = null;
    if (!this.followLatest()) this.selectedStepId.set(selected?.step_id ?? null);
  }

  public showLiveLog(): void {
    this.liveLogOpen.set(true);
    this.focusLiveLog = true;
  }

  public onMetadata(): void {
    this.applySeek();
    if (this.continuePlaying) {
      this.continuePlaying = false;
      void this.evidencePanel()?.player()?.nativeElement.play().catch(() => undefined);
    }
  }

  private applySeek(): void {
    const player = this.evidencePanel()?.player()?.nativeElement;
    if (player && this.pendingSeek !== null && player.readyState > 0) {
      const end = Number.isFinite(player.duration) ? player.duration : this.pendingSeek;
      player.currentTime = Math.min(this.pendingSeek, end);
      this.pendingSeek = null;
    }
  }

  public onEnded(): void {
    if (this.activeSegmentIndex() < this.segments().length - 1) {
      this.continuePlaying = true;
      this.activeSegmentIndex.update((index) => index + 1);
    }
  }

  // -- actions ----------------------------------------------------------------

  public onAction(action: RunActionEvent): void {
    if (action.id === 'stop') {
      if (this.canChange() && this.active()) this.agentService.stopTask(this.run()!.session_id);
    } else if (action.id === 'again') {
      if (!this.readOnlyRun() && !this.active()) this.startNewRun();
    } else if (action.id === 'resume') {
      if (this.canResume()) this.agentService.resumeTask();
    } else if (action.id === 'pin') this.togglePin(action.event);
    else if (action.id === 'retry') this.retry();
    else this.ask(action.id as DialogKind, action.event);
  }

  public ask(kind: DialogKind, event?: Event): void {
    if (!this.canChange() && (kind === 'delete' || kind === 'unpin_expired')) return;
    const control = event?.currentTarget as HTMLElement | null;
    this.opener = control?.closest('.more-actions')?.querySelector('summary') ?? control ?? null;
    this.dialogKind.set(kind);
    this.dialogEl()?.nativeElement.showModal();
  }

  public closeDialog(): void {
    const dialog = this.dialogEl()?.nativeElement;
    if (dialog?.open) dialog.close();
    this.dialogKind.set(null);
    this.opener?.focus();
  }

  public onDialogCancel(event: Event): void {
    event.preventDefault();
    this.closeDialog();
  }

  public confirm(): void {
    const kind = this.dialogKind();
    this.closeDialog();
    if (kind === 'share') void this.copyLink();
    else if (kind === 'download') this.download();
    else if (kind === 'unpin_expired') this.setPinned(false);
    else if (kind === 'delete') this.remove();
  }

  public togglePin(event: Event): void {
    if (!this.canChange()) return;
    const run = this.run();
    if (!run) return;
    if (!run.pinned) return this.setPinned(true);
    if (run.expires_at != null && run.expires_at <= Date.now() / 1000) return this.ask('unpin_expired', event);
    this.setPinned(false);
  }

  private setPinned(pinned: boolean): void {
    if (!this.canChange()) return;
    const run = this.run();
    if (!run) return;
    this.actionError.set(null);
    this.actionRequests.add(
      (pinned ? this.runsApi.pin(run.session_id) : this.runsApi.unpin(run.session_id)).subscribe({
        next: () => this.catalogRun.set({ ...run, pinned }),
        error: () => this.actionError.set({ text: RUN_STRINGS.pinFailed, retry: null })
      })
    );
  }

  private async copyLink(): Promise<void> {
    const run = this.run();
    if (!run) return;
    this.actionError.set(null);
    try {
      await navigator.clipboard.writeText(new URL(appUrl(`/runs/${encodeURIComponent(run.session_id)}`), document.baseURI).href);
      if (this.run()?.session_id === run.session_id) this.feedback.set(RUN_STRINGS.linkCopied);
    } catch (error) {
      this.logger.warn('UI operation failed:', error);
      if (this.run()?.session_id === run.session_id) this.actionError.set({ text: "Couldn't copy the link. Copy it from the address bar.", retry: null });
    }
  }

  public download(): void {
    const run = this.run();
    if (!run) return;
    this.actionError.set(null);
    this.feedback.set(RUN_STRINGS.preparingBundle);
    this.actionRequests.add(
      this.runsApi.downloadBundle(run.session_id).subscribe({
        next: (event) => this.onBundleEvent(event, run.session_id),
        error: () => {
          this.feedback.set('');
          this.actionError.set({ text: RUN_STRINGS.downloadFailed, retry: 'download' });
        }
      })
    );
  }

  public retry(): void {
    if (this.actionError()?.retry === 'download') this.download();
  }

  private onBundleEvent(event: HttpEvent<Blob>, sessionId: string): void {
    if (event.type === HttpEventType.ResponseHeader) {
      const bytes = Number(event.headers.get('Content-Length'));
      if (bytes > 0) this.feedback.set(`Downloading bundle (${Math.max(1, Math.round(bytes / 1048576))} MB)…`);
    } else if (event.type === HttpEventType.Response && event.body) {
      // ponytail: the bundle (1 GiB cap) is held as a blob so a failure can offer Retry; stream via an anchor if that hurts.
      const url = URL.createObjectURL(event.body);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `run-${sessionId.slice(0, 8)}.zip`;
      anchor.click();
      URL.revokeObjectURL(url);
      this.feedback.set(RUN_STRINGS.downloadStarted);
    }
  }

  private remove(): void {
    if (!this.canChange()) return;
    const run = this.run();
    if (!run) return;
    this.actionRequests.add(
      this.runsApi.remove(run.session_id).subscribe({
        next: () => void this.router.navigate(['/runs'], { queryParams: this.lastQuery() }),
        error: () => this.actionError.set({ text: RUN_STRINGS.deleteFailed, retry: null })
      })
    );
  }

  public startNewRun(): void {
    if (this.mode() === 'live') {
      this.newRunPrompt.emit(this.run()?.prompt ?? '');
      return;
    }
    void this.router.navigate(['/workspace'], { state: { draftPrompt: this.run()?.prompt ?? '' } });
  }

  public trackStep(_: number, step: StepItemData): string {
    return step.step_id;
  }
}
