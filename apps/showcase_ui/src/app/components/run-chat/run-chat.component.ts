import { afterEveryRender, ChangeDetectionStrategy, Component, computed, effect, ElementRef, inject, input, NgZone, output, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CdkTextareaAutosize } from '@angular/cdk/text-field';
import { HttpErrorResponse } from '@angular/common/http';
import { DatePipe, JsonPipe } from '@angular/common';
import { catchError, of, switchMap, takeWhile, timer } from 'rxjs';
import { ChatSession, RunSummary } from '../../core/models/run.model';
import { Session } from '../../core/models/session.model';
import { StepItemData } from '../../core/models/stream.model';
import { StartupProgressEvent } from '../../services/agent.service';
import { RunsService } from '../../services/runs.service';
import { ShellLayoutService } from '../../services/shell-layout.service';
import { mediaUrl } from '../../utils/app-url.util';
import { runTitle } from '../../utils/run-title.util';
import { SessionSidebarComponent } from '../session-sidebar/session-sidebar.component';

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
  public readonly shell = inject(ShellLayoutService);
  private readonly failedImages = signal(new Set<string>());
  private readonly revision = signal(0);
  private readonly historyElement = viewChild<ElementRef<HTMLElement>>('history');
  private followLatest = true;
  private previousRunId = '';
  private previousInputRequest = '';
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
  public readonly samples = [
    { title: 'Scan notifications', source: 'artemis/sdk/examples/smart_notification_assistant.py', prompt: 'Open the notification panel (swipe down from top). Scroll through the first 3 unread notifications. For each notification, identify the app name, title, and content. Tag messages from messaging apps or email as high priority.' },
    { title: 'Summarize a video', source: 'artemis/sdk/examples/video_transcription_example.py', prompt: '1. Open YouTube app\n2. Search for "Python tutorial"\n3. Start recording the screen\n4. Play the first video\n5. Wait for the first 30 seconds of the video to play\n6. Stop recording and tell me what was said in the video' }
  ];
  public titleDraft = '';
  public readonly chat = signal<ChatSession | null>(null);
  public readonly rootRun = signal<RunSummary | null>(null);
  public readonly error = signal<string | null>(null);
  public readonly sending = signal(false);
  public readonly latest = computed(() => this.chat()?.turns.at(-1));
  public readonly active = computed(() => ['queued', 'running', 'paused'].includes(this.latest()?.status ?? ''));
  public readonly paused = computed(() => this.latest()?.status === 'paused');
  public readonly inputRequest = computed(() => {
    if (!this.active()) return null;
    const requests = (this.chat()?.messages ?? []).filter(message => message.turn_id === this.latest()?.turn_id && message.kind === 'input_request');
    const request = requests.at(-1);
    return request?.payload?.['state'] === 'pending' && request.payload['expires_at'] * 1000 > Date.now() ? request : null;
  });
  public readonly canWrite = computed(() => !this.readOnly() && !this.rootRun()?.read_only);
  public readonly title = computed(() => this.chat()?.title || this.rootRun()?.chat_title || runTitle(this.rootRun()?.chat_prompt ?? this.rootRun()?.prompt));
  public readonly timeline = computed(() => (this.chat()?.timeline ?? this.chat()?.messages ?? [])
    .filter(message => message.kind !== 'verdict' && (this.shell.technicalDetails() || message.kind !== 'thinking')));
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
        this.followLatest = true;
        this.previousRunId = id;
        this.chat.set(null);
        this.rootRun.set(null);
        this.error.set(null);
        this.sending.set(false);
        this.editingTitle.set(false);
        this.savingTitle.set(false);
      }
      if (!id) return;
      const subscription = this.zone.runOutsideAngular(() => this.api.get(id).pipe(
        switchMap((run) => {
          this.rootRun.set(run);
          return timer(0, 1000).pipe(switchMap(() => this.api.chat(run.session_id).pipe(
            catchError((error) => { this.reportError(error); return of(null); })
          )), takeWhile(chat => !chat || ['queued', 'running', 'paused'].includes(chat.turns.at(-1)?.status ?? '') || chat.verdict?.tip?.state === 'generating', true));
        }),
        catchError((error) => { this.reportError(error); return of(null); })
      ).subscribe((chat) => this.zone.run(() => { if (chat) this.chat.set(chat); })));
      onCleanup(() => subscription.unsubscribe());
    });
    afterEveryRender(() => this.scrollToLatest());
  }

  public messages(turnId: string) {
    return this.chat()?.messages.filter((message) => message.turn_id === turnId) ?? [];
  }

  public async copyTip(): Promise<void> {
    const text = this.chat()?.verdict?.tip?.text;
    if (!text || this.chat()?.verdict?.status !== 'completed') return;
    try {
      await navigator.clipboard.writeText(text);
      this.copyNotice.set('Tip copied.');
    } catch {
      this.copyNotice.set('Could not copy the tip. Select the text and copy it.');
    }
  }

  public trackScroll(): void {
    const history = this.historyElement()?.nativeElement;
    if (history) this.followLatest = history.scrollHeight - history.clientHeight - history.scrollTop < 80;
  }

  public scrollToLatest(): void {
    const history = this.historyElement()?.nativeElement;
    if (history && this.followLatest) history.scrollTop = history.scrollHeight;
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
    if (!chat || !content || !this.canWrite() || this.sending() || this.inputRequest()) return;
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

  public screenshot(payload: Record<string, any> | undefined): string | null {
    const image = payload?.['post_image_name'] || payload?.['pre_image_name'];
    return typeof image === 'string' && !this.failedImages().has(image) ? mediaUrl('/images/' + encodeURIComponent(image)) : null;
  }

  public failScreenshot(payload: Record<string, any> | undefined): void {
    const image = payload?.['post_image_name'] || payload?.['pre_image_name'];
    if (typeof image === 'string') this.failedImages.update(images => new Set([...images, image]));
  }

  public turnNumber(identity: string): number {
    return this.chat()?.turns.find(turn => turn.turn_id === identity)?.number ?? 1;
  }

  private reportError(error: HttpErrorResponse): void {
    this.error.set(typeof error.error?.detail === 'string' ? error.error.detail : 'Chat could not connect. Your draft is kept. Try again.');
  }
}
