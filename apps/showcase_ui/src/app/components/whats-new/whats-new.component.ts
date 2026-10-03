import { HttpClient } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, ElementRef, HostListener, ViewChild, computed, effect, inject, signal } from '@angular/core';
import { AgentService } from '../../services/agent.service';
import {
  hasUnseenWhatsNewEntries,
  parseWhatsNewEntries,
  shouldAutoOpenWhatsNew,
  WhatsNewEntry
} from '../../utils/whats-new.util';

export const WHATS_NEW_LAST_SEEN_KEY = 'smartqa.whats-new.last-seen-id';

@Component({
  selector: 'app-whats-new',
  standalone: true,
  template: `
    <dialog
      #dialog
      class="whats-new-dialog"
      aria-labelledby="whats-new-title"
      (close)="onDialogClosed()"
      (click)="closeOnBackdrop($event)"
    >
      <section class="whats-new-sheet">
        <header class="sheet-header">
          <div>
            <p class="sheet-eyebrow">SMARTQA UPDATES</p>
            <h2 id="whats-new-title">What's New</h2>
          </div>
          <button
            #closeButton
            type="button"
            class="close-button"
            aria-label="Close What's New"
            autofocus
            (click)="close()"
          >
            <span class="material-symbols-outlined" aria-hidden="true">close</span>
          </button>
        </header>
        <div class="sheet-content" tabindex="0" aria-label="What's New updates">
          @if (!entriesLoaded()) {
            <p class="empty-state">Loading updates…</p>
          } @else if (entries().length === 0) {
            <p class="empty-state">You're up to date.</p>
          } @else {
            @for (entry of entries(); track entry.id) {
              <article class="update-card">
                <time [attr.datetime]="entry.date">{{ entry.date }}</time>
                <h3>{{ entry.title }}</h3>
                @if (entry.body) {
                  <p>{{ entry.body }}</p>
                }
              </article>
            }
          }
        </div>
        <footer class="sheet-footer">
          <span class="update-count">{{ entries().length }} updates</span>
          <button type="button" class="done-button" (click)="close()">Done</button>
        </footer>
      </section>
    </dialog>
  `,
  styles: [`
    :host { display: contents; }
    .whats-new-dialog {
      position: fixed;
      inset: 0 0 0 auto;
      width: min(440px, 100vw);
      height: 100dvh;
      max-width: 100vw;
      max-height: none;
      margin: 0;
      padding: 0;
      border: 0;
      border-radius: 20px 0 0 20px;
      overflow: visible;
      color: #172033;
      background: transparent;
    }
    .whats-new-dialog::backdrop { background: rgb(15 23 42 / 42%); backdrop-filter: blur(2px); }
    .whats-new-sheet {
      box-sizing: border-box;
      display: flex;
      flex-direction: column;
      width: 100%;
      height: 100%;
      padding: 28px;
      background: #fff;
      box-shadow: -18px 0 50px rgb(15 23 42 / 16%);
    }
    .sheet-header, .sheet-footer { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    .sheet-header { padding-bottom: 20px; border-bottom: 1px solid #e5eaf2; }
    .sheet-eyebrow { margin: 0 0 6px; color: #59708f; font-size: 11px; font-weight: 700; letter-spacing: .12em; }
    h2 { margin: 0; font-size: 24px; letter-spacing: -.03em; }
    .close-button, .done-button { border: 0; border-radius: 10px; cursor: pointer; font: inherit; }
    .close-button { display: grid; width: 40px; height: 40px; place-items: center; color: #334155; background: #f1f5f9; }
    .close-button .material-symbols-outlined { font-size: 20px; }
    .sheet-content { flex: 1; overflow: auto; padding: 20px 0; }
    .update-card { padding: 18px; border: 1px solid #e2e8f0; border-radius: 16px; background: #f8fafc; }
    .update-card + .update-card { margin-top: 12px; }
    .update-card time { color: #64748b; font-size: 12px; }
    .update-card h3 { margin: 8px 0; font-size: 17px; }
    .update-card p, .empty-state { margin: 0; color: #475569; font-size: 14px; line-height: 1.6; }
    .sheet-footer { padding-top: 18px; border-top: 1px solid #e5eaf2; }
    .update-count { color: #64748b; font-size: 12px; }
    .done-button { padding: 10px 18px; color: #fff; background: #2563eb; font-weight: 600; }
    :is(.close-button, .done-button):focus-visible { outline: 3px solid #93c5fd; outline-offset: 3px; }
    @media (max-width: 520px) {
      .whats-new-dialog { width: 100vw; border-radius: 0; }
      .whats-new-sheet { padding: 22px; }
    }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class WhatsNewComponent {
  private readonly http = inject(HttpClient);
  private readonly agentService = inject(AgentService);
  private readonly dialogRef = signal<HTMLDialogElement | null>(null);
  private autoOpenConsidered = false;

  @ViewChild('dialog', { static: true })
  set dialog(element: ElementRef<HTMLDialogElement> | undefined) {
    this.dialogRef.set(element?.nativeElement ?? null);
  }

  public readonly entries = signal<WhatsNewEntry[]>([]);
  public readonly entriesLoaded = signal(false);
  public readonly isRunActive = computed(() => {
    const status = this.agentService.agentStatus();
    return status === 'running'
      || status === 'paused'
      || this.agentService.activeTasks().some(task => task.status === 'running' || task.status === 'paused');
  });

  constructor() {
    this.http.get<unknown>('/whats-new.json').subscribe({
      next: value => {
        this.entries.set(parseWhatsNewEntries(value));
        this.entriesLoaded.set(true);
        this.updateNavState();
      },
      error: () => {
        this.entriesLoaded.set(true);
        this.updateNavState();
      }
    });

    effect(() => {
      const dialog = this.dialogRef();
      const entries = this.entries();
      if (!dialog || !this.entriesLoaded() || !this.agentService.hasFetchedStatus() || this.autoOpenConsidered) {
        return;
      }
      const isRunActive = this.isRunActive();
      const hasPromptDraft = this.agentService.whatsNewPromptDraft();
      const hasError = this.agentService.whatsNewErrorVisible();
      if (isRunActive || hasPromptDraft || hasError) return;

      this.autoOpenConsidered = true;
      if (shouldAutoOpenWhatsNew(entries, this.readLastSeenId(), isRunActive, hasPromptDraft, hasError)) {
        this.showDialog(dialog);
      }
    });
  }

  public open(): void {
    this.autoOpenConsidered = true;
    const dialog = this.dialogRef();
    if (dialog) this.showDialog(dialog);
  }

  public close(): void {
    const dialog = this.dialogRef();
    if (dialog?.open) dialog.close();
  }

  public closeOnBackdrop(event: MouseEvent): void {
    if (event.target === this.dialogRef()) this.close();
  }

  @HostListener('document:keydown', ['$event'])
  public keepFocusInsideDialog(event: KeyboardEvent): void {
    const dialog = this.dialogRef();
    if (event.key !== 'Tab' || !dialog?.open) return;

    const focusableElements = Array.from(dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    ));
    const first = focusableElements[0];
    const last = focusableElements.at(-1);
    if (!first || !last) return;

    const activeElement = document.activeElement;
    if (event.shiftKey && (activeElement === first || activeElement === dialog || !dialog.contains(activeElement))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (activeElement === last || activeElement === dialog || !dialog.contains(activeElement))) {
      event.preventDefault();
      first.focus();
    }
  }

  public onDialogClosed(): void {
    const latestEntry = this.entries()[0];
    if (latestEntry) {
      try {
        localStorage.setItem(WHATS_NEW_LAST_SEEN_KEY, latestEntry.id);
      } catch {
      }
    }
    this.agentService.whatsNewHasUnread.set(false);
  }

  private updateNavState(): void {
    const entries = this.entries();
    const hasEntries = this.entriesLoaded() && entries.length > 0;
    this.agentService.whatsNewHasUpdates.set(hasEntries);
    this.agentService.whatsNewHasUnread.set(
      hasEntries && hasUnseenWhatsNewEntries(entries, this.readLastSeenId())
    );
  }

  private showDialog(dialog: HTMLDialogElement): void {
    if (!dialog.open) dialog.showModal();
  }

  private readLastSeenId(): string | null {
    try {
      return localStorage.getItem(WHATS_NEW_LAST_SEEN_KEY);
    } catch {
      return null;
    }
  }
}
