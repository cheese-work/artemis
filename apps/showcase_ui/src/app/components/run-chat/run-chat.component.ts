import { ChangeDetectionStrategy, Component, computed, effect, inject, input, NgZone, output, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CdkTextareaAutosize } from '@angular/cdk/text-field';
import { HttpErrorResponse } from '@angular/common/http';
import { catchError, of, switchMap, timer } from 'rxjs';
import { ChatSession, RunSummary } from '../../core/models/run.model';
import { Session } from '../../core/models/session.model';
import { StepItemData } from '../../core/models/stream.model';
import { StartupProgressEvent } from '../../services/agent.service';
import { RunsService } from '../../services/runs.service';
import { RunViewComponent } from '../run-view/run-view.component';
import { runTitle } from '../../utils/run-title.util';
import { SessionSidebarComponent } from '../session-sidebar/session-sidebar.component';

@Component({
  selector: 'app-run-chat',
  standalone: true,
  imports: [FormsModule, CdkTextareaAutosize, RunViewComponent, SessionSidebarComponent],
  templateUrl: './run-chat.component.html',
  styleUrl: './run-chat.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunChatComponent {
  private readonly api = inject(RunsService);
  private readonly zone = inject(NgZone);
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
  public titleDraft = '';
  public readonly chat = signal<ChatSession | null>(null);
  public readonly rootRun = signal<RunSummary | null>(null);
  public readonly error = signal<string | null>(null);
  public readonly sending = signal(false);
  public readonly latest = computed(() => this.chat()?.turns.at(-1));
  public readonly active = computed(() => ['queued', 'running', 'paused'].includes(this.latest()?.status ?? ''));
  public readonly paused = computed(() => this.latest()?.status === 'paused');
  public readonly canWrite = computed(() => !this.readOnly() && !this.rootRun()?.read_only);
  public readonly title = computed(() => this.chat()?.title || this.rootRun()?.chat_title || runTitle(this.rootRun()?.chat_prompt ?? this.rootRun()?.prompt));
  public draft = '';
  private retry: { content: string; id: string } | null = null;

  constructor() {
    effect((onCleanup) => {
      const id = this.runId();
      this.chat.set(null);
      this.rootRun.set(null);
      this.error.set(null);
      this.sending.set(false);
      this.editingTitle.set(false);
      this.savingTitle.set(false);
      if (!id) return;
      const subscription = this.zone.runOutsideAngular(() => this.api.get(id).pipe(
        switchMap((run) => {
          this.rootRun.set(run);
          return timer(0, 1000).pipe(switchMap(() => this.api.chat(run.session_id).pipe(
            catchError((error) => { this.reportError(error); return of(null); })
          )));
        }),
        catchError((error) => { this.reportError(error); return of(null); })
      ).subscribe((chat) => this.zone.run(() => { if (chat) this.chat.set(chat); })));
      onCleanup(() => subscription.unsubscribe());
    });
  }

  public messages(turnId: string) {
    return this.chat()?.messages.filter((message) => message.turn_id === turnId) ?? [];
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
    if (!chat || !content || !this.canWrite() || this.sending()) return;
    this.retry = this.retry?.content === content ? this.retry : { content, id: crypto.randomUUID() };
    this.sending.set(true);
    this.error.set(null);
    this.api.sendChat(chat.session_id, content, this.retry.id).subscribe({
      next: () => { if (this.chat()?.session_id !== chat.session_id) return; this.draft = ''; this.retry = null; this.sending.set(false); this.refresh(chat.session_id); },
      error: (error) => { if (this.chat()?.session_id !== chat.session_id) return; if (error.status >= 400 && error.status < 500) this.retry = null; this.sending.set(false); this.reportError(error); }
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
    this.api.chat(identity).subscribe({ next: (chat) => { if (this.chat()?.session_id === identity) this.chat.set(chat); }, error: (error) => this.reportError(error) });
  }

  private reportError(error: HttpErrorResponse): void {
    this.error.set(typeof error.error?.detail === 'string' ? error.error.detail : 'Chat could not connect. Your draft is kept. Try again.');
  }
}
