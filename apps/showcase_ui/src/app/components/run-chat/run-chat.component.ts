import { afterRenderEffect, ChangeDetectionStrategy, Component, computed, DestroyRef, effect, ElementRef, inject, input, NgZone, output, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CdkTextareaAutosize } from '@angular/cdk/text-field';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, Router } from '@angular/router';
import { deployDrainMessage } from '../../utils/deploy-drain.util';
import { DatePipe, JsonPipe } from '@angular/common';
import { catchError, distinctUntilChanged, of, switchMap, takeWhile, tap, timer } from 'rxjs';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChatMessage, ChatSession, ChatTurnResult, RunSummary } from '../../core/models/run.model';
import { Session } from '../../core/models/session.model';
import { StepItemData } from '../../core/models/stream.model';
import { StartupProgressEvent } from '../../services/agent.service';
import { RunsService } from '../../services/runs.service';
import { ShellLayoutService } from '../../services/shell-layout.service';
import { SystemService } from '../../services/system.service';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';
import { mediaUrl } from '../../utils/app-url.util';
import { deviceTitle, isIdentifiedDevice } from '../../utils/device-label.util';
import { recordedDevice } from '../../utils/session-device.util';
import { runTitle } from '../../utils/run-title.util';
import { actionMarker } from '../../utils/step-evidence.util';
import { SessionSidebarComponent } from '../session-sidebar/session-sidebar.component';
import smartqaHelpRoute from '../../core/smartqa-help-route.json';

@Component({
  selector: 'app-run-chat',
  standalone: true,
  imports: [FormsModule, CdkTextareaAutosize, DatePipe, JsonPipe, SessionSidebarComponent],
  templateUrl: './run-chat.component.html',
  styleUrl: './run-chat.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunChatComponent {
  private readonly api = inject(RunsService);
  private readonly zone = inject(NgZone);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private readonly system = inject(SystemService);
  private readonly phones = inject(WorkspacePhoneService);
  public readonly shell = inject(ShellLayoutService);
  private readonly failedImages = signal(new Set<string>());
  private readonly revision = signal(0);
  private readonly historyElement = viewChild<ElementRef<HTMLElement>>('history');
  private readonly evidenceDialog = viewChild<ElementRef<HTMLDialogElement>>('evidenceDialog');
  private followLatest = true;
  private previousRunId = '';
  private previousInputRequest = '';
  private focusedEvidenceKey = '';
  private imageTrigger: HTMLElement | null = null;
  private copyFeedbackTimer: ReturnType<typeof setTimeout> | null = null;
  public readonly runId = input('');
  public readonly readOnly = input(false);
  public readonly compose = input(true);
  public readonly mode = input<'live' | 'review'>('review');
  public readonly liveSession = input<Session | null>(null);
  public readonly liveSteps = input<StepItemData[]>([]);
  public readonly startupProgress = input<StartupProgressEvent[]>([]);
  public readonly newRunPrompt = output<string>();
  public readonly renamed = output<void>();
  public readonly stopRun = output<string>();
  public readonly sidebarOpen = signal(true);
  public readonly editingTitle = signal(false);
  public readonly savingTitle = signal(false);
  public readonly copyNotice = signal('');
  public readonly copyNoticeFor = signal<string | null>(null);
  public readonly samples = [
    { title: 'Scan notifications', source: 'artemis/sdk/examples/smart_notification_assistant.py', prompt: 'Open the notification panel (swipe down from top). Scroll through the first 3 unread notifications. For each notification, identify the app name, title, and content. Tag messages from messaging apps or email as high priority.' },
    { title: 'Summarize a video', source: 'artemis/sdk/examples/video_transcription_example.py', prompt: '1. Open YouTube app\n2. Search for "Python tutorial"\n3. Start recording the screen\n4. Play the first video\n5. Wait for the first 30 seconds of the video to play\n6. Stop recording and tell me what was said in the video' }
  ];
  public titleDraft = '';
  public readonly chat = signal<ChatSession | null>(null);
  public readonly rootRun = signal<RunSummary | null>(null);
  public readonly error = signal<string | null>(null);
  public readonly resultsUnavailable = signal(false);
  public readonly selectedEvidence = signal<{ turnId: string; step: number } | null>(null);
  public readonly selectedTurnId = signal<string | null>(null);
  public readonly evidenceImage = signal<{ url: string; alt: string } | null>(null);
  public readonly sending = signal(false);
  public readonly latest = computed(() => this.chat()?.turns.at(-1));
  public readonly selectedTurn = computed(() => {
    const turns = this.chat()?.turns ?? [];
    return turns.find(turn => turn.turn_id === this.selectedTurnId()) ?? turns.at(-1) ?? null;
  });
  public readonly selectedResult = computed(() => {
    const turn = this.selectedTurn();
    return turn ? this.resultForTurn(turn.turn_id) : null;
  });
  public readonly latestResultLabel = computed(() => {
    if (this.resultsUnavailable()) return this.resultLabel(null);
    const turn = this.selectedTurn();
    return turn ? this.resultLabel(this.selectedResult(), turn) : '';
  });
  public readonly usageTurns = computed(() => (this.chat()?.turns ?? []).map(turn => ({ id: turn.turn_id, number: turn.number })));
  public readonly active = computed(() => ['queued', 'running', 'paused'].includes(this.latest()?.status ?? ''));
  public readonly paused = computed(() => this.latest()?.status === 'paused');
  public readonly inputRequest = computed(() => {
    if (!this.active()) return null;
    const requests = (this.chat()?.messages ?? []).filter(message => message.turn_id === this.latest()?.turn_id && message.kind === 'input_request');
    const request = requests.at(-1);
    return request?.payload?.['state'] === 'pending' && request.payload['expires_at'] * 1000 > Date.now() ? request : null;
  });
  public readonly canWrite = computed(() => !this.readOnly() && !this.rootRun()?.read_only);
  /** The session's phone, labelled model → product → "Android phone"; never the serial. */
  public readonly phone = computed(() => {
    const live = this.liveSession();
    const serial = this.rootRun()?.device_ref?.serial ?? live?.device_serial ?? live?.device_id;
    if (!serial || ['pending', 'null', 'undefined'].includes(serial)) return null;
    const listed = this.system.connectedDevices().find(device => device.serial === serial);
    const device = [listed, live && recordedDevice(live, serial)].find(known => known && isIdentifiedDevice(known));
    return { serial, label: device ? deviceTitle(device) : 'Android phone', online: listed?.state === 'device', known: this.system.readinessReport() !== null };
  });
  /** The session's phone is gone: the composer is off and the chip opens the way out. Unknown until the first phone list arrives. */
  public readonly disconnected = computed(() => { const phone = this.phone(); return !!phone && phone.known && !phone.online; });
  public readonly pickerOpen = signal(false);
  /** Other phones that could run this session's first prompt as a new session. */
  public readonly otherPhones = computed(() => this.phones.options().filter(option => option.usable && option.serial !== this.phone()?.serial));
  public readonly canReconnect = this.phones.canConnectFromBrowser;
  public readonly title = computed(() => this.chat()?.title || this.rootRun()?.chat_title || runTitle(this.rootRun()?.chat_prompt ?? this.rootRun()?.prompt));
  public readonly timeline = computed(() => (this.chat()?.timeline ?? this.chat()?.messages ?? []).filter(message => message.kind !== 'verdict'));
  public readonly stepCount = computed(() => this.timeline().filter(message => message.kind === 'step').length);
  public readonly emptyMessage = computed(() => this.active() ? 'Waiting for the first step. Steps appear here as the agent acts.'
    : this.canWrite() ? 'No steps were recorded. Send a new message to try again.' : 'No steps were recorded.');
  public readonly verdictTitle = computed(() => {
    const verdict = this.chat()?.verdict;
    if (!verdict) return '';
    if (verdict.status !== 'completed') return `Execution ${verdict.status}`;
    const tests = verdict.outcome?.tests;
    return tests?.failed ? 'Fail' : tests?.inconclusive || tests?.unchecked || !tests?.passed ? 'Inconclusive' : 'Pass';
  });
  public draft = '';
  public secretDraft = '';
  private retry: { content: string; id: string } | null = null;

  constructor() {
    this.destroyRef.onDestroy(() => {
      if (this.copyFeedbackTimer) clearTimeout(this.copyFeedbackTimer);
    });
    this.route.queryParamMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(params => {
      const turnId = params.get('turn');
      const step = Number(params.get('step'));
      this.selectedTurnId.set(turnId);
      this.selectedEvidence.set(turnId && Number.isInteger(step) && step > 0 ? { turnId, step } : null);
      this.focusedEvidenceKey = '';
    });
    effect(() => {
      const identity = `${this.runId()}:${this.canWrite()}:${this.inputRequest()?.payload?.['request_id'] ?? ''}`;
      if (identity !== this.previousInputRequest) {
        this.previousInputRequest = identity;
        this.secretDraft = '';
      }
    });
    effect((onCleanup) => {
      const id = this.runId();
      this.revision();
      if (id !== this.previousRunId) {
        if (this.copyFeedbackTimer) clearTimeout(this.copyFeedbackTimer);
        this.copyFeedbackTimer = null;
        this.copyNoticeFor.set(null);
        this.copyNotice.set('');
        this.followLatest = true;
        this.previousRunId = id;
        this.chat.set(null);
        this.rootRun.set(null);
        this.error.set(null);
        this.resultsUnavailable.set(false);
        this.focusedEvidenceKey = '';
        this.sending.set(false);
        this.editingTitle.set(false);
        this.savingTitle.set(false);
      }
      if (!id) return;
      const subscription = this.zone.runOutsideAngular(() => this.api.get(id).pipe(
        switchMap((run) => {
          this.rootRun.set(run);
          let pendingTurnId: string | null = null;
          let pendingPolls = 0;
          return timer(0, 1000).pipe(
            switchMap(() => this.api.chat(run.session_id).pipe(
              catchError((error) => { this.resultsUnavailable.set(true); this.reportError(error); return of(null); })
            )),
            tap((chat) => {
              if (!chat) return;
              const latestTurn = chat.turns.at(-1);
              const pending = latestTurn && !['queued', 'running', 'paused'].includes(latestTurn.status)
                ? chat.results?.find(result => result.evidence.turn_id === latestTurn.turn_id && result.label_state === 'pending')
                : null;
              if (!pending) {
                pendingTurnId = null;
                pendingPolls = 0;
                return;
              }
              if (pending.evidence.turn_id !== pendingTurnId) {
                pendingTurnId = pending.evidence.turn_id;
                pendingPolls = 0;
              }
              pendingPolls += 1;
            }),
            takeWhile((chat) => {
              if (!chat) return true;
              const latestTurn = chat.turns.at(-1);
              const activeTurn = ['queued', 'running', 'paused'].includes(latestTurn?.status ?? '');
              const terminalPending = latestTurn && chat.results?.some(result =>
                result.evidence.turn_id === latestTurn.turn_id && result.label_state === 'pending');
              return activeTurn
                || chat.verdict?.tip?.state === 'generating'
                || !!terminalPending && pendingTurnId === latestTurn?.turn_id && pendingPolls < 30;
            }, true)
          );
        }),
        catchError((error) => { this.reportError(error); return of(null); }),
        distinctUntilChanged((previous, current) => JSON.stringify(previous) === JSON.stringify(current))
      ).subscribe((chat) => this.zone.run(() => {
        if (chat) {
          this.resultsUnavailable.set(false);
          this.error.set(null);
          this.chat.set(chat);
        }
      })));
      onCleanup(() => subscription.unsubscribe());
    });
    afterRenderEffect(() => {
      this.chat();
      this.liveSteps();
      this.startupProgress();
      this.scrollToLatest();
      this.focusSelectedEvidence();
    });
  }

  public messages(turnId: string) {
    return this.chat()?.messages.filter((message) => message.turn_id === turnId) ?? [];
  }

  public reconnect(): void {
    this.pickerOpen.set(false);
    void this.phones.connectFromBrowser();
  }

  /** Same first prompt, another phone: fills a new session draft, like a sample prompt. */
  public runOn(serial: string): void {
    this.pickerOpen.set(false);
    this.phones.choose(serial);
    this.newRunPrompt.emit(this.rootRun()?.chat_prompt ?? this.rootRun()?.prompt ?? '');
  }

  public async copyTip(): Promise<void> {
    const text = this.chat()?.verdict?.tip?.text;
    if (!text || this.chat()?.verdict?.status !== 'completed') return;
    try {
      await navigator.clipboard.writeText(text);
      this.showCopyNotice('tip', 'Tip copied.');
    } catch {
      this.showCopyNotice('tip', 'Could not copy the tip. Select the text and copy it.');
    }
  }

  public resultForTurn(turnId: string): ChatTurnResult | null {
    return this.chat()?.results?.find(result => result.evidence?.turn_id === turnId) ?? null;
  }

  public resultLabel(result: ChatTurnResult | null, turn?: ChatSession['turns'][number]): string {
    if (this.resultsUnavailable()) return 'Không tải được kết quả · Thử lại';
    if (turn && ['queued', 'running', 'paused'].includes(turn.status)) return 'Đang chạy — chưa có kết quả';
    if (!result) {
      if (turn?.turn_id === this.latest()?.turn_id && this.chat()?.verdict) return `Kết quả · ${this.verdictTitle()}`;
      return 'Run cũ — chưa có verdict';
    }
    if (result.label_state === 'pending') return 'Đã kết thúc · Đang xác định kết quả';
    if (result.label_state === 'cancelled') return 'Đã huỷ';
    if (result.label_state === 'legacy') return 'Run cũ — chưa có verdict';
    if (result.label_state === 'error') return 'Không tải được kết quả · Thử lại';
    switch (result.verdict) {
      case 'pass': return 'Pass';
      case 'fail': return 'Fail';
      case 'blocked': return 'Blocked';
      default: return result.verdict;
    }
  }

  public verification(result: ChatTurnResult): string {
    return result.display_labels?.verification ?? 'Chưa xác minh';
  }

  public evidenceStep(result: ChatTurnResult): ChatMessage | null {
    return this.selectedStep(result);
  }

  public evidenceSteps(result: ChatTurnResult) {
    return this.timeline().filter(message => message.kind === 'step' && message.turn_id === result.evidence.turn_id);
  }

  public stepNumber(message: { payload?: Record<string, any> }): number | null {
    const step = Number(message.payload?.['step_number']);
    return Number.isInteger(step) && step > 0 ? step : null;
  }

  public selectedStep(result: ChatTurnResult) {
    if (result.evidence.step === null) return null;
    return this.evidenceSteps(result).find(message => this.stepNumber(message) === result.evidence.step) ?? null;
  }

  public stepElementId(message: ChatMessage): string | null {
    const step = this.stepNumber(message);
    return message.kind === 'step' && step !== null ? `step-${message.turn_id}-${step}` : null;
  }

  public evidenceText(value: unknown): string {
    if (typeof value === 'string') return value;
    if (value === null || value === undefined) return '';
    return typeof value === 'object' ? JSON.stringify(value) : String(value);
  }

  public selectTurn(turnId: string): void {
    const result = this.resultForTurn(turnId);
    this.selectTurnAndStep(turnId, result?.label_state === 'final' ? result.evidence.step : null);
  }

  public selectEvidence(turnId: string, step: number): void {
    this.selectTurnAndStep(turnId, step);
    this.focusSelectedEvidence();
  }

  public openEvidenceStep(event: MouseEvent, turnId: string, step: number): void {
    event.preventDefault();
    this.selectEvidence(turnId, step);
  }

  private selectTurnAndStep(turnId: string, step: number | null): void {
    this.selectedTurnId.set(turnId);
    this.selectedEvidence.set(step === null ? null : { turnId, step });
    this.focusedEvidenceKey = '';
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { turn: turnId, step },
      queryParamsHandling: 'merge'
    });
  }

  public isSelectedStep(message: { turn_id: string; payload?: Record<string, any> }): boolean {
    const selected = this.selectedEvidence();
    if (selected) return selected.turnId === message.turn_id && selected.step === this.stepNumber(message);
    const result = this.selectedResult();
    return !!result && result.evidence.turn_id === message.turn_id
      && result.evidence.step === this.stepNumber(message);
  }

  public resultScreenshot(result: ChatTurnResult): string | null {
    return result.evidence.screenshot_url ? mediaUrl(result.evidence.screenshot_url) : null;
  }

  public openEvidenceImage(result: ChatTurnResult, trigger: HTMLElement): void {
    const url = this.resultScreenshot(result);
    if (!url) return;
    this.imageTrigger = trigger;
    this.evidenceImage.set({ url, alt: `Decisive screenshot for step ${result.evidence.step ?? ''}`.trim() });
    const dialog = this.evidenceDialog()?.nativeElement;
    if (dialog && !dialog.open) dialog.showModal();
  }

  public onEvidenceDialogClose(): void {
    this.evidenceImage.set(null);
    this.imageTrigger?.focus({ preventScroll: true });
    this.imageTrigger = null;
  }

  public helpHref(result: ChatTurnResult): string {
    return `${smartqaHelpRoute.path}#${encodeURIComponent(result.category ?? 'unknown')}`;
  }

  public retryResults(): void {
    this.resultsUnavailable.set(false);
    this.error.set(null);
    this.revision.update(value => value + 1);
  }

  public async copyRunId(turnId: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(turnId);
      this.showCopyNotice(turnId, 'Đã sao chép mã lượt chạy.');
    } catch {
      this.showCopyNotice(turnId, 'Không thể sao chép mã lượt chạy. Hãy chọn và sao chép mã.');
    }
  }

  private showCopyNotice(owner: string, message: string): void {
    if (this.copyFeedbackTimer) clearTimeout(this.copyFeedbackTimer);
    this.copyNoticeFor.set(owner);
    this.copyNotice.set(message);
    this.copyFeedbackTimer = setTimeout(() => {
      this.copyNoticeFor.set(null);
      this.copyNotice.set('');
      this.copyFeedbackTimer = null;
    }, 2000);
  }

  private focusSelectedEvidence(): void {
    const selected = this.selectedEvidence();
    const history = this.historyElement()?.nativeElement;
    if (!selected || !history) return;
    const key = `${selected.turnId}:${selected.step}`;
    if (key === this.focusedEvidenceKey) return;
    const row = Array.from(history.querySelectorAll<HTMLElement>('[data-event-kind="step"]'))
      .find(element => element.dataset['turnId'] === selected.turnId && Number(element.dataset['stepNumber']) === selected.step);
    const details = row?.querySelector<HTMLDetailsElement>('details');
    if (!row || !details) return;
    details.open = true;
    row.scrollIntoView({ block: 'center' });
    details.querySelector<HTMLElement>('summary')?.focus({ preventScroll: true });
    this.focusedEvidenceKey = key;
  }

  public trackScroll(): void {
    const history = this.historyElement()?.nativeElement;
    if (history) this.followLatest = history.scrollHeight - history.clientHeight - history.scrollTop < 1;
  }

  public scrollToLatest(): void {
    const history = this.historyElement()?.nativeElement;
    if (!history || !this.followLatest) return;
    const verdicts = history.querySelectorAll<HTMLElement>('[data-testid="run-verdict"]');
    const latestVerdict = verdicts.item(verdicts.length - 1);
    if (window.matchMedia('(max-width: 799px)').matches && latestVerdict?.dataset['labelState'] === 'final') {
      history.scrollTop += latestVerdict.getBoundingClientRect().top - history.getBoundingClientRect().top;
      return;
    }
    history.scrollTop = history.scrollHeight;
  }

  public beginRename(): void {
    this.titleDraft = this.title().slice(0, 120);
    this.editingTitle.set(true);
  }

  public saveTitle(): void {
    const chat = this.chat();
    const title = this.titleDraft.trim();
    if (!chat || !title || title.length > 120 || this.savingTitle()) return;
    this.savingTitle.set(true);
    this.api.renameChat(chat.session_id, title).subscribe({
      next: updated => {
        if (this.chat()?.session_id !== chat.session_id) return;
        this.chat.set(updated);
        this.rootRun.update(run => run ? { ...run, chat_title: updated.title } : run);
        this.editingTitle.set(false);
        this.savingTitle.set(false);
        this.renamed.emit();
      },
      error: error => { this.savingTitle.set(false); this.reportError(error); }
    });
  }

  public send(): void {
    const chat = this.chat();
    const content = this.draft.trim();
    if (!chat || !content || !this.canWrite() || this.sending() || this.inputRequest() || this.disconnected()) return;
    this.retry = this.retry?.content === content ? this.retry : { content, id: crypto.randomUUID() };
    this.sending.set(true);
    this.error.set(null);
    this.api.sendChat(chat.session_id, content, this.retry.id).subscribe({
      next: () => { if (this.chat()?.session_id !== chat.session_id) return; this.draft = ''; this.retry = null; this.sending.set(false); this.refresh(chat.session_id); },
      error: (error) => { if (this.chat()?.session_id !== chat.session_id) return; if (error.status >= 400 && error.status < 500) this.retry = null; this.sending.set(false); this.reportError(error); }
    });
  }

  public sendInput(): void {
    const chat = this.chat();
    const request = this.inputRequest();
    const value = this.secretDraft;
    if (!chat || !request || !value || value.length > 4096 || !this.canWrite() || this.sending()) return;
    this.secretDraft = '';
    this.sending.set(true);
    this.error.set(null);
    this.api.sendInput(chat.session_id, request.payload!['request_id'], value).subscribe({
      next: () => {
        if (this.chat()?.session_id !== chat.session_id) return;
        this.sending.set(false);
        this.refresh(chat.session_id);
      },
      error: () => {
        if (this.chat()?.session_id !== chat.session_id) return;
        this.secretDraft = '';
        this.sending.set(false);
        this.error.set('Input was not accepted. The request may have expired. Ask the agent for a new request.');
      }
    });
  }

  public control(action: 'pause' | 'continue'): void {
    const chat = this.chat();
    if (!chat || !this.active() || !this.canWrite() || this.sending()) return;
    this.sending.set(true);
    this.error.set(null);
    this.api.controlChat(chat.session_id, action, crypto.randomUUID()).subscribe({
      next: () => { if (this.chat()?.session_id !== chat.session_id) return; this.sending.set(false); this.refresh(chat.session_id); },
      error: (error) => { if (this.chat()?.session_id !== chat.session_id) return; this.sending.set(false); this.reportError(error); }
    });
  }

  private refresh(identity: string): void {
    if (this.chat()?.session_id === identity) this.revision.update(value => value + 1);
  }

  public readonly actionMarker = actionMarker;

  public screenshot(payload: Record<string, any> | undefined, key: 'pre_image_name' | 'post_image_name' = 'post_image_name'): string | null {
    const image = payload?.[key];
    return typeof image === 'string' && !this.failedImages().has(image) ? mediaUrl('/images/' + encodeURIComponent(image)) : null;
  }

  public failScreenshot(payload: Record<string, any> | undefined, key: 'pre_image_name' | 'post_image_name' = 'post_image_name'): void {
    const image = payload?.[key];
    if (typeof image === 'string') this.failedImages.update(images => new Set([...images, image]));
  }

  public turnNumber(identity: string): number {
    return this.chat()?.turns.find(turn => turn.turn_id === identity)?.number ?? 1;
  }

  private reportError(error: HttpErrorResponse): void {
    this.error.set(deployDrainMessage(error) ?? (typeof error.error?.detail === 'string' ? error.error.detail : 'Chat could not connect. Your draft is kept. Try again.'));
  }
}
